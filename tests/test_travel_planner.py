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

    def test_gemini_auth_error_is_not_retried(self):
        client = MagicMock()
        client.models.generate_content.side_effect = FakeApiError(401)
        with self.assertRaisesRegex(travel_planner.TravelPlannerError, "인증"):
            travel_planner.request_recommendation(client, "2026-10-10")
        self.assertEqual(client.models.generate_content.call_count, 1)


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

    def test_final_report_prompt_marks_empty_restaurants(self):
        client = MagicMock()
        client.models.generate_content.return_value = response_with_text(VALID_REPORT)
        travel_planner.request_final_report(
            client, "2026-10-10", VALID_RECOMMENDATION, [], ["맛집 검색 결과 없음"]
        )
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn("데이터 없음", prompt)
        self.assertIn('"restaurants": []', prompt)

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
        final_report.assert_called_once()
        self.assertEqual(final_report.call_args.args[1], "2026-10-10")
        self.assertEqual(final_report.call_args.args[2], VALID_RECOMMENDATION)
        self.assertEqual(final_report.call_args.args[3], [])

    @patch("travel_planner.run_pipeline")
    def test_main_handles_runtime_error_without_traceback(self, run_pipeline):
        run_pipeline.side_effect = travel_planner.TravelPlannerError("설정 오류")
        output = io.StringIO()
        with patch("sys.stdout", output):
            exit_code = travel_planner.main(["--date", "2026-10-10"])
        self.assertEqual(exit_code, 1)
        self.assertIn("오류: 설정 오류", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
