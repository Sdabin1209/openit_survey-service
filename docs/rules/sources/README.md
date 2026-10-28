# 입력 출처와 쓰기 대상 (원본 HealthCheckSchema `work/contracts/`)

헤드 전달(2026-10-15). 구현할 때의 기준표다.

- `output_bindings.yaml`: 컴포넌트 출력에서 Session Store의 어느 필드로 어떤 방식(write_decisions, write_update, create_record, append_record)으로 반영되는지.
- `components.yaml`: 현행 컴포넌트와 입출력 계약의 대응. v1 컴포넌트(파서·정규화기·판정기·기록기·Plan·생성기)와 입력 출처 표 `bindings.yaml`은 지웠다(D99, git 이력에 남는다).

원본 설명대로 이 표는 선언이며 실행 구현이 아니다. `selection_owner: Orchestrator 구현 담당자`로 적힌 선택 정책(예: LoRA-A에 줄 기존 답 후보를 고르는 방식)은 이 영역이 정하고 design.md에 근거를 적는다.
