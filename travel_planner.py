"""CLI entry point for the domestic travel planner."""

import argparse
import json
import os
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from google.genai import types


RESPONSE_FIELDS = {
	"recommended_city",
	"weather",
	"events",
	"reason",
}


def validate_date(date_text):
	"""Return a valid date string in YYYY-MM-DD format."""
	try:
		parsed_date = datetime.strptime(date_text, "%Y-%m-%d")
	except ValueError as error:
		raise argparse.ArgumentTypeError(
			"date must be a valid date in YYYY-MM-DD format"
		) from error

	return parsed_date.strftime("%Y-%m-%d")


def create_parser():
	parser = argparse.ArgumentParser(
		description="Get a domestic travel recommendation for a date."
	)
	parser.add_argument(
		"-date",
		"--date",
		dest="travel_date",
		required=True,
		type=validate_date,
		help="Travel date in YYYY-MM-DD format.",
	)
	return parser


def request_recommendation(travel_date):
	load_dotenv()
	if not os.getenv("GEMINI_API_KEY"):
		raise RuntimeError("GEMINI_API_KEY is not configured in .env")

	client = genai.Client(http_options=types.HttpOptions(timeout=60000))
	response_schema = {
		"type": "OBJECT",
		"properties": {
			"recommended_city": {"type": "STRING"},
			"weather": {"type": "STRING"},
			"events": {"type": "ARRAY", "items": {"type": "STRING"}},
			"reason": {"type": "STRING"},
		},
		"required": ["recommended_city", "weather", "events", "reason"],
	}
	prompt = (
		f"여행 날짜는 {travel_date}입니다. 해당 시기에 여행하기 좋은 국내 지역 1곳을 "
		"추천하세요. weather는 실시간 예보가 아닌 해당 시기의 일반적인 계절 특성만 "
		"작성하세요. events는 최신 일정이 확실하지 않으면 반드시 '확인 필요'라고 "
		"표현하세요. 반드시 지정된 JSON 구조만 반환하세요."
	)

	print("[1/3] 1차 여행지 추천 생성 중(LLM)...")
	for attempt in range(3):
		try:
			response = client.models.generate_content(
				model="gemini-flash-latest",
				contents=prompt,
				config=types.GenerateContentConfig(
					response_mime_type="application/json",
					response_schema=response_schema,
				),
			)
			break
		except Exception as error:
			if getattr(error, "code", None) not in {500, 502, 503, 504} or attempt == 2:
				raise
	print("[2/3] Gemini 응답 수신 완료...")

	try:
		recommendation = json.loads(response.text)
	except (TypeError, json.JSONDecodeError) as error:
		raise RuntimeError("Gemini 응답을 JSON으로 해석하지 못했습니다.") from error

	if set(recommendation) != RESPONSE_FIELDS:
		raise RuntimeError("Gemini 응답에 필요한 JSON 필드가 없습니다.")
	if not isinstance(recommendation["events"], list):
		raise RuntimeError("Gemini 응답의 events 필드 형식이 올바르지 않습니다.")

	print("[3/3] JSON 필드 검증 완료...")
	return recommendation


def main():
	args = create_parser().parse_args()
	try:
		recommendation = request_recommendation(args.travel_date)
	except Exception:
		print("Gemini 여행지 추천 요청에 실패했습니다. 설정과 네트워크를 확인하세요.")
		return 1

	print(f"추천 지역: {recommendation['recommended_city']}")
	print(json.dumps(recommendation, ensure_ascii=False, indent=2))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())