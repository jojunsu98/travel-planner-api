# 프로젝트

`travel-planner-api`

# 과제 목적

사용자가 입력한 날짜를 바탕으로 Gemini가 국내 여행지를 추천하고, 추천 지역의 Kakao Local 맛집 데이터를 다시 Gemini가 종합해 JSON과 Markdown 여행 리포트를 만드는 Python API 활용 과제입니다.

# 전체 흐름

```text
날짜 CLI 입력 및 검증
→ Gemini Structured Output 여행지 추천
→ recommended_city 추출
→ Kakao Local 맛집 최대 5곳 검색
→ 추천 JSON과 맛집을 Gemini가 종합
→ results/에 Raw JSON과 최종 Markdown 저장
→ 파일 재읽기 검증
```

# 완료 기능

- `-date`, `--date` 양쪽 CLI 옵션
- 실제 존재하는 `YYYY-MM-DD` 날짜 검증
- `.env`의 `GEMINI_API_KEY`, `KAKAO_REST_API_KEY` 사용
- Gemini 1차 추천 Structured JSON 생성·파싱·필드 및 타입 검증
- `recommended_city`를 Kakao 키워드 `{도시} 맛집`에 연결
- Kakao 장소 최대 5곳의 이름·주소·카테고리·URL·좌표 정리
- Kakao 부가 데이터 실패 시 빈 목록과 `errors`로 계속 진행
- Gemini가 두 API 데이터를 종합한 필수 7개 섹션 Markdown 생성
- `results/travel_result_날짜.json`, `results/travel_report_날짜.md` 저장
- 저장 직후 UTF-8 파일 재읽기 검증
- 사용자에게 traceback을 노출하지 않는 오류 처리
- 25개 네트워크 독립 자동 테스트

# 실제 검증 완료 항목

검증일: 2026-08-30

- Python 3.12.13 프로젝트 전용 `.venv` 구성
- `google-genai`, `requests`, `python-dotenv` 설치 확인
- `python -m unittest -v`: 25개 모두 PASS
- 잘못된 날짜 `2026-02-30`: 종료 코드 2, traceback 없음
- Gemini 모델 목록 API: 인증 HTTP 200
- Gemini 실제 텍스트 생성: 명시 모델에서 성공
- Gemini 실제 Structured JSON: 성공, 추천 지역 `경주`
- Kakao Local 실제 `경주 맛집`: HTTP 200, 5건
- Gemini 실제 최종 Markdown: 성공
- `python travel_planner.py --date "2026-10-10"`: 종료 코드 0
- 생성된 두 파일을 다시 읽어 내용 검증 완료
- 실제 통합 결과의 `errors`: 빈 목록

## 2026-09-02 최종 재검증

- Python 3.13.15 프로젝트 전용 `.venv` 새로 구성
- `pip check`: 의존성 충돌 없음
- `python -m unittest -v`: 25개 모두 PASS, 종료 코드 0
- `2026-13-40` 입력: API 호출 전에 차단, 종료 코드 2, traceback 없음
- 모듈 import: 자동 실행 없이 성공
- Gemini·Kakao 실제 통합 실행 1회: 종료 코드 0
- 추천 지역 필드 존재, Kakao 맛집 5건, `errors` 0건
- JSON과 Markdown UTF-8 재읽기 및 SHA-256 산출 성공
- 실제 API 키 2개를 현재 파일·Git 전체 이력·stash와 대조: 발견 0건

# 실패/오류 해결 기록

- 최초 진단용 표준입력 스크립트에서 `load_dotenv()` 자동 경로 탐색이 실패해 프로젝트 `.env` 경로를 명시했습니다. 실제 프로그램도 파일 위치 기준 경로를 사용합니다.
- 현재 `gemini-flash-latest` 모델은 인증과 모델 조회는 성공하지만 생성 요청에서 일시적 503 또는 시간초과가 재현됐습니다.
- 현재 실제 생성이 성공한 `gemini-3.5-flash`를 5xx/네트워크 오류 때 한 번만 사용하는 대체 모델로 추가했습니다. 기본 요청 모델은 과제 지침의 `gemini-flash-latest`를 유지합니다.
- Google 공식 문서는 2026년 6월부터 새 프로젝트에 Interactions API를 권장하지만, 기존 `generateContent` API도 완전 지원한다고 명시합니다. 현재 구현은 실제 통합 실행과 25개 회귀 테스트를 통과하므로 `client.models.generate_content` 방식을 유지했습니다.
- 공식 모델 문서에서 `gemini-flash-latest`의 현재 대상인 `gemini-3.5-flash`와 명시 대체 모델 `gemini-3.5-flash`의 지원 및 Structured Output 기능을 확인했습니다.
- Kakao 공식 문서에서 `GET https://dapi.kakao.com/v2/local/search/keyword.json`과 `Authorization: KakaoAK ${REST_API_KEY}` 방식을 다시 확인했습니다.
- Kakao API의 과거 403 기록은 이번 PC 검증에서는 재현되지 않았고 현재 HTTP 200으로 정상입니다.
- API 키 변수명은 최종적으로 `GEMINI_API_KEY`, `KAKAO_REST_API_KEY` 두 이름으로 통일했습니다.

# 현재 API 상태

- Gemini API 키: 설정됨, 인증 성공
- Gemini 기본 모델: 사용 가능 목록 확인, 생성 요청은 현재 일시적 503/시간초과
- Gemini 대체 모델: 실제 생성 성공
- Kakao REST API 키: 설정됨, 실제 Local 검색 HTTP 200
- 실제 키 값은 이 문서와 Git 파일 어디에도 기록하지 않음

# 공식 문서 확인

확인일: 2026-09-02

- Gemini Interactions API: https://ai.google.dev/gemini-api/docs/interactions-overview
- Gemini 모델 목록: https://ai.google.dev/gemini-api/docs/models
- Gemini Structured Output: https://ai.google.dev/gemini-api/docs/structured-output
- Kakao Local API: https://developers.kakao.com/docs/latest/ko/local/dev-guide

# 생성된 결과 파일

- `results/travel_result_2026-10-10.json`
- `results/travel_report_2026-10-10.md`

실제 결과는 경주 추천, Kakao 맛집 5곳, 오류 0건을 포함합니다.

# Git/GitHub 상태

- 저장소: `https://github.com/jojunsu98/travel-planner-api.git`
- 기본 브랜치: `main`
- 기존 3개 커밋 이력 보존
- force push, hard reset, clean 명령 미사용
- 최종 검증 후 논리적 단위 커밋을 일반 push로 `origin/main`에 반영하는 정책
- `.env`, `.venv`, `__pycache__`는 Git 제외

# 다음 작업

**필수 과제 구현 완료**

선택 개선 사항은 실제 날씨 API 추가, 여러 후보 도시 비교, 여행 일수별 일정 확장입니다. 현재 과제 필수 범위에는 필요하지 않습니다.
