# L18: 학습 정답 값 대 문진표 칸 정의 (서비스 값 검증)

날짜 2026-11-03. 이 파일은 survey-service 영역의 측정 기록이다(이웃 영역의 사실은 아래 입력 식별자로만 가리킨다).

## 한 일
question-authoring의 `forms/health-screening-v1.txt`(리포 wo-320, 커밋 d7ce64c, 읽기만)를 그 리포 추출기(`extract_textform.run`, 그 리포 venv)로 bank로 만든 뒤, 그 bank의 칸 정의로 학습 데이터셋의 LoRA-A 정답 출력 값 전부를 이 서비스의 값 검증에 통과시켰다.
- 서비스의 값 검증: `field_specs.build_field_specs`(bank -> 계약 QuestionFieldSpec) -> `boundary_contracts.action_rules.apply_value_rules`(자료형, 최솟값, 최댓값, 선택지, 단위 규칙, 계약 3.4.0). 규칙에 걸려 정답을 다시 쓰는 경우도 센다.
- 서비스가 값 규칙 앞에서 보는 둘: 정답의 칸(`문항.칸`)이 문진표에 있는가, 반복 키(`[soju]` 등)가 그 문항의 `repeat_keys`에 있고 반복 키가 없는 문항에는 없는가.
- 도구: `scripts/check_dataset_values.py`(`python scripts/check_dataset_values.py <bank.json> <lora_a_*.jsonl>`), 시험 `tests/test_dataset_values_l18.py`(합성 정답으로 위반을 실제로 잡는지).

## 입력
- 양식: `question-authoring/forms/health-screening-v1.txt` (sha256 앞 16자 b70eee304d2525d2), 추출 상태 PARSED, 플래그 0.
- 추출된 bank: 문진표 ID `health-screening`, 문항 31개, 칸 52개(sha256 앞 16자 e31fc9cc8ca823d2, JSON 직렬화 기준).
- bank 자체 검사: `invalid_field_specs` 0건, `flow_problems` 0건.
- 데이터: `reference/openit-server/data/final/lora_a_{train,val,test}.jsonl` (읽기만).

## 결과
| 파일 | 레코드 | 저장되는 값(save true, 값 있음) |
| --- | --- | --- |
| lora_a_train | 25,700 | 36,794 |
| lora_a_val | 3,221 | 4,449 |
| lora_a_test | 3,249 | 4,804 |

위반: **0건** (알 수 없는 칸 0, 반복 키 오류 0, 자료형 불일치 0, 최솟값 미만 0, 규칙에 걸려 다시 쓰이는 정답 0).

따라서 서비스 결함도, 양식 칸 정의 결함도 이 데이터에서는 찾지 못했다. 고친 것이 없다.

## 한계
- 검사한 것은 위 네 가지 값 규칙이다. 정답의 `action`·`next_item`이 흐름과 맞는지는 이 검사의 범위가 아니다(추론 서버의 계약 규칙 검사, 별도).
- 값이 null이거나 `save`가 false인 항목은 저장되지 않으므로 검사하지 않았다.
- 데이터셋 자신의 `question_fields`(52칸)와 이 bank의 칸 정의를 칸 단위로 대조하는 일은 기존 시험(`tests/test_question_fields.py`, 시험용 문항 세트 기준)이 하며, 이번 bank 기준의 별도 대조는 하지 않았다.
- 재현: question-authoring venv로 양식을 bank JSON으로 낸 뒤(`PYTHONDONTWRITEBYTECODE=1`, 그 리포를 수정하지 않는다) 위 명령을 실행한다.
