# Python API 국내 여행지 추천 프로그램

> 여행 날짜를 입력하면 Gemini가 국내 여행지를 추천하고, Kakao Local 맛집 데이터를 결합해 JSON과 Markdown 여행 리포트를 만드는 Python CLI 프로그램입니다.

## 1. 프로젝트 개요

이 프로젝트는 코디세이 **Python 응용: API 활용 국내 여행지 추천 프로그램 개발** 과제입니다. 사용자가 입력한 날짜를 검증한 뒤 Gemini의 Structured Output, Kakao Local 키워드 검색, Gemini의 최종 리포트 생성을 하나의 실행 흐름으로 연결합니다.

실시간 날씨 API를 사용하지 않으므로 날씨는 해당 시기의 일반적인 계절 특성으로 안내합니다. 행사 일정이 확실하지 않은 경우에는 `확인 필요`를 표시하도록 프롬프트를 구성했습니다.

## 2. 학습 목표

- `argparse`로 명령행 인자를 받고 실제 날짜를 검증하기
- `.env`로 API 키를 코드와 분리하기
- Gemini Structured Output을 JSON으로 파싱하고 필드·타입 검증하기
- Kakao Local REST API를 호출하고 필요한 장소 정보만 정리하기
- 두 API의 결과를 다음 API 입력으로 연결하기
- 오류를 구분해 핵심 실패는 종료하고 부가 데이터 실패는 안전하게 계속하기
- JSON과 Markdown 파일을 UTF-8로 저장하고 다시 읽어 검증하기
- `unittest`와 mock으로 외부 API에 의존하지 않는 자동 테스트 작성하기

## 3. 사용 기술

- Python 3.10 이상
- Google Gemini API / `google-genai`
- Kakao Local REST API / `requests`
- 환경 변수 / `python-dotenv`
- 테스트 / Python 표준 라이브러리 `unittest`, `unittest.mock`

## 4. 전체 워크플로우

```text
날짜 입력 및 검증
  → Gemini 1차 호출(Structured JSON)
  → recommended_city 추출
  → Kakao Local에서 "{recommended_city} 맛집" 검색(최대 5곳)
  → 추천 JSON + 맛집 데이터를 Gemini에 전달
  → 최종 Markdown 여행 리포트 생성
  → results/에 JSON + Markdown 저장 및 재검증
```

Gemini는 `gemini-3.8-flash`를 우선 사용합니다. 추천 응답은 JSON MIME 모드와 response schema를 사용하는 Structured Output으로 요청하고 Python에서 필드와 타입을 다시 검증합니다. 최종 리포트는 `text/plain`으로 요청합니다. 일시적인 5xx 또는 네트워크 오류가 발생하면 `gemini-3.5-flash`로 한 번 재시도합니다. 401/403 인증 오류는 재시도하지 않습니다.

## 5. 프로젝트 구조

```text
travel-planner-api/
├── .env.example
├── .gitignore
├── PROJECT_STATUS.md
├── README.md
├── requirements.txt
├── travel_planner.py
├── results/
│   ├── .gitkeep
│   ├── travel_result_2026-10-10.json
│   └── travel_report_2026-10-10.md
└── tests/
    ├── __init__.py
    └── test_travel_planner.py
```

로컬의 `.env`와 `.venv`는 Git에 포함되지 않습니다.

## 6. 설치 방법

Windows PowerShell 기준입니다.

```powershell
git clone https://github.com/jojunsu98/travel-planner-api.git
cd travel-planner-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

가상환경 활성화가 제한된 환경에서는 실행 명령의 `python` 대신 `.\.venv\Scripts\python.exe`를 사용할 수 있습니다.

## 7. API Key 설정

프로젝트 루트에서 `.env.example`을 `.env`로 복사하고 실제 키를 입력합니다.

```dotenv
GEMINI_API_KEY=YOUR_KEY_HERE
KAKAO_REST_API_KEY=YOUR_KEY_HERE
```

- Gemini 키는 Google AI Studio에서 발급합니다.
- Kakao Developers에서 REST API 키를 확인하고 Kakao Map 사용 설정을 활성화합니다.
- `.env`는 `.gitignore`에 등록되어 있으며 절대 커밋하지 않습니다.
- 프로그램과 테스트는 API 키 값을 화면에 출력하지 않습니다.

## 8. 실행 방법

`-date`와 `--date`를 모두 지원합니다.

```powershell
python travel_planner.py --date "2026-10-10"
python travel_planner.py -date "2026-10-10"
python travel_planner.py --date "2026-10-07" --city "서울" --origin "창원" --request "1박 2일, 예산 20만원. 여의도, 홍대, 이태원의 예쁜 카페와 맛집을 방문하고 싶어요."
```

입력은 실제 존재하는 `YYYY-MM-DD` 날짜여야 합니다. 예를 들어 `2026-02-30`은 이해 가능한 오류 메시지와 함께 거부되며 traceback을 노출하지 않습니다.

`--origin`, `--city`, `--request`는 선택 옵션입니다. `--request`를 지정하면 여행 조건을 Structured Output으로 분석해 선호 지역·활동별 Kakao 검색을 수행하고, 검색된 Kakao 장소만 이름과 지도 링크로 리포트에 표시합니다. 검색이 없는 카테고리는 결과 없음으로 기록하고 계속 진행합니다.

## 9. Gemini Structured Output

1차 Gemini 응답은 JSON MIME 모드와 schema로 구조를 제한하고, 파싱 후 Python 코드에서 필수 필드와 타입을 다시 검증합니다.

```json
{
  "recommended_city": "국내 지역 1곳",
  "weather": "해당 시기의 일반적인 계절·날씨 특성",
  "events": ["행사 1~3개 또는 일정 확인 필요 표시"],
  "reason": "추천 이유 2~4문장"
}
```

JSON 파싱 또는 필드 검증 실패 시 한 번만 재시도합니다.

## 10. Kakao Local 검색

추천 지역을 이용해 `{recommended_city} 맛집`을 검색하고 최대 5곳을 정리합니다.

- 이름
- 도로명 주소 우선 주소
- 카테고리
- Kakao Map URL
- 좌표 `x`, `y`
- 전화번호(제공될 때만)

Kakao 키 누락, 401/403, 네트워크 오류, 5xx, 0건은 전체 프로그램을 중단하지 않습니다. `restaurants`를 빈 목록으로 유지하고 민감정보 없는 설명을 `errors`에 기록한 뒤 최종 리포트 생성을 계속합니다.

## 11. 결과 파일

입력 날짜가 `2026-10-10`이면 다음 두 파일이 생성됩니다.

```text
results/travel_result_2026-10-10.json
results/travel_report_2026-10-10.md
```

Raw JSON의 기본 구조는 다음과 같습니다.

```json
{
  "recommendation": {},
  "restaurants": [],
  "errors": []
}
```

최종 Markdown에는 추천 지역, 추천 이유, 예상 날씨/계절 정보, 행사/이벤트, 추천 맛집, 오전·점심·오후·저녁의 1일 일정이 포함됩니다. 맛집이 없으면 `데이터 없음`을 표시합니다.

## 12. 오류 처리

- 잘못된 날짜: `argparse` 오류로 안전하게 종료
- Gemini 키 누락: `.env` 설정 방법 안내 후 종료
- Gemini 401/403: 불필요한 재시도 없이 종료
- Gemini JSON 형식 오류 또는 일시적 5xx: 최대 한 번만 재시도
- Kakao 키 누락·인증·네트워크·서버·0건: `errors`에 기록하고 계속 실행
- 예상하지 못한 오류: traceback 대신 안전한 사용자 메시지 출력
- 결과 저장: 저장 직후 파일을 다시 읽어 JSON/Markdown 일치 여부 검증

## 13. 실제 테스트 결과

2026-08-30 최초 통합 검증 후, 2026-09-02에 현재 PC의 프로젝트 전용
Python 3.13.15 가상환경에서 다시 확인했습니다.

자동 테스트:

```powershell
python -m unittest -v
```

- 25개 테스트 모두 통과 (`exit code 0`)
- 정상/잘못된 날짜 및 두 CLI 옵션
- Gemini 키 누락, JSON 파싱 오류, 인증 오류, 5xx 대체 재시도
- Kakao 키 누락, 0건, 인증 오류, 네트워크 오류, 정상 5건 제한
- 최종 Markdown 필수 섹션, JSON/Markdown 저장 및 재검증
- mock 기반 전체 단계 연결과 사용자 오류 출력

실제 통합 실행:

```powershell
python travel_planner.py --date "2026-10-10"
```

- Gemini 실제 Structured JSON 생성 성공
- 추천 지역 `경주` 추출 성공
- Kakao Local HTTP 200, 맛집 5곳 검색 성공
- Gemini 최종 Markdown 생성 성공
- JSON/Markdown 파일 생성 및 재읽기 검증 성공
- 최종 `errors`는 빈 목록

2026-09-02 재검증에서도 같은 명령이 `exit code 0`으로 완료되었고,
추천 지역 필드가 채워진 JSON, Kakao 맛집 5건, 빈 `errors` 목록 및 Markdown
리포트를 UTF-8로 다시 읽어 확인했습니다. 이 재검증은 API 호출을 불필요하게
반복하지 않기 위해 한 번만 수행했습니다.

2026-09-28 새 노트북 환경에서 전체 단위 테스트 27개가 통과했습니다. 과거 실제
End-to-End 성공 기록과 별개로, 이날 Gemini 재검증은 HTTP 503 서버 혼잡으로
완주하지 못했습니다. 임시 JSON MIME 모드 실험에서는 추천과 Kakao 검색 5건까지
성공한 실행이 있었지만 최종 Gemini 생성은 실패했고, 후속 실행도 추천 단계의
503으로 중단됐습니다. 제출용 코드에서는 과제 요구사항에 맞춰 Structured Output
schema를 유지했습니다. 이날 결과 파일은 생성되지 않았으며 기존 `2026-10-10`
결과 파일은 그대로 보존했습니다.

2026-09-02 문서 확인 당시 Google은 새 프로젝트에 Interactions API를 권장하면서도
기존 `generateContent` API를 지원한다고 안내했습니다. 과거 실제 End-to-End
성공과 회귀 테스트가 확인된 `client.models.generate_content` 구현을 유지합니다.
현재 코드의 기본 모델은 `gemini-3.8-flash`이며, 추천에는 Structured Output schema를
사용합니다. 2026-09-28 재검증은 HTTP 503으로 완주하지 못했으며 이후 API 호출은
수행하지 않았습니다. Kakao 호출은 공식 keyword endpoint와 `KakaoAK` 인증 방식을
사용합니다.

## 14. 보안

- 실제 키가 든 `.env`는 Git에서 제외합니다.
- `.env.example`에는 placeholder만 둡니다.
- API 키를 URL, 예외 메시지, 결과 파일, README에 기록하지 않습니다.
- push 전 tracked 파일과 결과 파일에서 키 패턴을 다시 검사합니다.
- HTTP 인증 헤더는 요청을 보낼 때 메모리에서만 구성합니다.

## 15. 배운 점

외부 API를 연결할 때는 성공 경로뿐 아니라 인증 실패, 일시적 서버 오류, 빈 검색 결과, 잘못된 응답 형식을 각각 구분해야 합니다. 특히 핵심 추천을 만드는 Gemini 실패와 부가 맛집 데이터를 가져오는 Kakao 실패의 처리 정책을 다르게 설계하면 일부 서비스가 불안정해도 사용자에게 가능한 결과를 제공할 수 있습니다. 또한 mock 단위 테스트와 실제 API 통합 실행을 분리하면 빠른 회귀 테스트와 현실적인 동작 검증을 함께 확보할 수 있습니다.
