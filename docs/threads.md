# 스레드 시작 지점 목록과 예외 처리 근거 (S6)

날짜 2026-11-03. 목적: `PytestUnhandledThreadExceptionWarning`(스레드가 처리되지 않은 예외로 죽음)을 한 번 보고 출력을 잃었다. 재현으로 닫지 못해, 스레드를 시작하는 모든 곳을 목록으로 만들어 각각 예외가 처리되거나 정상 종료 경로에서 생기지 않음을 코드 근거로 적는다.
수집 명령: `grep -rn "Thread(\|ThreadPoolExecutor\|Timer(" survey_service tests scripts` (ThreadPoolExecutor는 없다). 서버 루프(waitress)는 스레드로 돌리는 곳을 따로 센다.

## 제품 코드 (스레드 시작 14곳 + 타이머 1곳 + 서버 루프)
| # | 위치 | 하는 일 | 예외 처리 근거 |
| --- | --- | --- | --- |
| 1 | `server.py` on_term -> `_close_logged` | SIGTERM에서 우아한 종료 | **고침**: 전에는 `close_server`를 그대로 스레드 대상으로 써 예외가 스레드 예외로 남았다. 이제 `_close_logged`가 `Exception`을 기록하고 삼킨다(주 스레드 finally가 다시 닫는다) |
| 2 | `server.py` 서버 루프 `run_server(httpd)` (주 스레드) | waitress 루프 | **고침**(99b27f8): 다른 스레드의 `close_server`가 소켓을 닫으면 select가 EBADF(OSError)로 깨어난다. 전에는 `httpd.run()`이 그대로 올려 SIGTERM 정상 종료가 추적 로그와 종료 코드 1로 끝났다. 이제 닫기 요청(`_survey_closed`) 뒤의 OSError는 삼키고 그 밖은 올린다 |
| 3 | `turn_graph.py` probe (위험 선별의 세션 존재 확인) | 저장소 읽기 | 본문 전체가 `try/except Exception` -> `DB_ERROR` |
| 4 | `config.py` `real_timer` (threading.Timer, 문장 강제 끊기) | `session_store._abort` | `_abort`가 `except Exception: pass`로 감싼다 |
| 5 | `link_trace_export.py` trace-exporter 루프 | outbox 전송 | 루프 안 `flush_once`가 `try/except Exception`(스레드가 죽으면 안 된다 주석) |
| 6 | `orchestrator.py` request-db | DB 문장 하나 | `except BaseException`으로 잡아 호출 스레드에 넘긴다 |
| 7 | `orchestrator.py` turn-<id> | 턴 처리 | `except BaseException`으로 잡아 호출 스레드에 넘긴다 |
| 8 | `alert_service.py` 웹 푸시 일꾼 `one` | 구독자 한 명에게 발송 | **고침**: 발송기가 던진 예외와 `remove_subscription`의 저장소 외 오류(`raise`)가 스레드를 죽일 수 있었다. 이제 `except Exception`으로 FAILED로 세고 기록한다 |
| 9 | `app.py` release-poller | 릴리스 폴링 | 반복 본문이 `try/except Exception`. 루프 대기는 `Event.wait` |
| 10 | `app.py` retention | 보존 정리 | 반복 본문 `try/except Exception` |
| 11 | `app.py` alert-queue | 알림 큐 비우기 | 반복 본문 `try/except Exception` |
| 12 | `app.py` idle-sweeper | 유휴 만료 | 반복 본문 `try/except Exception` |
| 13 | `app.py` `_bounded`의 readyz-<이름> | /readyz 보조 확인 | `run`이 `try/except Exception` |
| 14 | `app.py` `_db_check`의 readyz-<이름> | /readyz DB 확인 | `run`이 `try/except Exception` |
| 15 | `app.py` result-push | 결과 도착 푸시 | `_result_push`가 `try/except Exception` |

waitress의 작업 스레드(요청 처리)는 라이브러리 소유이고, 핸들러의 예외는 앱 계층(`openit_link`)이 500/503 응답으로 바꾼다(`처리 중 예기치 않은 예외` 로그). 스레드를 죽이지 않는다.

## 시험 코드 (30곳)
시험 스레드는 대부분 시험이 `join`하고 결과를 단언한다. 그런 스레드가 예외로 죽으면 그 시험은 단언 실패로도 실패한다(정상 경로에서는 예외가 없다). 정상 종료 경로에서 예외가 날 수 있는 것은 서버 루프뿐이었고 그것을 고쳤다.
| 묶음 | 위치 | 근거 |
| --- | --- | --- |
| 서버 루프 스레드 | `http_server_support.start_server` | **고침**: `run_server`를 대상으로 쓴다(전에는 `h.run`) |
| `serve_forever` 데몬 | `runtime_support.py`, `test_audit_ssrf_and_input.py` x2, `test_fault_a6_a8_a10.py`, `test_staff_alerts.py`(PushService) | 표준 라이브러리 `serve_forever`는 `shutdown()`을 받아 정상 종료한다(`close`가 `shutdown`+`server_close`). 핸들러 예외는 서버의 `handle_error`로 가고 스레드 예외가 아니다 |
| SSE 읽기 | `test_staff_alerts.py` Sse._read | 본문이 `try/except Exception`으로 끝난다 |
| 동시 호출 | `test_store.py`, `test_staff_call*.py`, `test_results_commit_order_u26.py`, `test_orchestrator.py`, `test_inference_client.py`, `test_turn_idempotency_deadline.py`, `test_public_api*.py`, `test_request_deadline.py`, `test_risk_failsafe.py`, `test_readyz_db_outage.py`, `test_audit_fixes.py`, `test_http_server_properties.py` | 모두 호출 결과를 변수에 모아 단언한다. 호출은 위 제품 코드의 예외 처리(턴 `BaseException` 전달, API의 오류 변환)를 거친다 |
| 스크립트 | `scripts/load_test.py`(시험 밖, 수동) | 이 목록의 범위 밖. `httpd.run` 스레드는 같은 경쟁이 가능하나 부하 시험 도구라 `run_server`로 바꾸지 않았다 |

## 증명
- 결정적 시험 `tests/test_thread_exception_s6.py`: select를 가짜로 바꿔 EBADF를 항상 일으키고(타이밍 무관), 별도 pytest 프로세스에서 `-W error::pytest.PytestUnhandledThreadExceptionWarning`로 돌린다. 옛 경로(waitress `run`을 그대로 스레드로)는 경고로 실패하고 `Bad file descriptor`가 출력에 나오며, `run_server` 경로는 통과한다.
- 한계: 처음 본 경고가 이 경쟁이었는지는 출력을 잃어 확증하지 못했다. 위 목록의 다른 곳은 코드 근거로 예외가 처리됨을 적었다. 그 밖의 곳에서 다시 나오면 `-rw`로 전체 메시지를 남겨 이 표에 더한다.
