> **상태: 해소(2026-10-30, link v0.3.0).** 기기 머리글 인증은 `App(authenticate=...)`, `Retry-After`는 `HttpError`, 엄격한 JSON은 라이브러리 기본으로 해결됐다. `link_public_api`는 `openit_link.App` 위로 옮겼다(ADR-0048). 아래는 요청 당시의 기록이다. 계약 `BuildImageRequest.dependency_sources`에 `link` 키가 없는 점은 아직 열려 있다.

# CR-link-001: link의 받는 쪽(openit_link.App)이 survey-service 공개 API를 담기에 부족한 점 (v0.1.0 기준, v0.2.0 갱신 포함)

요청처: 헤드(link 소유). 작성: survey-service 영역 에이전트, 2026-10-30, U400.

## 배경
지시서 "추가(15:58)"는 받는 쪽 link 컴포넌트를 `openit_link.App/Route` 위에 쓰라고 한다. 보내는 쪽(`link_boundary_clients`, `link_inference`)은 `openit_link.clients`로 옮겼다. 받는 쪽 `link_public_api`는 옮기지 못했다. "동작(와이어 형식·HTTP 응답)은 바꾸지 않는다"가 오늘의 조건이고, 아래 항목이 v0.1.0에 없어 옮기면 응답이 달라지거나 기능이 사라진다. 확인한 것은 `link/openit_link/server.py`를 읽은 범위다(실행해 본 것은 아니다).

## 부족한 점
1. **토큰 -> 역할 하나만**: `App(tokens: 토큰->역할)`이다. 이 영역은 기기별 토큰(`device_tokens`, 세션을 만든 기기의 토큰으로만 그 세션에 접근, ADR-0016)과 `X-Openit-Device` 헤더, 키오스크·직원·결과·위험신호 네 역할 토큰을 쓴다. 요청마다 "이 세션의 소유 기기" 같은 상태 기반 인가가 필요하다. 요청: 인증 훅(함수가 Request로 역할/주체를 돌려줌) 또는 Route별 `authorize` 콜백.
2. **스트리밍 응답 없음**: `Response.body`는 dict/list/str/bytes뿐이다. 직원 알림 SSE(`GET /v1/staff/alerts/stream`)는 반복 가능한 본문과 heartbeat, 연결 수 상한, 느린 연결 끊기가 필요하다. 요청: `Response.body`에 bytes 이터레이터 허용.
3. **오류 응답 형식의 예외**: 이 영역은 `Retry-After` 헤더를 503/429에 붙이고, `details`에 영역 필드를 싣는다. `BoundaryError.details` 전달은 되나 헤더 지정 방법이 오류 경로에는 없다(Response로만 가능). 요청: `BoundaryError`와 함께 헤더를 지정하는 방법.
4. **`/readyz` 본문**: 이 영역의 `/readyz`는 의존별 상세(`release_loader`, `reask_limit.effective`, `inference` 등)와 총 마감 안에 모으는 병렬 확인을 한다. `ready()`가 `(ok, dict)`를 돌려주는 형태는 맞지만 응답 스키마(`SurveyReadyz`) 검증이 없다. 요청: ready 응답 스키마 지정.
5. **본문 상한을 앱이 정하는 경로**: 발화 길이 상한(`max_utterance_chars`)처럼 설정으로 정해지는 값은 Route 정의 시점에 알 수 있어 문제 없다. 다만 413 본문 형식이 `{"message":...}`로 `BoundaryError`가 아니다. 이 영역의 현행 413 본문과 같은지 확인이 필요하다(아직 대조하지 않았다).
6. **쿼리 파라미터 검증**: `Request.query`는 문자열만 준다. 목록 조회의 `limit`, `cursor`, `status` 검증은 스키마 대신 코드에 남는다. 요청: Route에 `query_schema`.

## 이 영역이 한 것
- `components/link_boundary_clients.py`, `components/link_inference.py`: 클라이언트를 `openit_link.clients`에서 가져온다.
- `components/link_public_api.py`: 기존 `PublicApi` 유지(HTTP 서버 라이브러리를 직접 쓰지 않는 순수 라우팅). 위 1~6이 해결되면 `openit_link.App` 위로 옮긴다.
- `requirements.txt`, `pyproject.toml`에 `openit-link`(현재 v0.2.0, GitHub 리포에서 git+ssh로 설치)를 고정했다. `scripts/build-image.sh`가 같은 태그를 받아 이미지에 넣는다(요청 계약에 link 키가 없어서; 이 점도 계약 쪽 요청 사항이다: `BuildImageRequest.dependency_sources`에 `link` 키).

## v0.2.0 이후 상태 (2026-10-30 갱신, server.py를 읽은 범위. 옮겨 시험하지는 않았다)
v0.2.0이 해결하는 것: 위 2번(bytes 반복자 스트리밍), 6번(`Route.query_schema`), 1번의 일부(토큰 표 값 `{"role","actor"}` -> `Request.actor`로 기기 토큰의 기기 ID를 줄 수 있다), 토큰 상수 시간 비교.
아직 막는 것(그래서 `link_public_api`는 `openit_link.App` 위로 옮기지 않았다. 옮기면 응답이 달라진다):
1. **토큰 없이 기기 헤더로 인증**: 키오스크 경로는 게이트웨이가 mTLS로 확인한 `X-Openit-Device`가 있으면 토큰 없이 통과한다(ADR-0031). `App`은 역할이 있는 경로에서 토큰을 항상 요구한다. 요청: Route별 "헤더 신원 허용"(예: `device_header=True`).
2. **오류 응답에 임의 헤더와 상태**: 429/503에 `Retry-After`를 붙이고 429(동시 처리 상한)를 낸다. `BoundaryError` 경로에는 헤더를 줄 방법이 없다(`details.http_status`로 상태만 바꿀 수 있다).
3. **본문 해석의 추가 조건**: 중복 키 거부, 짝 없는 서로게이트 거부, 본문이 JSON 객체가 아니면 400. `App`은 `json.loads`만 한다.
4. **상태 기반 인가**: "세션을 만든 기기만 접근" 판정은 저장소를 읽어야 한다(핸들러에 남는다. 이것은 라이브러리 몫이 아니다).
5. 요청 단위 카운터(`/readyz`의 `api`)와 거절 로그는 라이브러리가 낼 수 없다(미들웨어 훅이 있으면 좋다).
