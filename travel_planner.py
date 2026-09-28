"""Gemini와 Kakao Local API를 연결한 국내 여행 계획 CLI."""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
RESULTS_DIR = BASE_DIR / "results"

GEMINI_PRIMARY_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash"
GEMINI_MODELS = (GEMINI_PRIMARY_MODEL, GEMINI_FALLBACK_MODEL)
KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}

RECOMMENDATION_FIELDS = {
    "recommended_city",
    "weather",
    "events",
    "reason",
}
REQUIRED_REPORT_HEADINGS = (
    "# 여행 추천",
    "## 추천 지역",
    "## 추천 이유",
    "## 예상 날씨 / 계절 정보",
    "## 행사 / 이벤트",
    "## 추천 맛집",
    "## 1일 여행 일정",
)


class TravelPlannerError(RuntimeError):
    """사용자에게 안전하게 안내할 수 있는 여행 플래너 오류."""


class GeminiResponseError(TravelPlannerError):
    """Gemini 응답의 형식이 올바르지 않을 때 발생하는 오류."""


def validate_date(date_text):
    """실제 존재하는 YYYY-MM-DD 날짜를 같은 형식으로 반환한다."""
    try:
        parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "날짜는 실제 존재하는 YYYY-MM-DD 형식이어야 합니다."
        ) from error

    return parsed_date.strftime("%Y-%m-%d")


def create_parser():
    """명령행 인자 파서를 만든다."""
    parser = argparse.ArgumentParser(
        description="선택한 날짜의 국내 여행지와 맛집을 추천합니다."
    )
    parser.add_argument(
        "-date",
        "--date",
        dest="travel_date",
        required=True,
        type=validate_date,
        help="여행 날짜(YYYY-MM-DD)",
    )
    return parser


def load_api_keys(dotenv_path=ENV_PATH):
    """키 값을 출력하지 않고 환경 변수에서 API 키를 읽는다."""
    load_dotenv(dotenv_path=dotenv_path, override=False)
    return os.getenv("GEMINI_API_KEY"), os.getenv("KAKAO_REST_API_KEY")


def create_gemini_client(api_key):
    """제한 시간을 둔 Gemini 클라이언트를 만든다."""
    if not api_key:
        raise TravelPlannerError(
            "GEMINI_API_KEY가 없습니다. .env 파일에 키를 설정해 주세요."
        )
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=30000),
    )


def _error_status(error):
    """SDK 예외에서 HTTP 상태 코드만 안전하게 추출한다."""
    return getattr(error, "status_code", None) or getattr(error, "code", None)


def _parse_json_object(response_text):
    """Gemini 문자열 응답을 JSON 객체로 변환한다."""
    try:
        parsed = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise GeminiResponseError(
            "Gemini 응답을 JSON으로 해석하지 못했습니다."
        ) from error

    if not isinstance(parsed, dict):
        raise GeminiResponseError("Gemini JSON 응답은 객체여야 합니다.")
    return parsed


def validate_recommendation(recommendation):
    """1차 추천 JSON의 필수 필드와 타입을 검사한다."""
    missing_fields = RECOMMENDATION_FIELDS - set(recommendation)
    if missing_fields:
        raise GeminiResponseError("Gemini 추천 JSON에 필수 필드가 없습니다.")

    text_fields = ("recommended_city", "weather", "reason")
    if any(
        not isinstance(recommendation[field], str)
        or not recommendation[field].strip()
        for field in text_fields
    ):
        raise GeminiResponseError("Gemini 추천 JSON의 문자열 필드가 올바르지 않습니다.")

    events = recommendation["events"]
    if (
        not isinstance(events, list)
        or not 1 <= len(events) <= 3
        or any(not isinstance(event, str) or not event.strip() for event in events)
    ):
        raise GeminiResponseError(
            "Gemini 추천 JSON의 events는 1~3개의 문자열이어야 합니다."
        )

    return {
        "recommended_city": recommendation["recommended_city"].strip(),
        "weather": recommendation["weather"].strip(),
        "events": [event.strip() for event in events],
        "reason": recommendation["reason"].strip(),
    }


def _recommendation_schema():
    """Gemini Structured Output용 JSON 스키마를 반환한다."""
    return {
        "type": "OBJECT",
        "properties": {
            "recommended_city": {"type": "STRING"},
            "weather": {"type": "STRING"},
            "events": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "minItems": 1,
                "maxItems": 3,
            },
            "reason": {"type": "STRING"},
        },
        "required": sorted(RECOMMENDATION_FIELDS),
    }


def request_recommendation(client, travel_date):
    """Gemini에서 날짜에 맞는 국내 여행지 Structured JSON을 받는다."""
    prompt = (
        f"여행 날짜는 {travel_date}입니다. 이 시기에 여행하기 좋은 대한민국 지역 "
        "1곳을 추천하세요. recommended_city에는 Kakao Local 검색에 쓸 수 있는 "
        "간결한 국내 지역명을 작성하세요. weather는 실시간 예보가 아니라 해당 "
        "시기의 일반적인 계절·날씨 특성만 작성하세요. events는 1~3개이며, 최신 "
        "일정이 불확실하면 항목에 '확인 필요'를 명시하세요. reason은 2~4문장으로 "
        "작성하세요. 지정된 JSON 구조만 반환하세요."
    )

    last_error = None
    for attempt, model in enumerate(GEMINI_MODELS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_recommendation_schema(),
                ),
            )
            recommendation = _parse_json_object(response.text)
            return validate_recommendation(recommendation)
        except GeminiResponseError as error:
            last_error = error
            if attempt == 0:
                continue
            raise
        except Exception as error:
            status = _error_status(error)
            if status in {401, 403}:
                raise TravelPlannerError(
                    "Gemini API 인증에 실패했습니다. .env의 키를 확인해 주세요."
                ) from error
            last_error = error
            if attempt == 0 and (status in TRANSIENT_STATUS_CODES or status is None):
                continue
            if status is not None:
                raise TravelPlannerError(
                    f"Gemini 여행지 추천 요청에 실패했습니다 (HTTP {status})."
                ) from error
            raise TravelPlannerError("Gemini 여행지 추천 요청에 실패했습니다.") from error

    raise TravelPlannerError("Gemini 여행지 추천 요청에 실패했습니다.") from last_error


def _restaurant_from_document(document):
    """Kakao 문서 한 건을 과제용 맛집 구조로 바꾼다."""
    restaurant = {
        "name": document.get("place_name", ""),
        "address": document.get("road_address_name")
        or document.get("address_name", ""),
        "category": document.get("category_name", ""),
        "url": document.get("place_url", ""),
        "x": document.get("x", ""),
        "y": document.get("y", ""),
    }
    if document.get("phone"):
        restaurant["phone"] = document["phone"]
    return restaurant


def search_restaurants(city, kakao_api_key, errors, http_get=requests.get):
    """Kakao Local에서 도시 맛집을 최대 5곳 찾고 실패는 errors에 기록한다."""
    if not kakao_api_key:
        errors.append("Kakao REST API 키가 설정되지 않음")
        return []

    try:
        response = http_get(
            KAKAO_KEYWORD_URL,
            headers={"Authorization": f"KakaoAK {kakao_api_key}"},
            params={"query": f"{city} 맛집", "size": 5},
            timeout=15,
        )
    except requests.RequestException:
        errors.append("Kakao API 네트워크 오류")
        return []

    if response.status_code in {401, 403}:
        errors.append("Kakao API 인증 실패")
        return []
    if response.status_code >= 500:
        errors.append("Kakao API 서버 오류")
        return []
    if not response.ok:
        errors.append(f"Kakao API 요청 실패(HTTP {response.status_code})")
        return []

    try:
        documents = response.json().get("documents", [])
    except (ValueError, AttributeError):
        errors.append("Kakao API 응답 형식 오류")
        return []

    restaurants = [
        _restaurant_from_document(document) for document in documents[:5]
    ]
    if not restaurants:
        errors.append("맛집 검색 결과 없음")
    return restaurants


def _validate_markdown_report(report):
    """최종 리포트에 과제 필수 섹션이 모두 있는지 검사한다."""
    if not isinstance(report, str) or not report.strip():
        raise GeminiResponseError("Gemini 최종 리포트가 비어 있습니다.")
    if any(heading not in report for heading in REQUIRED_REPORT_HEADINGS):
        raise GeminiResponseError("Gemini 최종 리포트에 필수 섹션이 없습니다.")
    return report.strip() + "\n"


def request_final_report(client, travel_date, recommendation, restaurants, errors):
    """추천 JSON과 Kakao 맛집을 Gemini가 종합한 Markdown을 받는다."""
    source_data = {
        "travel_date": travel_date,
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
    }
    prompt = (
        "다음 JSON 데이터를 바탕으로 한국어 국내 여행 리포트를 Markdown으로 "
        "작성하세요. JSON에 없는 맛집 정보나 행사 일정을 지어내지 마세요. 맛집이 "
        "비어 있으면 추천 맛집 섹션에 반드시 '데이터 없음'이라고 쓰세요. 각 맛집은 "
        "이름, 주소, 카테고리, Kakao Map URL을 포함하세요. 1일 일정은 오전, 점심, "
        "오후, 저녁 순서로 작성하세요. 반드시 다음 제목을 정확히 모두 사용하세요:\n"
        + "\n".join(REQUIRED_REPORT_HEADINGS)
        + "\n\n입력 JSON:\n"
        + json.dumps(source_data, ensure_ascii=False, indent=2)
    )

    last_error = None
    for attempt, model in enumerate(GEMINI_MODELS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="text/plain"),
            )
            return _validate_markdown_report(response.text)
        except GeminiResponseError as error:
            last_error = error
            if attempt == 0:
                continue
            raise
        except Exception as error:
            status = _error_status(error)
            if status in {401, 403}:
                raise TravelPlannerError(
                    "Gemini API 인증에 실패했습니다. .env의 키를 확인해 주세요."
                ) from error
            last_error = error
            if attempt == 0 and (status in TRANSIENT_STATUS_CODES or status is None):
                continue
            if status in TRANSIENT_STATUS_CODES:
                raise TravelPlannerError(
                    f"Gemini 최종 리포트 요청에 실패했습니다 (HTTP {status})."
                ) from error
            raise TravelPlannerError("Gemini 최종 리포트 요청에 실패했습니다.") from error

    raise TravelPlannerError("Gemini 최종 리포트 요청에 실패했습니다.") from last_error


def save_results(
    travel_date,
    recommendation,
    restaurants,
    errors,
    report,
    output_dir=RESULTS_DIR,
):
    """Raw JSON과 Markdown을 results 폴더에 저장하고 다시 읽어 검증한다."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"travel_result_{travel_date}.json"
    markdown_path = output_dir / f"travel_report_{travel_date}.md"

    result_data = {
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
    }
    json_path.write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(report, encoding="utf-8")

    saved_json = json.loads(json_path.read_text(encoding="utf-8"))
    saved_markdown = markdown_path.read_text(encoding="utf-8")
    if saved_json != result_data or saved_markdown != report:
        raise TravelPlannerError("결과 파일 저장 후 검증에 실패했습니다.")

    return json_path, markdown_path


def run_pipeline(travel_date, output_dir=RESULTS_DIR):
    """Gemini → Kakao → Gemini → 파일 저장 전체 흐름을 실행한다."""
    gemini_key, kakao_key = load_api_keys()
    client = create_gemini_client(gemini_key)
    errors = []

    print("[1/5] Gemini 국내 여행지 추천 요청 중...")
    recommendation = request_recommendation(client, travel_date)
    city = recommendation["recommended_city"]
    print(f"[2/5] 추천 지역 확인: {city}")

    print("[3/5] Kakao Local 맛집 검색 중...")
    restaurants = search_restaurants(city, kakao_key, errors)
    print(f"      맛집 {len(restaurants)}곳 확인")

    print("[4/5] Gemini 최종 여행 리포트 생성 중...")
    report = request_final_report(
        client,
        travel_date,
        recommendation,
        restaurants,
        errors,
    )

    print("[5/5] JSON과 Markdown 결과 저장 및 검증 중...")
    json_path, markdown_path = save_results(
        travel_date,
        recommendation,
        restaurants,
        errors,
        report,
        output_dir,
    )
    return {
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
        "json_path": json_path,
        "markdown_path": markdown_path,
    }


def main(argv=None):
    """CLI를 실행하고 성공 시 0, 실패 시 1을 반환한다."""
    args = create_parser().parse_args(argv)
    try:
        result = run_pipeline(args.travel_date)
    except TravelPlannerError as error:
        print(f"오류: {error}")
        return 1
    except Exception:
        print("오류: 예상하지 못한 문제가 발생했습니다. 설정과 네트워크를 확인하세요.")
        return 1

    print("\n여행 추천 리포트 생성이 완료되었습니다.")
    print(f"JSON: {result['json_path']}")
    print(f"Markdown: {result['markdown_path']}")
    if result["errors"]:
        print("참고: 일부 부가 데이터 오류가 결과 JSON에 기록되었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
