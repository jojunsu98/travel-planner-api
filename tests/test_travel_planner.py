"""travel_planner 모듈의 네트워크 독립 단위 테스트."""

import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

import travel_planner


VALID_RECOMMENDATION = {
    "recommended_city": "경주",
    "weather": "선선하고 일교차가 큰 가을 날씨",
    "events": ["지역 행사 일정 확인 필요"],
    "reason": "역사 유적과 가을 풍경을 함께 즐길 수 있습니다. 도보 여행에도 좋습니다.",
}
VALID_REPORT = "\n\n".join(travel_planner.REQUIRED_REPORT_HEADINGS) + "\n"
VALID_REQUEST_ANALYSIS = {
    "duration": "1박 2일",
    "budget": "약 20만원",
    "preferred_areas": ["여의도", "홍대", "이태원"],
    "activities": ["여의도 둘러보기", "홍대 카페 방문", "이태원 펍 방문"],
    "food_preferences": ["맛집"],
    "cafe_preferences": ["예쁜 카페"],
    "nightlife_preferences": ["펍"],
    "search_queries": [
        {"query": "홍대 전시", "area": "홍대", "category": "activity"}
    ],
}
VALID_REQUEST_REPORT = {
    "getting_there": {
        "mode": "대중교통",
        "duration": "출발지 확인 후 산정",
    },
    "estimated_costs": {
        "round_trip_transport": {"minimum": 0, "maximum": 0},
        "food": {"minimum": 50000, "maximum": 80000},
        "cafe_snack": {"minimum": 20000, "maximum": 40000},
        "attractions": {"minimum": 10000, "maximum": 30000},
        "total": {"minimum": 80000, "maximum": 150000},
    },
    "itinerary": [
        {
            "day": "2일차",
            "time": "저녁",
            "area": "이태원",
            "activity_index": 2,
            "place_ids": ["not-from-kakao"],
        },
        {
            "day": "1일차",
            "time": "오후",
            "area": "여의도",
            "activity_index": 0,
            "place_ids": ["kakao-1"],
        },
        {
            "day": "2일차",
            "time": "오후",
            "area": "홍대",
            "activity_index": 1,
            "place_ids": [],
        },
    ],
}


class FakeApiError(Exception):
    """상태 코드를 제공하는 테스트용 API 예외."""

    def __init__(self, code):
        super().__init__("API error")
        self.code = code


def response_with_text(text):
    return SimpleNamespace(text=text)


class DateAndConfigTests(unittest.TestCase):
    def test_validate_date_accepts_real_date(self):
        self.assertEqual(travel_planner.validate_date("2026-10-10"), "2026-10-10")

    def test_validate_date_rejects_bad_month(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            travel_planner.validate_date("2026-99-99")

    def test_validate_date_rejects_impossible_day(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            travel_planner.validate_date("2026-02-30")

    def test_parser_supports_short_date_option(self):
        args = travel_planner.create_parser().parse_args(["-date", "2026-10-10"])
        self.assertEqual(args.travel_date, "2026-10-10")

    def test_parser_supports_long_date_option(self):
        args = travel_planner.create_parser().parse_args(["--date", "2026-10-10"])
        self.assertEqual(args.travel_date, "2026-10-10")

    def test_parser_accepts_optional_origin(self):
        parser = travel_planner.create_parser()
        without_origin = parser.parse_args(["--date", "2026-10-10"])
        with_origin = parser.parse_args(
            ["--date", "2026-10-10", "--origin", "창원"]
        )
        self.assertIsNone(without_origin.origin)
        self.assertEqual(with_origin.origin, "창원")

    def test_parser_accepts_optional_city(self):
        parser = travel_planner.create_parser()
        without_city = parser.parse_args(["--date", "2026-10-10"])
        with_city = parser.parse_args(
            ["--date", "2026-10-10", "--city", " 서울 "]
        )
        self.assertIsNone(without_city.city)
        self.assertEqual(with_city.city, "서울")

    def test_parser_accepts_optional_request(self):
        parser = travel_planner.create_parser()
        without_request = parser.parse_args(["--date", "2026-10-10"])
        with_request = parser.parse_args(
            ["--date", "2026-10-10", "--request", "1박 2일, 예산 20만원"]
        )
        self.assertIsNone(without_request.user_request)
        self.assertEqual(with_request.user_request, "1박 2일, 예산 20만원")

    def test_parser_rejects_empty_request(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                travel_planner.create_parser().parse_args(
                    ["--date", "2026-10-10", "--request", "   "]
                )

    def test_load_api_keys_can_return_missing_values(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("travel_planner.load_dotenv"),
        ):
            self.assertEqual(travel_planner.load_api_keys("unused"), (None, None))

    def test_missing_gemini_key_is_clear_error(self):
        with self.assertRaisesRegex(travel_planner.TravelPlannerError, "GEMINI_API_KEY"):
            travel_planner.create_gemini_client(None)


class RecommendationTests(unittest.TestCase):
    def test_validate_recommendation_accepts_required_structure(self):
        self.assertEqual(
            travel_planner.validate_recommendation(VALID_RECOMMENDATION),
            VALID_RECOMMENDATION,
        )

    def test_validate_recommendation_rejects_missing_field(self):
        incomplete = dict(VALID_RECOMMENDATION)
        incomplete.pop("reason")
        with self.assertRaises(travel_planner.GeminiResponseError):
            travel_planner.validate_recommendation(incomplete)

    def test_validate_recommendation_rejects_bad_events(self):
        invalid = dict(VALID_RECOMMENDATION, events=[])
        with self.assertRaises(travel_planner.GeminiResponseError):
            travel_planner.validate_recommendation(invalid)

    def test_request_recommendation_parses_structured_json(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(
            json.dumps(VALID_RECOMMENDATION, ensure_ascii=False)
        )
        result = travel_planner.request_recommendation(client, "2026-10-10")
        self.assertEqual(result["recommended_city"], "경주")
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            travel_planner.GEMINI_PRIMARY_MODEL,
        )
        self.assertEqual(travel_planner.GEMINI_PRIMARY_MODEL, "gemini-3.8-flash")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_schema)

    def test_request_recommendation_retries_until_city_matches(self):
        wrong_city = dict(VALID_RECOMMENDATION, recommended_city="부산")
        requested_city = dict(VALID_RECOMMENDATION, recommended_city="서울")
        client = MagicMock()
        client.models.generate_content.side_effect = [
            response_with_text(json.dumps(wrong_city, ensure_ascii=False)),
            response_with_text(json.dumps(requested_city, ensure_ascii=False)),
        ]

        result = travel_planner.request_recommendation(
            client, "2026-10-10", city="서울"
        )

        self.assertEqual(result["recommended_city"], "서울")
        self.assertEqual(client.models.generate_content.call_count, 2)
        for call in client.models.generate_content.call_args_list:
            self.assertIn("사용자가 지정한 '서울'", call.kwargs["contents"])

    def test_gemini_json_parse_error_retries_once(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            response_with_text("not-json"),
            response_with_text(json.dumps(VALID_RECOMMENDATION, ensure_ascii=False)),
        ]
        result = travel_planner.request_recommendation(client, "2026-10-10")
        self.assertEqual(result, VALID_RECOMMENDATION)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_gemini_transient_error_retries_with_fallback_model(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            FakeApiError(503),
            response_with_text(json.dumps(VALID_RECOMMENDATION, ensure_ascii=False)),
        ]
        travel_planner.request_recommendation(client, "2026-10-10")
        second_call = client.models.generate_content.call_args_list[1]
        self.assertEqual(
            second_call.kwargs["model"], travel_planner.GEMINI_FALLBACK_MODEL
        )

    def test_gemini_transient_failure_exposes_only_status_code(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            FakeApiError(503),
            FakeApiError(503),
        ]
        with self.assertRaisesRegex(
            travel_planner.TravelPlannerError, "HTTP 503"
        ) as raised:
            travel_planner.request_recommendation(client, "2026-10-10")
        self.assertNotIn("API error", str(raised.exception))
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_gemini_auth_error_is_not_retried(self):
        client = MagicMock()
        client.models.generate_content.side_effect = FakeApiError(401)
        with self.assertRaisesRegex(travel_planner.TravelPlannerError, "인증"):
            travel_planner.request_recommendation(client, "2026-10-10")
        self.assertEqual(client.models.generate_content.call_count, 1)


class NaturalLanguageRequestTests(unittest.TestCase):
    def test_explicit_fallback_request_analysis_extracts_only_supplied_values(self):
        request_text = (
            "1박 2일, 예산 20만원, 여의도, 홍대, 이태원, 맛집, 카페, 펍"
        )

        analysis = travel_planner.build_fallback_request_analysis(
            request_text, "서울"
        )

        self.assertEqual(analysis["duration"], "1박 2일")
        self.assertEqual(analysis["budget"], "예산 20만원")
        self.assertEqual(analysis["preferred_areas"], ["여의도", "홍대", "이태원"])
        self.assertEqual(analysis["food_preferences"], ["맛집"])
        self.assertEqual(analysis["cafe_preferences"], ["카페"])
        self.assertEqual(analysis["nightlife_preferences"], ["펍"])
        self.assertEqual(
            [item["query"] for item in analysis["search_queries"]],
            [
                "여의도 맛집",
                "여의도 카페",
                "홍대 맛집",
                "홍대 카페",
                "이태원 맛집",
                "이태원 카페",
                "이태원 펍",
            ],
        )

    def test_request_analysis_504_fallback_continues_to_save(self):
        request_text = (
            "1박 2일, 예산 20만원, 여의도, 홍대, 이태원, 맛집, 카페, 펍"
        )
        paths = (Path("result.json"), Path("report.md"))
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", "kakao")),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch("travel_planner.request_recommendation") as recommend,
            patch(
                "travel_planner.analyze_travel_request",
                side_effect=travel_planner.TravelPlannerError(
                    "Gemini 여행 요청 분석 실패 (HTTP 504)"
                ),
            ) as analyze,
            patch("travel_planner.search_requested_places", return_value=[]) as search,
            patch(
                "travel_planner.request_structured_trip_report",
                return_value=VALID_REQUEST_REPORT,
            ) as final_report,
            patch(
                "travel_planner.render_request_trip_report",
                return_value="# 서울 fallback",
            ),
            patch("travel_planner.save_results", return_value=paths) as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = travel_planner.run_pipeline(
                "2026-10-07",
                "unused",
                city="서울",
                user_request=request_text,
            )

        recommend.assert_not_called()
        analyze.assert_called_once()
        self.assertEqual(
            [item["query"] for item in search.call_args.args[0]],
            [
                "여의도 맛집",
                "여의도 카페",
                "홍대 맛집",
                "홍대 카페",
                "이태원 맛집",
                "이태원 카페",
                "이태원 펍",
            ],
        )
        final_report.assert_called_once()
        self.assertEqual(save.call_args.kwargs["request_analysis"]["budget"], "예산 20만원")
        self.assertIn("HTTP 504", result["errors"][-1])
        self.assertEqual(result["json_path"], paths[0])
        self.assertEqual(result["markdown_path"], paths[1])

    def test_request_analysis_authentication_error_does_not_fallback(self):
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", "kakao")),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch("travel_planner.request_recommendation") as recommend,
            patch(
                "travel_planner.analyze_travel_request",
                side_effect=travel_planner.TravelPlannerError(
                    "Gemini API 인증에 실패했습니다."
                ),
            ),
            patch("travel_planner.search_requested_places") as search,
            patch("travel_planner.save_results") as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            with self.assertRaisesRegex(travel_planner.TravelPlannerError, "인증"):
                travel_planner.run_pipeline(
                    "2026-10-07",
                    "unused",
                    city="서울",
                    user_request="1박 2일, 예산 20만원, 여의도 맛집",
                )

        recommend.assert_not_called()
        search.assert_not_called()
        save.assert_not_called()

    def test_analyze_request_adds_searches_for_each_preferred_area(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(
            json.dumps(VALID_REQUEST_ANALYSIS, ensure_ascii=False)
        )

        analysis = travel_planner.analyze_travel_request(
            client,
            "1박 2일, 여의도 홍대 이태원, 예쁜 카페와 펍",
            city="서울",
            destination="서울",
        )

        queries = {item["query"] for item in analysis["search_queries"]}
        self.assertIn("여의도 맛집", queries)
        self.assertIn("여의도 예쁜 카페", queries)
        self.assertIn("여의도 펍", queries)
        self.assertIn("홍대 맛집", queries)
        self.assertIn("이태원 맛집", queries)
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_schema)

    def test_search_requested_places_deduplicates_and_continues_after_error(self):
        document = {
            "id": "kakao-1",
            "place_name": "확인된 장소",
            "road_address_name": "서울 주소",
            "category_name": "음식점",
            "place_url": "https://place.map.kakao.com/1",
        }
        success = MagicMock(status_code=200, ok=True)
        success.json.return_value = {"documents": [document]}
        failure = MagicMock(status_code=500, ok=False)
        http_get = MagicMock(side_effect=[success, failure, success])
        queries = [
            {"query": "여의도 맛집", "area": "여의도", "category": "restaurant"},
            {"query": "홍대 예쁜 카페", "area": "홍대", "category": "cafe"},
            {"query": "이태원 카페", "area": "이태원", "category": "cafe"},
        ]
        errors = []

        places = travel_planner.search_requested_places(
            queries, "test-key", errors, http_get=http_get
        )

        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["kinds"], ["restaurant", "cafe"])
        self.assertIn("Kakao API 서버 오류(HTTP 500)", errors[0])
        self.assertEqual(http_get.call_count, 3)

    def test_structured_report_renders_only_kakao_place_ids_in_activity_links(self):
        place = {
            "place_id": "kakao-1",
            "name": "확인된 카카오 장소",
            "address": "서울 주소",
            "category": "카페",
            "phone": "02-1234-5678",
            "url": "https://place.map.kakao.com/1",
            "kind": "cafe",
            "kinds": ["cafe"],
            "areas": ["여의도"],
            "search_queries": ["여의도 예쁜 카페"],
        }
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(
            json.dumps(VALID_REQUEST_REPORT, ensure_ascii=False)
        )

        report_data = travel_planner.request_structured_trip_report(
            client,
            VALID_REQUEST_ANALYSIS,
            dict(VALID_RECOMMENDATION, recommended_city="서울"),
            [place],
            [],
        )
        markdown = travel_planner.render_request_trip_report(
            "2026-10-07",
            dict(VALID_RECOMMENDATION, recommended_city="서울"),
            VALID_REQUEST_ANALYSIS,
            [place],
            report_data,
        )

        self.assertEqual(
            [item["activity_index"] for item in report_data["itinerary"]], [0, 1, 2]
        )
        self.assertEqual(report_data["itinerary"][2]["place_ids"], [])
        self.assertIn("**확인된 카카오 장소**", markdown)
        self.assertIn(
            "[카카오맵에서 보기](https://place.map.kakao.com/1)", markdown
        )
        self.assertIn("카테고리: 카페", markdown)
        self.assertIn("주소: 서울 주소", markdown)
        self.assertIn("전화: 02-1234-5678", markdown)
        self.assertIn("**여행 날짜:** 2026-10-07", markdown)
        self.assertIn("장소명과 지도 링크는 Kakao 실제 검색 결과입니다.", markdown)
        self.assertNotIn("not-from-kakao", markdown)
        self.assertIn("50,000~80,000원", markdown)
        self.assertIn("출발지 미지정으로 산정 불가", markdown)
        self.assertIn("# 2026-10-07 서울 여행 플랜", markdown)
        self.assertIn("### 여의도", markdown)
        self.assertIn("#### 맛집", markdown)
        self.assertIn("#### 카페", markdown)
        self.assertIn("검색 결과 없음", markdown)
        self.assertIn("| 항목 | 예상 비용 |\n| --- | ---: |", markdown)
        self.assertIn("| 식비 | 50,000~80,000원 |", markdown)
        self.assertIn("### Day 1 · 2026-10-07", markdown)
        self.assertIn("### Day 2 · 2026-10-08", markdown)
        for heading in (
            "## 추천 장소",
            "## 여행 요약",
            "### 이태원",
            "#### 활동/펍",
            "## 가는 방법",
            "## 예상 비용",
            "## 일정",
            "## 주의사항",
        ):
            self.assertIn(heading, markdown)

    def test_fallback_report_uses_only_searched_places_and_records_api_error(self):
        place = {
            "place_id": "kakao-1",
            "name": "확인된 Kakao 맛집",
            "address": "서울 주소",
            "category": "음식점",
            "phone": "02-1234-5678",
            "url": "https://place.map.kakao.com/1",
            "kind": "restaurant",
            "kinds": ["restaurant"],
            "areas": ["여의도"],
        }
        errors = ["Gemini 최종 리포트 생성 실패 (HTTP 503); Python fallback 사용"]

        markdown = travel_planner.render_fallback_report(
            "2026-10-07",
            dict(VALID_RECOMMENDATION, recommended_city="서울"),
            errors,
            request_analysis=VALID_REQUEST_ANALYSIS,
            places=[place],
            origin="창원",
        )

        self.assertIn("**확인된 Kakao 맛집**", markdown)
        self.assertIn("[카카오맵에서 보기](https://place.map.kakao.com/1)", markdown)
        self.assertIn("02-1234-5678", markdown)
        self.assertIn("### 여의도", markdown)
        self.assertIn("Gemini 최종 리포트 생성 실패 (HTTP 503)", markdown)
        self.assertNotIn("가짜 상호", markdown)


class KakaoTests(unittest.TestCase):
    def test_missing_kakao_key_returns_empty_list(self):
        errors = []
        result = travel_planner.search_restaurants("경주", None, errors)
        self.assertEqual(result, [])
        self.assertIn("Kakao REST API 키가 설정되지 않음", errors)

    def test_kakao_zero_results_is_recorded(self):
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {"documents": []}
        errors = []
        result = travel_planner.search_restaurants(
            "경주", "test-key", errors, http_get=MagicMock(return_value=response)
        )
        self.assertEqual(result, [])
        self.assertIn("맛집 검색 결과 없음", errors)

    def test_kakao_auth_error_is_nonfatal(self):
        response = MagicMock(status_code=403, ok=False)
        errors = []
        result = travel_planner.search_restaurants(
            "경주", "test-key", errors, http_get=MagicMock(return_value=response)
        )
        self.assertEqual(result, [])
        self.assertIn("Kakao API 인증 실패", errors)

    def test_kakao_network_error_is_nonfatal(self):
        errors = []
        http_get = MagicMock(side_effect=requests.ConnectionError())
        result = travel_planner.search_restaurants(
            "경주", "test-key", errors, http_get=http_get
        )
        self.assertEqual(result, [])
        self.assertIn("Kakao API 네트워크 오류", errors)

    def test_kakao_success_maps_fields_and_limits_five(self):
        documents = []
        for index in range(6):
            documents.append(
                {
                    "place_name": f"맛집 {index}",
                    "road_address_name": f"도로명 {index}",
                    "address_name": f"지번 {index}",
                    "category_name": "음식점 > 한식",
                    "place_url": f"https://place.map.kakao.com/{index}",
                    "x": "127.0",
                    "y": "35.0",
                    "phone": "000-0000",
                }
            )
        response = MagicMock(status_code=200, ok=True)
        response.json.return_value = {"documents": documents}
        errors = []
        result = travel_planner.search_restaurants(
            "경주", "test-key", errors, http_get=MagicMock(return_value=response)
        )
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["address"], "도로명 0")
        self.assertIn("phone", result[0])
        self.assertEqual(errors, [])


class ReportAndPipelineTests(unittest.TestCase):
    def test_transient_error_classification_excludes_authentication_errors(self):
        self.assertEqual(
            travel_planner._transient_error_details(
                travel_planner.TravelPlannerError("HTTP 503")
            ),
            (503, True),
        )
        self.assertEqual(
            travel_planner._transient_error_details(
                travel_planner.TravelPlannerError("HTTP 401")
            ),
            (None, False),
        )
        network_error = travel_planner.TravelPlannerError("temporary network failure")
        try:
            raise requests.Timeout()
        except requests.Timeout as error:
            network_error.__cause__ = error
        self.assertEqual(
            travel_planner._transient_error_details(network_error), (None, True)
        )

    def test_legacy_pipeline_saves_fallback_after_final_report_503(self):
        restaurants = [
            {
                "name": "확인된 Kakao 맛집",
                "address": "서울 주소",
                "category": "음식점",
                "url": "https://place.map.kakao.com/1",
            }
        ]
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", "kakao")),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch("travel_planner.request_recommendation") as recommend,
            patch("travel_planner.search_restaurants", return_value=restaurants) as search,
            patch(
                "travel_planner.request_final_report",
                side_effect=travel_planner.TravelPlannerError(
                    "Gemini 최종 리포트 요청 실패 (HTTP 503)."
                ),
            ),
            patch("travel_planner.save_results", return_value=(Path("result.json"), Path("report.md"))) as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = travel_planner.run_pipeline(
                "2026-10-07", "unused", origin="창원", city="서울"
            )

        self.assertIn("확인된 Kakao 맛집", save.call_args.args[4])
        self.assertIn("HTTP 503", result["errors"][-1])
        self.assertEqual(save.call_args.args[2], restaurants)
        recommend.assert_not_called()
        self.assertEqual(search.call_args.args[0], "서울")
        self.assertEqual(result["recommendation"]["weather"], "확인 필요")
        self.assertEqual(result["recommendation"]["events"], [])

    def test_request_pipeline_saves_fallback_after_structured_report_503(self):
        recommendation = dict(VALID_RECOMMENDATION, recommended_city="서울")
        place = {
            "place_id": "kakao-1",
            "name": "확인된 Kakao 카페",
            "address": "서울 주소",
            "category": "카페",
            "url": "https://place.map.kakao.com/1",
            "kind": "cafe",
            "kinds": ["cafe"],
            "areas": ["여의도"],
            "search_queries": ["여의도 예쁜 카페"],
        }
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", "kakao")),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch("travel_planner.request_recommendation") as recommend,
            patch(
                "travel_planner.analyze_travel_request",
                return_value=VALID_REQUEST_ANALYSIS,
            ),
            patch("travel_planner.search_requested_places", return_value=[place]),
            patch(
                "travel_planner.request_structured_trip_report",
                side_effect=travel_planner.TravelPlannerError(
                    "Gemini 여행 리포트 요청에 실패했습니다 (HTTP 503)."
                ),
            ),
            patch("travel_planner.save_results", return_value=(Path("result.json"), Path("report.md"))) as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = travel_planner.run_pipeline(
                "2026-10-07",
                "unused",
                city="서울",
                user_request="1박 2일, 여의도 카페",
            )

        self.assertIn("확인된 Kakao 카페", save.call_args.args[4])
        self.assertIn("HTTP 503", result["errors"][-1])
        self.assertEqual(save.call_args.kwargs["places"], [place])
        recommend.assert_not_called()
        self.assertEqual(result["recommendation"]["recommended_city"], "서울")

    def test_legacy_pipeline_keeps_authentication_error_as_failure(self):
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", None)),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch("travel_planner.request_recommendation", return_value=VALID_RECOMMENDATION),
            patch("travel_planner.search_restaurants", return_value=[]),
            patch(
                "travel_planner.request_final_report",
                side_effect=travel_planner.TravelPlannerError(
                    "Gemini API 인증에 실패했습니다."
                ),
            ),
            patch("travel_planner.save_results") as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            with self.assertRaisesRegex(travel_planner.TravelPlannerError, "인증"):
                travel_planner.run_pipeline("2026-10-07", "unused")
        save.assert_not_called()

    def test_final_report_accepts_all_required_sections(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(VALID_REPORT)
        result = travel_planner.request_final_report(
            client, "2026-10-10", VALID_RECOMMENDATION, [], []
        )
        self.assertEqual(result, VALID_REPORT)

    def test_final_report_invalid_format_retries_once(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            response_with_text("불완전한 문서"),
            response_with_text(VALID_REPORT),
        ]
        result = travel_planner.request_final_report(
            client, "2026-10-10", VALID_RECOMMENDATION, [], []
        )
        self.assertEqual(result, VALID_REPORT)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_final_report_transient_failure_exposes_only_status_code(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            FakeApiError(503),
            FakeApiError(503),
        ]
        with self.assertRaisesRegex(
            travel_planner.TravelPlannerError, "HTTP 503"
        ) as raised:
            travel_planner.request_final_report(
                client, "2026-10-10", VALID_RECOMMENDATION, [], []
            )
        self.assertNotIn("API error", str(raised.exception))
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_final_report_prompt_marks_empty_restaurants(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(VALID_REPORT)
        travel_planner.request_final_report(
            client, "2026-10-10", VALID_RECOMMENDATION, [], ["맛집 검색 결과 없음"]
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("데이터 없음", prompt)
        self.assertIn('"restaurants": []', prompt)
        self.assertIn("[카카오맵에서 보기](URL)", prompt)
        self.assertIn("출발지를 '미지정'으로 표시", prompt)
        self.assertIn("교통비 제외 범위", prompt)

    def test_final_report_prompt_includes_origin_and_estimate_requirements(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(VALID_REPORT)
        travel_planner.request_final_report(
            client,
            "2026-10-10",
            VALID_RECOMMENDATION,
            [],
            [],
            origin="창원",
            city="경주",
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn('"origin": "창원"', prompt)
        self.assertIn('"destination": "경주"', prompt)
        self.assertIn('"requested_city": "경주"', prompt)
        self.assertIn("사용자가 지정한 '경주'가 목적지입니다", prompt)
        self.assertIn("왕복 교통비", prompt)
        self.assertIn("모든 금액은 1인 기준의 범위", prompt)
        self.assertIn("실시간 교통요금 API를 사용하지", prompt)
        self.assertIn("방문 전 확인", prompt)

    def test_save_results_writes_and_verifies_both_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path, markdown_path = travel_planner.save_results(
                "2026-10-10",
                VALID_RECOMMENDATION,
                [],
                ["맛집 검색 결과 없음"],
                VALID_REPORT,
                temp_dir,
            )
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["recommendation"]["recommended_city"], "경주")
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), VALID_REPORT)

    def test_save_results_includes_request_analysis_and_places_when_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            json_path, _ = travel_planner.save_results(
                "2026-10-10",
                VALID_RECOMMENDATION,
                [],
                [],
                VALID_REPORT,
                temp_dir,
                request_analysis=VALID_REQUEST_ANALYSIS,
                places=[],
            )
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["request_analysis"], VALID_REQUEST_ANALYSIS)
            self.assertEqual(saved["places"], [])

    def test_request_pipeline_connects_analysis_search_and_structured_report(self):
        recommendation = dict(VALID_RECOMMENDATION, recommended_city="서울")
        place = {
            "place_id": "kakao-1",
            "name": "확인된 맛집",
            "kind": "restaurant",
            "kinds": ["restaurant"],
        }
        report_paths = (Path("result.json"), Path("report.md"))
        with (
            patch("travel_planner.load_api_keys", return_value=("gemini", "kakao")),
            patch("travel_planner.create_gemini_client", return_value="client"),
            patch(
                "travel_planner.request_recommendation", return_value=recommendation
            ) as recommend,
            patch(
                "travel_planner.analyze_travel_request",
                return_value=VALID_REQUEST_ANALYSIS,
            ) as analyze,
            patch(
                "travel_planner.search_requested_places", return_value=[place]
            ) as search,
            patch(
                "travel_planner.request_structured_trip_report",
                return_value=VALID_REQUEST_REPORT,
            ) as create_report,
            patch(
                "travel_planner.render_request_trip_report",
                return_value="# 서울 여행",
            ) as render_report,
            patch("travel_planner.save_results", return_value=report_paths) as save,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = travel_planner.run_pipeline(
                "2026-10-07",
                "unused",
                city="서울",
                user_request="1박 2일, 서울 지역별 카페와 맛집",
            )

        recommend.assert_not_called()
        analyze.assert_called_once_with(
            "client",
            "1박 2일, 서울 지역별 카페와 맛집",
            city="서울",
            destination="서울",
        )
        search.assert_called_once_with(
            VALID_REQUEST_ANALYSIS["search_queries"], "kakao", []
        )
        create_report.assert_called_once()
        render_report.assert_called_once()
        self.assertEqual(result["places"], [place])
        self.assertEqual(result["recommendation"]["recommended_city"], "서울")
        self.assertEqual(result["recommendation"]["weather"], "확인 필요")
        self.assertEqual(result["recommendation"]["events"], [])
        save.assert_called_once()
        self.assertEqual(save.call_args.kwargs["request_analysis"], VALID_REQUEST_ANALYSIS)
        self.assertEqual(save.call_args.kwargs["places"], [place])

    @patch("travel_planner.save_results")
    @patch("travel_planner.request_final_report", return_value=VALID_REPORT)
    @patch("travel_planner.search_restaurants", return_value=[])
    @patch("travel_planner.request_recommendation", return_value=VALID_RECOMMENDATION)
    @patch("travel_planner.create_gemini_client", return_value="client")
    @patch("travel_planner.load_api_keys", return_value=("gemini", None))
    def test_full_pipeline_keeps_errors_and_connects_steps(
        self,
        _load_keys,
        _create_client,
        _recommend,
        search,
        final_report,
        save,
    ):
        def add_missing_key_error(_city, _key, errors):
            errors.append("Kakao REST API 키가 설정되지 않음")
            return []

        search.side_effect = add_missing_key_error
        save.return_value = (Path("result.json"), Path("report.md"))
        with patch("sys.stdout", new_callable=io.StringIO):
            result = travel_planner.run_pipeline("2026-10-10", "unused")
        self.assertEqual(result["errors"], ["Kakao REST API 키가 설정되지 않음"])
        _recommend.assert_called_once_with("client", "2026-10-10")
        self.assertEqual(
            search.call_args.args[0], VALID_RECOMMENDATION["recommended_city"]
        )
        final_report.assert_called_once()
        self.assertEqual(final_report.call_args.args[1], "2026-10-10")
        self.assertEqual(final_report.call_args.args[2], VALID_RECOMMENDATION)
        self.assertEqual(final_report.call_args.args[3], [])
        self.assertEqual(
            final_report.call_args.kwargs["city"],
            VALID_RECOMMENDATION["recommended_city"],
        )

    @patch("travel_planner.run_pipeline")
    def test_main_handles_runtime_error_without_traceback(self, run_pipeline):
        run_pipeline.side_effect = travel_planner.TravelPlannerError("설정 오류")
        output = io.StringIO()
        with patch("sys.stdout", output):
            exit_code = travel_planner.main(
                [
                    "--date",
                    "2026-10-10",
                    "--origin",
                    "창원",
                    "--city",
                    "서울",
                    "--request",
                    "1박 2일, 예산 20만원",
                ]
            )
        self.assertEqual(exit_code, 1)
        self.assertIn("오류: 설정 오류", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        run_pipeline.assert_called_once_with(
            "2026-10-10",
            origin="창원",
            city="서울",
            user_request="1박 2일, 예산 20만원",
        )


if __name__ == "__main__":
    unittest.main()
