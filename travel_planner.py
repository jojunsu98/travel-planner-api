"""Gemini와 Kakao Local API를 연결한 국내 여행 계획 CLI."""

import argparse
import json
import os
import re
from datetime import datetime, timedelta
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
    "## 가는 방법",
    "## 예상 비용",
)
REQUEST_SEARCH_CATEGORIES = {"restaurant", "cafe", "activity", "nightlife"}
MAX_REQUEST_SEARCH_QUERIES = 20


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


def validate_city(city_text):
    """도시 옵션에서 앞뒤 공백을 제거하고 빈 값은 거부한다."""
    city = city_text.strip()
    if not city:
        raise argparse.ArgumentTypeError("도시는 공백일 수 없습니다.")
    return city


def validate_travel_request(request_text):
    """여행 자연어 요청의 앞뒤 공백을 제거하고 빈 값은 거부한다."""
    request = request_text.strip()
    if not request:
        raise argparse.ArgumentTypeError("여행 요청은 공백일 수 없습니다.")
    return request


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
    parser.add_argument(
        "--origin",
        help="출발지(선택)",
    )
    parser.add_argument(
        "--city",
        type=validate_city,
        help="여행할 도시를 고정(선택)",
    )
    parser.add_argument(
        "--request",
        dest="user_request",
        type=validate_travel_request,
        help="여행 목적과 조건을 자연어로 입력(선택)",
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


def _travel_request_schema():
    """자연어 여행 요청 Structured Output 스키마를 반환한다."""
    return {
        "type": "OBJECT",
        "properties": {
            "duration": {"type": "STRING"},
            "budget": {"type": "STRING"},
            "preferred_areas": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "activities": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "food_preferences": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "cafe_preferences": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "nightlife_preferences": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "search_queries": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "query": {"type": "STRING"},
                        "area": {"type": "STRING"},
                        "category": {"type": "STRING"},
                    },
                    "required": ["query", "area", "category"],
                },
            },
        },
        "required": [
            "duration",
            "budget",
            "preferred_areas",
            "activities",
            "food_preferences",
            "cafe_preferences",
            "nightlife_preferences",
            "search_queries",
        ],
    }


def _validate_travel_request_analysis(analysis):
    """자연어 요청 분석 결과의 필수 필드와 검색어를 검증한다."""
    text_fields = ("duration", "budget")
    if any(
        not isinstance(analysis.get(field), str) or not analysis[field].strip()
        for field in text_fields
    ):
        raise GeminiResponseError("여행 요청 분석의 기간 또는 예산이 올바르지 않습니다.")

    list_fields = (
        "preferred_areas",
        "activities",
        "food_preferences",
        "cafe_preferences",
        "nightlife_preferences",
    )
    normalized = {field: [] for field in list_fields}
    for field in list_fields:
        values = analysis.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise GeminiResponseError(f"여행 요청 분석의 {field}가 올바르지 않습니다.")
        normalized[field] = [value.strip() for value in values]

    search_queries = analysis.get("search_queries")
    if not isinstance(search_queries, list):
        raise GeminiResponseError("여행 요청 분석의 검색어가 올바르지 않습니다.")
    normalized_queries = []
    for item in search_queries:
        if not isinstance(item, dict):
            raise GeminiResponseError("여행 요청 분석의 검색어 항목이 올바르지 않습니다.")
        query = item.get("query")
        area = item.get("area")
        category = item.get("category")
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(area, str)
            or not isinstance(category, str)
            or category not in REQUEST_SEARCH_CATEGORIES
        ):
            raise GeminiResponseError("여행 요청 분석의 검색어 항목이 올바르지 않습니다.")
        normalized_queries.append(
            {"query": query.strip(), "area": area.strip(), "category": category}
        )

    return {
        "duration": analysis["duration"].strip(),
        "budget": analysis["budget"].strip(),
        **normalized,
        "search_queries": normalized_queries,
    }


def analyze_travel_request(client, travel_request, city=None, destination=None):
    """자연어 조건을 Structured Output으로 분석하고 지역 검색어를 보완한다."""
    prompt = (
        "사용자의 여행 요청을 지정된 JSON 구조로 분석하세요. 기간, 예산, 선호 지역, "
        "활동 순서, 음식·카페·야간 활동 선호를 추출하세요. 사용자가 언급한 지역과 "
        "활동 순서는 그대로 보존하세요. search_queries는 각 항목에 query, area, "
        "category를 넣으세요. category는 restaurant, cafe, activity, nightlife 중 "
        "하나를 사용하고, 선호 지역마다 요청된 음식·카페·야간 활동에 맞는 검색어를 "
        "만드세요. 요청에 없는 예산이나 선호는 '미지정' 또는 빈 목록으로 표현하고 "
        "없는 정보를 지어내지 마세요.\n\n"
        + json.dumps(
            {
                "request": travel_request,
                "fixed_city": city,
                "destination": destination,
            },
            ensure_ascii=False,
        )
    )

    last_error = None
    for attempt, model in enumerate(GEMINI_MODELS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_travel_request_schema(),
                ),
            )
            analysis = _validate_travel_request_analysis(
                _parse_json_object(response.text)
            )
            required_queries = []
            for area in analysis["preferred_areas"]:
                required_queries.append(
                    {"query": f"{area} 맛집", "area": area, "category": "restaurant"}
                )
                for preference in analysis["cafe_preferences"][:1]:
                    required_queries.append(
                        {
                            "query": f"{area} {preference}",
                            "area": area,
                            "category": "cafe",
                        }
                    )
                for preference in analysis["nightlife_preferences"][:1]:
                    required_queries.append(
                        {
                            "query": f"{area} {preference}",
                            "area": area,
                            "category": "nightlife",
                        }
                    )

            unique_queries = []
            seen_queries = set()
            for item in required_queries + analysis["search_queries"]:
                key = item["query"].casefold()
                if key not in seen_queries:
                    seen_queries.add(key)
                    unique_queries.append(item)
                if len(unique_queries) >= MAX_REQUEST_SEARCH_QUERIES:
                    break
            analysis["search_queries"] = unique_queries
            return analysis
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
                    f"Gemini 여행 요청 분석에 실패했습니다 (HTTP {status})."
                ) from error
            raise TravelPlannerError("Gemini 여행 요청 분석에 실패했습니다.") from error

    raise TravelPlannerError("Gemini 여행 요청 분석에 실패했습니다.") from last_error


def request_recommendation(client, travel_date, city=None):
    """Gemini에서 날짜에 맞는 국내 여행지 Structured JSON을 받는다."""
    destination_instruction = (
        f"사용자가 지정한 '{city}'를 여행지로 고정하세요. 다른 도시를 추천하지 "
        "말고 recommended_city에 지정 도시명을 그대로 작성하세요. weather, "
        "events, reason도 모두 이 도시를 기준으로 작성하세요. "
        if city
        else "이 시기에 여행하기 좋은 대한민국 지역 1곳을 추천하세요. "
    )
    prompt = (
        f"여행 날짜는 {travel_date}입니다. "
        + destination_instruction
        + "recommended_city에는 Kakao Local 검색에 쓸 수 있는 "
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
            recommendation = validate_recommendation(recommendation)
            if city and recommendation["recommended_city"] != city:
                raise GeminiResponseError("Gemini 추천 지역이 지정 도시와 일치하지 않습니다.")
            return recommendation
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


def search_requested_places(search_queries, kakao_api_key, errors, http_get=requests.get):
    """요청 분석 검색어를 Kakao에서 조회하고 중복 장소를 합친다."""
    if not kakao_api_key:
        errors.append("Kakao REST API 키가 설정되지 않음")
        return []

    places = []
    place_indexes = {}
    for search_query in search_queries:
        query = search_query["query"]
        try:
            response = http_get(
                KAKAO_KEYWORD_URL,
                headers={"Authorization": f"KakaoAK {kakao_api_key}"},
                params={"query": query, "size": 5},
                timeout=15,
            )
        except requests.RequestException:
            errors.append(f"Kakao API 네트워크 오류: {query}")
            continue

        if response.status_code in {401, 403}:
            errors.append(f"Kakao API 인증 실패: {query}")
            continue
        if response.status_code >= 500:
            errors.append(f"Kakao API 서버 오류(HTTP {response.status_code}): {query}")
            continue
        if not response.ok:
            errors.append(f"Kakao API 요청 실패(HTTP {response.status_code}): {query}")
            continue

        try:
            documents = response.json().get("documents", [])
        except (ValueError, AttributeError):
            errors.append(f"Kakao API 응답 형식 오류: {query}")
            continue

        if not documents:
            errors.append(f"검색 결과 없음: {query}")
            continue

        for document in documents[:5]:
            place = _restaurant_from_document(document)
            place["place_id"] = str(
                document.get("id")
                or place["url"]
                or f"{place['name']}|{place['address']}"
            )
            place["kind"] = search_query["category"]
            place["kinds"] = [search_query["category"]]
            place["areas"] = [search_query["area"]] if search_query["area"] else []
            place["search_queries"] = [query]

            identity = (
                str(document["id"])
                if document.get("id")
                else place["url"]
                or f"{place['name'].casefold()}|{place['address'].casefold()}"
            )
            if identity not in place_indexes:
                place_indexes[identity] = len(places)
                places.append(place)
                continue

            existing = places[place_indexes[identity]]
            if search_query["category"] not in existing["kinds"]:
                existing["kinds"].append(search_query["category"])
            for field, value in (
                ("areas", search_query["area"]),
                ("search_queries", query),
            ):
                if value and value not in existing[field]:
                    existing[field].append(value)

    return places


def _validate_markdown_report(report):
    """최종 리포트에 과제 필수 섹션이 모두 있는지 검사한다."""
    if not isinstance(report, str) or not report.strip():
        raise GeminiResponseError("Gemini 최종 리포트가 비어 있습니다.")
    if any(heading not in report for heading in REQUIRED_REPORT_HEADINGS):
        raise GeminiResponseError("Gemini 최종 리포트에 필수 섹션이 없습니다.")
    return report.strip() + "\n"


def request_final_report(
    client, travel_date, recommendation, restaurants, errors, origin=None, city=None
):
    """추천 JSON과 Kakao 맛집을 Gemini가 종합한 Markdown을 받는다."""
    source_data = {
        "travel_date": travel_date,
        "origin": origin,
        "destination": recommendation["recommended_city"],
        "requested_city": city,
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
    }
    city_instruction = (
        f"사용자가 지정한 '{city}'가 목적지입니다. 다른 도시를 추천하거나 "
        "목적지를 변경하지 마세요.\n"
        if city
        else ""
    )
    prompt = (
        city_instruction
        + "다음 JSON 데이터를 바탕으로 한국어 국내 여행 리포트를 Markdown으로 "
        "작성하세요. JSON에 없는 맛집 정보나 행사 일정을 지어내지 마세요. 맛집이 "
        "비어 있으면 추천 맛집 섹션에 반드시 '데이터 없음 (검색 결과 없음)'이라고 "
        "쓰세요. "
        "상단에는 여행 날짜, 출발지, 목적지, 예산을 간결한 bullet 요약으로 작성하세요. "
        "제목은 '# {날짜} {목적지} 여행 플랜' 형식을 사용하고 짧은 문단과 일관된 "
        "Markdown 제목 계층을 사용하세요. 장소는 Kakao 결과의 지역·이름·카테고리·주소로 "
        "구분하세요. 전화번호는 제공될 때만 표시하고, 장소 URL은 원문으로 노출하지 "
        "말고 '[카카오맵에서 보기](URL)' 링크로 표시하세요. 일정은 날짜별 Day 단위로 "
        "정리하고, 이동·비용은 예상임을 표시하세요. 반드시 다음 기존 제목도 모두 "
        "사용하세요:\n"
        + "\n".join(REQUIRED_REPORT_HEADINGS)
        + "\n'가는 방법'에는 다음 항목을 각각 bullet로 작성하세요: 출발지, 목적지, "
        "추천 이동수단, 예상 소요시간, 이동 팁. '예상 비용'에는 왕복 교통비, 식비, "
        "카페/간식, 관광지/입장료, 예상 총비용을 각각 bullet로 작성하세요. 모든 "
        "금액은 1인 기준의 범위로 표시하고 '예상 비용'임을 분명히 하세요. "
        "실시간 교통요금 API를 사용하지 "
        "않으므로 실제 요금과 운행·운영 정보는 방문 전 확인해야 한다는 안내를 "
        "포함하세요. 출발지가 입력되지 않았으면 출발지를 '미지정'으로 표시하고 "
        "실제 출발 노선이나 소요시간을 지어내지 마세요. 왕복 교통비는 산정 불가라고 "
        "밝히고, 예상 총비용은 교통비 제외 범위임을 명시하세요.\n"
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


def _request_trip_report_schema():
    """요청 기반 여행 보고서 Structured Output 스키마를 반환한다."""
    cost_range = {
        "type": "OBJECT",
        "properties": {
            "minimum": {"type": "INTEGER"},
            "maximum": {"type": "INTEGER"},
        },
        "required": ["minimum", "maximum"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "getting_there": {
                "type": "OBJECT",
                "properties": {
                    "mode": {"type": "STRING"},
                    "duration": {"type": "STRING"},
                },
                "required": ["mode", "duration"],
            },
            "estimated_costs": {
                "type": "OBJECT",
                "properties": {
                    "round_trip_transport": cost_range,
                    "food": cost_range,
                    "cafe_snack": cost_range,
                    "attractions": cost_range,
                    "total": cost_range,
                },
                "required": [
                    "round_trip_transport",
                    "food",
                    "cafe_snack",
                    "attractions",
                    "total",
                ],
            },
            "itinerary": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "day": {"type": "STRING"},
                        "time": {"type": "STRING"},
                        "area": {"type": "STRING"},
                        "activity_index": {"type": "INTEGER"},
                        "place_ids": {
                            "type": "ARRAY",
                            "items": {"type": "STRING"},
                        },
                    },
                    "required": ["day", "time", "area", "activity_index", "place_ids"],
                },
            },
        },
        "required": [
            "getting_there",
            "estimated_costs",
            "itinerary",
        ],
    }


def _validate_request_trip_report(report, analysis, places, destination=None):
    """요청형 보고서 필드와 일정 인덱스 및 Kakao 장소 ID를 검증한다."""
    getting_there = report.get("getting_there")
    transport_fields = ("mode", "duration")
    if not isinstance(getting_there, dict) or any(
        not isinstance(getting_there.get(field), str)
        or not getting_there[field].strip()
        for field in transport_fields
    ):
        raise GeminiResponseError("여행 보고서 이동 정보가 올바르지 않습니다.")

    estimated_costs = report.get("estimated_costs")
    cost_fields = (
        "round_trip_transport",
        "food",
        "cafe_snack",
        "attractions",
        "total",
    )
    if not isinstance(estimated_costs, dict):
        raise GeminiResponseError("여행 보고서 예상 비용이 올바르지 않습니다.")
    normalized_costs = {}
    for field in cost_fields:
        value = estimated_costs.get(field)
        if not isinstance(value, dict):
            raise GeminiResponseError("여행 보고서 예상 비용이 올바르지 않습니다.")
        minimum, maximum = value.get("minimum"), value.get("maximum")
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or minimum < 0
            or maximum < minimum
        ):
            raise GeminiResponseError("여행 보고서 예상 비용 범위가 올바르지 않습니다.")
        normalized_costs[field] = {"minimum": minimum, "maximum": maximum}

    itinerary = report.get("itinerary")
    activities = analysis["activities"]
    if not isinstance(itinerary, list) or len(itinerary) != len(activities):
        raise GeminiResponseError("여행 보고서 일정이 요청 활동과 일치하지 않습니다.")

    preferred_areas = set(analysis["preferred_areas"])
    valid_place_ids = {place["place_id"] for place in places}
    normalized_itinerary = []
    seen_activity_indexes = set()
    for item in itinerary:
        if not isinstance(item, dict):
            raise GeminiResponseError("여행 보고서 일정 항목이 올바르지 않습니다.")
        day, time, area = item.get("day"), item.get("time"), item.get("area")
        activity_index = item.get("activity_index")
        place_ids = item.get("place_ids")
        if (
            not all(isinstance(value, str) and value.strip() for value in (day, time, area))
            or not isinstance(activity_index, int)
            or isinstance(activity_index, bool)
            or not 0 <= activity_index < len(activities)
            or activity_index in seen_activity_indexes
            or not isinstance(place_ids, list)
            or any(not isinstance(place_id, str) for place_id in place_ids)
            or (preferred_areas and area not in preferred_areas)
            or (not preferred_areas and destination and area != destination)
        ):
            raise GeminiResponseError("여행 보고서 일정 항목이 올바르지 않습니다.")
        seen_activity_indexes.add(activity_index)
        normalized_itinerary.append(
            {
                "day": day.strip(),
                "time": time.strip(),
                "area": area.strip(),
                "activity_index": activity_index,
                "place_ids": [
                    place_id for place_id in place_ids if place_id in valid_place_ids
                ],
            }
        )

    if seen_activity_indexes != set(range(len(activities))):
        raise GeminiResponseError("여행 보고서 일정에서 요청 활동이 누락됐습니다.")

    return {
        "getting_there": {
            field: getting_there[field].strip() for field in transport_fields
        },
        "estimated_costs": normalized_costs,
        "itinerary": sorted(
            normalized_itinerary, key=lambda item: item["activity_index"]
        ),
    }


def request_structured_trip_report(
    client, analysis, recommendation, places, errors, origin=None
):
    """요청과 Kakao 장소 자료를 바탕으로 ID 참조형 구조화 리포트를 받는다."""
    source_data = {
        "analysis": analysis,
        "destination": recommendation["recommended_city"],
        "recommendation": recommendation,
        "origin": origin or "미지정",
        "places": places,
        "search_errors": errors,
    }
    prompt = (
        "입력 JSON으로 여행 리포트 데이터를 생성하세요. 사용자 예산 안에서 계획하고, "
        "비용 항목은 원화의 최소·최대 정수 금액으로 반환하세요. 실제 교통 요금이나 운영 정보는 "
        "확인할 수 없으므로 지어내지 마세요. 출발지가 미지정이면 교통비는 산정 "
        "불가에 해당하는 0 범위를 사용하고 총액은 교통비를 제외해 산출하세요. 사용자가 제시한 "
        "선호 지역을 최대한 유지하고, 활동 순서는 analysis.activities의 순서를 "
        "그대로 따라 activity_index에 연결하세요. activity_index는 0부터 시작합니다. "
        "일정의 area는 선호 지역에서만 "
        "선택하세요. itinerary.place_ids에는 입력 places의 place_id만 넣으세요. "
        "장소 상호명은 반환하지 말고 itinerary.place_ids로만 장소를 참조하세요. "
        "검색 결과가 없는 카테고리는 빈 장소 목록으로 처리하고 전체 여행 계획은 "
        "계속 작성하세요.\n\n"
        + json.dumps(source_data, ensure_ascii=False, indent=2)
    )

    last_error = None
    for attempt, model in enumerate(GEMINI_MODELS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_request_trip_report_schema(),
                ),
            )
            return _validate_request_trip_report(
                _parse_json_object(response.text),
                analysis,
                places,
                destination=recommendation["recommended_city"],
            )
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
                    f"Gemini 여행 리포트 요청에 실패했습니다 (HTTP {status})."
                ) from error
            raise TravelPlannerError("Gemini 여행 리포트 요청에 실패했습니다.") from error

    raise TravelPlannerError("Gemini 여행 리포트 요청에 실패했습니다.") from last_error


def _format_kakao_place(place):
    """Kakao 장소 한 곳을 읽기 쉬운 Markdown 항목으로 만든다."""
    lines = [f"- **{place['name']}**"]
    if place["category"]:
        lines.append(f"  - 카테고리: {place['category']}")
    if place["address"]:
        lines.append(f"  - 주소: {place['address']}")
    if place.get("phone"):
        lines.append(f"  - 전화: {place['phone']}")
    if place["url"]:
        lines.append(f"  - [카카오맵에서 보기]({place['url']})")
    return "\n".join(lines)


def _format_cost_range(cost_range):
    """검증된 최소·최대 원화 비용을 사람이 읽기 쉬운 범위로 표시한다."""
    return f"{cost_range['minimum']:,}~{cost_range['maximum']:,}원"


def _format_itinerary_day(day, travel_date):
    """Gemini 날짜 라벨을 Day N과 여행 날짜로 정규화한다."""
    match = re.match(r"\s*(?:day\s*)?(\d+)\s*(?:일차?)?", day, re.IGNORECASE)
    if not match:
        return day
    day_number = int(match.group(1))
    trip_day = datetime.strptime(travel_date, "%Y-%m-%d") + timedelta(
        days=day_number - 1
    )
    return f"Day {day_number} · {trip_day:%Y-%m-%d}"


def render_request_trip_report(
    travel_date, recommendation, analysis, places, report_data, origin=None
):
    """검증된 Kakao 장소를 사용해 요청형 최종 Markdown을 렌더링한다."""
    destination = recommendation["recommended_city"]
    places_by_id = {place["place_id"]: place for place in places}
    preferred_areas = analysis["preferred_areas"] or [destination]
    category_titles = (
        ("맛집", {"restaurant"}),
        ("카페", {"cafe"}),
        ("활동/펍", {"activity", "nightlife"}),
    )

    def place_section(title, kinds, area):
        lines = [f"#### {title}"]
        matches = [
            place
            for place in places
            if kinds.intersection(place["kinds"])
            and (place["areas"] or [destination])[0] == area
        ]
        lines.extend(_format_kakao_place(place) for place in matches)
        if not matches:
            lines.append("- 검색 결과 없음")
        return lines

    lines = [
        f"# {travel_date} {destination} 여행 플랜",
        f"- **여행 날짜:** {travel_date}",
        f"- **출발지:** {origin or '미지정'}",
        f"- **목적지:** {destination}",
        f"- **여행 기간:** {analysis['duration']}",
        f"- **예산:** {analysis['budget']}",
        "- **선호 지역:** " + (" · ".join(analysis["preferred_areas"]) or "미지정"),
        "- **여행 목적:** " + (" · ".join(analysis["activities"]) or "미지정"),
        "## 여행 요약",
        f"{analysis['duration']} 동안 {destination}의 선호 지역을 방문하며 "
        f"'{analysis['budget']}' 범위에 맞춘 일정입니다.",
        "## 추천 장소",
    ]

    shown_place_ids = set()
    for area in preferred_areas:
        lines.append(f"### {area}")
        for title, kinds in category_titles:
            matches = [
                place
                for place in places
                if kinds.intersection(place["kinds"])
                and (place["areas"] or [destination])[0] == area
            ]
            shown_place_ids.update(place["place_id"] for place in matches)
            lines.extend(place_section(title, kinds, area))

    other_places = [place for place in places if place["place_id"] not in shown_place_ids]
    if other_places:
        lines.append("### 기타 검색 결과")
        for title, kinds in category_titles:
            matches = [place for place in other_places if kinds.intersection(place["kinds"])]
            lines.append(f"#### {title}")
            lines.extend(_format_kakao_place(place) for place in matches)
            if not matches:
                lines.append("- 검색 결과 없음")

    lines.extend(
        [
            "## 참고 정보",
            f"- 추천 이유: {recommendation['reason']}",
            f"- 계절 정보: {recommendation['weather']}",
            "- 행사/이벤트: " + (" · ".join(recommendation["events"]) or "정보 없음"),
            "## 가는 방법",
            f"- 출발지: {origin or '미지정'}",
            f"- 목적지: {destination}",
        ]
    )

    transport = report_data["getting_there"]
    lines.extend(
        [
            "- 추천 이동수단: "
            + (transport["mode"] if origin else "출발지 미지정으로 산정 불가"),
            "- 예상 소요시간: "
            + (transport["duration"] if origin else "출발지 미지정으로 산정 불가"),
            "- 이동 팁: 이동 경로와 운행 정보는 방문 전에 확인하세요.",
            "## 예상 비용",
            "| 항목 | 예상 비용 |\n| --- | ---: |\n"
            "| 왕복 교통비 | "
            + (
                _format_cost_range(report_data["estimated_costs"]["round_trip_transport"])
                if origin
                else "산정 불가 (출발지 미지정)"
            )
            + " |\n| 식비 | "
            + _format_cost_range(report_data["estimated_costs"]["food"])
            + " |\n| 카페/간식 | "
            + _format_cost_range(report_data["estimated_costs"]["cafe_snack"])
            + " |\n| 관광/야간 활동·기타 | "
            + _format_cost_range(report_data["estimated_costs"]["attractions"])
            + " |\n| 총 예상 비용 | "
            + _format_cost_range(report_data["estimated_costs"]["total"])
            + (" (교통비 제외)" if not origin else "")
            + " |",
            "모든 금액은 1인 기준 예상 범위입니다. 실제 요금은 방문 전에 확인하세요.",
            "## 일정",
        ]
    )

    day_groups = {}
    for item in report_data["itinerary"]:
        day_groups.setdefault(item["day"], []).append(item)
    for day, day_items in day_groups.items():
        lines.append(f"### {_format_itinerary_day(day, travel_date)}")
        for item in day_items:
            activity = analysis["activities"][item["activity_index"]]
            lines.append(f"- **{item['time']} · {item['area']}**: {activity}")
            matched_places = [
                places_by_id[place_id]
                for place_id in item["place_ids"]
                if place_id in places_by_id
            ]
            if matched_places:
                lines.extend(_format_kakao_place(place) for place in matched_places)
            else:
                lines.append("  - 연결된 Kakao 검색 장소 없음")

    lines.extend(
        [
            "## 주의사항",
            "- 장소명과 지도 링크는 Kakao 실제 검색 결과입니다.",
            "- 여행 일정과 추천 정보는 Gemini 생성 내용이며, 장소명과 혼동하지 마세요.",
            "- 예상 비용은 실시간 요금 조회가 아니며, 실제 요금과 영업·운영 정보는 방문 전에 확인하세요.",
        ]
    )
    return "\n\n".join(lines) + "\n"


def _transient_error_details(error):
    """Gemini 실패 원인 체인에서 일시 상태 코드 또는 네트워크 오류를 찾는다."""
    current = error
    while current is not None:
        status = _error_status(current)
        if isinstance(status, int) or (
            isinstance(status, str) and status.isdigit()
        ):
            status = int(status)
            if status in TRANSIENT_STATUS_CODES:
                return status, True

        if isinstance(current, requests.RequestException):
            return None, True

        module = current.__class__.__module__
        name = current.__class__.__name__
        if module.startswith(("httpx", "httpcore")) and name in {
            "ConnectError",
            "ConnectTimeout",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "RemoteProtocolError",
            "TimeoutException",
            "WriteError",
        }:
            return None, True

        current = current.__cause__ or current.__context__

    status_match = re.search(r"\bHTTP\s+(\d{3})\b", str(error), re.IGNORECASE)
    if status_match:
        status = int(status_match.group(1))
        if status in TRANSIENT_STATUS_CODES:
            return status, True
    return None, False


def _final_report_failure_summary(error):
    """로그나 API 원문 대신 fallback에 기록할 안전한 오류 요약을 만든다."""
    status, is_transient = _transient_error_details(error)
    if not is_transient:
        raise ValueError("일시적 Gemini 오류가 아닙니다.")
    if status is not None:
        return f"Gemini 최종 리포트 생성 실패 (HTTP {status}); Python fallback 사용"
    return "Gemini 최종 리포트 생성 실패 (일시적 네트워크 오류); Python fallback 사용"


def render_fallback_report(
    travel_date,
    recommendation,
    errors,
    restaurants=None,
    request_analysis=None,
    places=None,
    origin=None,
):
    """이미 확보한 Gemini 추천과 Kakao 장소만으로 최종 Markdown을 만든다."""
    destination = recommendation["recommended_city"]
    gathered_places = places if places is not None else (restaurants or [])
    areas = (
        request_analysis["preferred_areas"]
        if request_analysis is not None
        else [destination]
    ) or [destination]
    category_titles = (
        ("맛집", {"restaurant"}),
        ("카페", {"cafe"}),
        ("활동/펍", {"activity", "nightlife"}),
    )

    def place_kinds(place):
        return set(place.get("kinds", [place.get("kind", "restaurant")]))

    def place_area(place):
        return (place.get("areas") or [destination])[0]

    lines = [
        f"# {travel_date} {destination} 여행 플랜",
        f"- **여행 날짜:** {travel_date}",
        f"- **출발지:** {origin or '미지정'}",
        f"- **목적지:** {destination}",
        "- **여행 기간:** "
        + (request_analysis["duration"] if request_analysis else "날짜 기준 여행"),
        "- **예산:** "
        + (request_analysis["budget"] if request_analysis else "미지정"),
        "- **선호 지역:** "
        + (
            " · ".join(request_analysis["preferred_areas"])
            if request_analysis and request_analysis["preferred_areas"]
            else "미지정"
        ),
    ]
    if request_analysis:
        lines.append(
            "- **여행 목적:** "
            + (" · ".join(request_analysis["activities"]) or "미지정")
        )

    lines.extend(
        [
            "## 여행 요약",
            f"{destination} 여행입니다. Gemini 최종 리포트를 사용할 수 없어 "
            "확보된 추천 및 검색 자료로 기본 보고서를 구성했습니다.",
            "## 추천 장소",
        ]
    )
    shown_place_ids = set()
    for area in areas:
        lines.append(f"### {area}")
        area_places = [place for place in gathered_places if place_area(place) == area]
        for title, kinds in category_titles:
            matches = [place for place in area_places if kinds.intersection(place_kinds(place))]
            lines.append(f"#### {title}")
            lines.extend(_format_kakao_place(place) for place in matches)
            shown_place_ids.update(
                place.get("place_id") or place.get("url") or place["name"]
                for place in matches
            )
            if not matches:
                lines.append("- 검색 결과 없음")

    other_places = [
        place
        for place in gathered_places
        if (place.get("place_id") or place.get("url") or place["name"])
        not in shown_place_ids
    ]
    if other_places:
        lines.append("### 기타 검색 결과")
        lines.extend(_format_kakao_place(place) for place in other_places)

    lines.extend(
        [
            "## 참고 정보",
            f"- 추천 이유: {recommendation['reason']}",
            f"- 계절 정보: {recommendation['weather']}",
            "- 행사/이벤트: " + (" · ".join(recommendation["events"]) or "정보 없음"),
            "## 가는 방법",
            f"- 출발지: {origin or '미지정'}",
            f"- 목적지: {destination}",
            "- 추천 이동수단 및 예상 소요시간: Gemini 최종 리포트 실패로 미산정",
            "- 이동 경로와 운행 정보는 방문 전에 확인하세요.",
            "## 예상 비용",
            "| 항목 | 예상 비용 |\n"
            "| --- | ---: |\n"
            "| 왕복 교통비 | 출발지 또는 요금 자료 부족으로 미산정 |\n"
            "| 식비 | 상세 예상 자료 없음 |\n"
            "| 카페/간식 | 상세 예상 자료 없음 |\n"
            "| 관광/야간 활동·기타 | 상세 예상 자료 없음 |\n"
            "| 총 예상 비용 | "
            + (
                f"사용자 예산 참고: {request_analysis['budget']} (실제 추정액 아님)"
                if request_analysis
                else "예산 미지정으로 미산정"
            )
            + " |",
        ]
    )

    lines.append("## 일정")
    if request_analysis and request_analysis["activities"]:
        lines.append("### 요청 활동 순서")
        lines.extend(
            f"{index}. {activity}"
            for index, activity in enumerate(request_analysis["activities"], start=1)
        )
    else:
        lines.append("- Gemini 최종 리포트 실패로 상세 일정은 미산정입니다.")

    lines.extend(
        [
            "## 주의사항",
            "- 장소명, 주소, 전화번호, 링크는 Kakao 실제 검색 결과입니다.",
            "- 요금·이동시간은 산정하지 못했으며, 실제 정보는 방문 전에 확인하세요.",
            "## API 오류 요약",
        ]
    )
    lines.extend(f"- {error}" for error in errors)
    return "\n\n".join(lines) + "\n"


def save_results(
    travel_date,
    recommendation,
    restaurants,
    errors,
    report,
    output_dir=RESULTS_DIR,
    request_analysis=None,
    places=None,
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
    if request_analysis is not None:
        result_data["request_analysis"] = request_analysis
        result_data["places"] = places or []
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


def run_pipeline(
    travel_date, output_dir=RESULTS_DIR, origin=None, city=None, user_request=None
):
    """Gemini → Kakao → Gemini → 파일 저장 전체 흐름을 실행한다."""
    gemini_key, kakao_key = load_api_keys()
    client = create_gemini_client(gemini_key)
    errors = []

    if city is None:
        print("[1/5] Gemini 국내 여행지 추천 요청 중...")
        recommendation = request_recommendation(client, travel_date)
        city = recommendation["recommended_city"]
    else:
        print(f"[1/5] 지정 도시 사용: {city}")
        recommendation = {
            "recommended_city": city,
            "weather": "확인 필요",
            "events": [],
            "reason": "사용자가 지정한 도시를 목적지로 사용했습니다.",
        }
    print(f"[2/5] 추천 지역 확인: {city}")

    request_analysis = None
    places = None
    if user_request is None:
        print("[3/5] Kakao Local 맛집 검색 중...")
        restaurants = search_restaurants(city, kakao_key, errors)
        print(f"      맛집 {len(restaurants)}곳 확인")

        print("[4/5] Gemini 최종 여행 리포트 생성 중...")
        try:
            report = request_final_report(
                client,
                travel_date,
                recommendation,
                restaurants,
                errors,
                origin=origin,
                city=city,
            )
        except TravelPlannerError as error:
            if not _transient_error_details(error)[1]:
                raise
            errors.append(_final_report_failure_summary(error))
            report = render_fallback_report(
                travel_date,
                recommendation,
                errors,
                restaurants=restaurants,
                origin=origin,
            )
    else:
        print("[3/5] Gemini 여행 요청 조건 분석 중...")
        request_analysis = analyze_travel_request(
            client,
            user_request,
            city=city,
            destination=city,
        )
        print(
            f"      Kakao 검색어 {len(request_analysis['search_queries'])}개 생성"
        )

        print("[4/5] Kakao Local 장소 검색 및 Gemini 일정 구성 중...")
        places = search_requested_places(
            request_analysis["search_queries"], kakao_key, errors
        )
        restaurants = [place for place in places if place["kind"] == "restaurant"]
        print(f"      중복 제거 후 장소 {len(places)}곳 확인")
        try:
            report_data = request_structured_trip_report(
                client,
                request_analysis,
                recommendation,
                places,
                errors,
                origin=origin,
            )
        except TravelPlannerError as error:
            if not _transient_error_details(error)[1]:
                raise
            errors.append(_final_report_failure_summary(error))
            report = render_fallback_report(
                travel_date,
                recommendation,
                errors,
                request_analysis=request_analysis,
                places=places,
                origin=origin,
            )
        else:
            report = render_request_trip_report(
                travel_date,
                recommendation,
                request_analysis,
                places,
                report_data,
                origin=origin,
            )

    print("[5/5] JSON과 Markdown 결과 저장 및 검증 중...")
    json_path, markdown_path = save_results(
        travel_date,
        recommendation,
        restaurants,
        errors,
        report,
        output_dir,
        request_analysis=request_analysis,
        places=places,
    )
    return {
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
        "request_analysis": request_analysis,
        "places": places,
        "json_path": json_path,
        "markdown_path": markdown_path,
    }


def main(argv=None):
    """CLI를 실행하고 성공 시 0, 실패 시 1을 반환한다."""
    args = create_parser().parse_args(argv)
    try:
        result = run_pipeline(
            args.travel_date,
            origin=args.origin,
            city=args.city,
            user_request=args.user_request,
        )
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
