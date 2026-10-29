# CR-020: 계약 클라이언트 `HttpModelRuntime.extract`가 `fields`를 넘기게 해 달라 (U32, 2026-10-30)

상태: **반영됨(계약 v2.4.1, boundary-contracts a8851e3).** `HttpModelRuntime.extract(release_id, request, flow=None, fields=None)`. 서비스는 우회 없이 문항 은행에 칸 규격이 있으면 항상 `fields=`로 보낸다. 아래는 당시 요청이다. 계약은 건드리지 않았다(ADR-0033).

## 사실
- 계약 2.4.0은 `ExtractRequest`에 선택 필드 `fields`(`QuestionFieldSpec`)를 더했다(`schemas/ExtractRequest.schema.json`, CHANGELOG 2.4.0).
- 설치된 클라이언트 `boundary_contracts.clients.HttpModelRuntime.extract(release_id, request, flow=None)`는 `fields`를 받는 인자가 없다. 확인: `clients/runtime.py:60`의 시그니처와 `grep fields clients/runtime.py` 결과 없음(boundary-contracts v2.4.0 설치본).
- 그래서 서비스가 칸 규격을 만들어도 서버로 보낼 수 없다. 클라이언트의 비공개 메서드를 쓰거나 본문을 직접 만드는 우회는 하지 않았다.

## 서비스 쪽 준비
- `survey_service/fields.py`가 문항 은행의 칸 정의에서 `QuestionFieldSpec`을 만든다(시험이 학습 데이터셋 question_fields 52칸과 대조).
- `RemoteModelSession.extract(request, deadline, flow, fields)`는 `fn`의 시그니처에 `fields` 인자가 있을 때만 그 값을 넘긴다. 클라이언트가 받는 판이 나오면 코드 변경 없이 동작한다(시험은 `fields`를 받는 대역으로 확인).

## 요청
`HttpModelRuntime.extract(self, release_id, request, flow=None, fields=None)`: `fields`가 있으면 본문 `{"input", "flow", "fields"}`를 `ExtractRequest`로 검증해 보낸다(`flow`가 없고 `fields`만 있으면 오류). 계약 패치 버전(2.4.1)이면 충분하다고 본다.
