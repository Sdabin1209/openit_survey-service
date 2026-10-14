# 입력 출처와 쓰기 대상 (원본 HealthCheckSchema `work/contracts/`)

헤드 전달(2026-10-15). 구현할 때의 기준표다.

- `bindings.yaml`: 컴포넌트 **입력 필드마다** 원천 계약·필드 경로·읽는 시점(`snapshot`: turn_start / post_store_write / post_turn_write / current_turn)·전달 방식(copy, projection, join, lookup 등)을 정한다. Orchestrator가 입력을 조립할 때의 기준이다.
- `output_bindings.yaml`: 컴포넌트 출력에서 Session Store의 어느 필드로 어떤 방식(write_decisions, write_update, create_record, append_record)으로 반영되는지.
- `components.yaml`: 컴포넌트와 입출력 계약의 대응.

원본 설명대로 이 표는 선언이며 실행 구현이 아니다. `selection_owner: Orchestrator 구현 담당자`로 적힌 선택 정책(예: LoRA-A에 줄 기존 답 후보를 고르는 방식)은 이 영역이 정하고 design.md에 근거를 적는다.
