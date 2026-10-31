# 시험의 시간 처리 (U422)

시험 중 실제 시간을 기다리거나 재서 판정하는 곳은 없다. 처리 기준은 `docs/adr/ADR-0050.md`.
숫자 timeout은 `tests/timing.py`의 `HANG_GUARD_SECONDS`(매달림 감시), 반응 간격 `POLL_SECONDS`, 그리고 가짜 시계 안의 값(예: `load_timeout=30`을 `FakeClock`과 함께 줄 때는 실제 시간이 아니다)뿐이다.

| 파일 | 시험(무엇) | 목적(요구사항) | 처리 | 바뀐 시험 |
|---|---|---|---|---|
| test_fault_a6_a8_a10 | ingest_stuck_or_broken_keeps_rows_pending | 수집이 실패해도 행은 PENDING으로 남고 턴은 영향이 없다 | 3(시간 단언 삭제) | 같은 이름(hang 모드는 아래) |
| test_fault_a6_a8_a10 | (hang 모드: 응답 없는 수집) | 수집 시간 초과 때 보낸 것으로 치지 않는다 | 2(가짜 수집기가 즉시 UNAVAILABLE 시간 초과를 던짐) | test_ingest_timeout_keeps_rows_pending_and_never_fails_turns |
| test_fault_a6_a8_a10 | SSE 하트비트·구독 정리 대기 | 신호가 올 때까지 | 매달림 감시(wait_until) | test_dropped_sse_clients..., test_named_heartbeat... |
| test_graph | test_graph_overhead_is_small | (속도 측정: 정확성 요구 없음) | 지움(3) | - |
| test_http_server_properties | idle_partial_connections_do_not_block | waitress가 반쯤 열린 연결을 처리하는 방식 | 지움(2, 라이브러리) | test_waitress_gets_the_connection_and_timeout_values_from_settings (channel_timeout·connection_limit·threads를 설정에서 넘기는지) |
| test_http_server_properties | in_flight_request_gets_its_response | 종료를 요청해도 처리 중 요청이 응답을 받는다 | 사건(Event로 요청을 붙잡고 종료 요청 뒤 풀음) | 같은 이름 |
| test_audit_ssrf_and_input | connection_limit_holds... | waitress 접속 상한·유휴 시한 | 지움(2, 라이브러리) | 위 설정 값 확인이 대신한다 |
| test_audit_ssrf_and_input | webpush 발송 시한 | 리다이렉트를 따르지 않는다(시한은 주제 아님) | 시한 없음(None) 주입 | test_sender_does_not_follow_redirects |
| test_staff_alerts | webpush 410 흔들림 | 410이면 GONE | 시한 없음(None) 주입 | test_webpush_sender_against_local_push_service |
| test_inference_client | slow_extract_beyond_timeout / slow_respond | 시간 초과 시 단계 실패·재시도 없음·커밋 뒤 실패 상태 유지 | 2(b: 클라이언트가 즉시 던지는 시간 초과 오류) | test_extract_timeout_fails_the_step_without_retry, test_respond_timeout_fails_after_commit_state_is_kept |
| test_inference_client | server_down_fails_fast_after_one_retry | 연결 거부면 한 번 재시도 뒤 단계 실패 | 3(시간 단언 삭제) | 같은 이름 |
| test_inference_client | other_sessions_are_not_blocked | 느린 추론이 다른 세션을 막지 않는다 | 사건(서버가 응답을 붙잡은 채 네 호출이 모두 도착) | 같은 이름 |
| test_inference_client | 적재 지연 시험 5곳(load_delay) | 적재가 LOADING이면 기다리거나 재시도로 처리 | 시간 대신 질의 횟수(load_polls)·게이트 Event, 가짜 시계 | test_async_load_is_polled_until_loaded 외 |
| test_inference_time_budget | extract 3.0초·respond 5.0초에 끊긴다, 두 호출 합이 마감 안 | 시한 값이 맞다 | 지움(2, 소켓 시한은 라이브러리) | test_default_settings_carry_the_contract_budget, test_the_app_passes_the_per_call_limits_from_settings_to_the_inference_client |
| test_inference_time_budget | 재시도 예산 2개 | 남은 예산 >= 호출 상한일 때만 재시도 | 1(주입한 시계) | test_no_retry_when_remaining..., test_retry_happens_when... |
| test_public_api, test_public_api_v132 | 429 동시 처리 상한 | 상한을 넘으면 429 | 매달림 감시 | 같은 이름 |
| test_readyz_db_outage | unreachable_then_recovers | DB 연결 거부면 503, 돌아오면 200 | 3 + 2(즉시 거부하는 닫힌 포트) | test_readyz_503_when_db_unreachable_then_recovers |
| test_readyz_db_outage | does_not_wait_for_write_path_lock | /readyz는 쓰기 경로 락을 기다리지 않는다 | 사건(락을 쥔 채 돌아온다) | 같은 이름 |
| test_readyz_db_outage | hard_cap_when_a_check_hangs | 확인이 영영 안 끝나도 마감 뒤 503 | 1(주입한 시계) | 같은 이름 |
| test_readyz_db_outage | turn_ends_not_committed_when_db_is_frozen | docker pause된 DB에서 턴이 NOT_COMMITTED | 지움(2, PostgreSQL 시한) | test_db_timeouts_are_passed_from_settings_to_the_connection + test_turn_idempotency_deadline의 마감 시험(주입한 시계) |
| test_readyz_db_outage | pausing_own_container_leaves_others | docker pause 대상 선택 | 지움(이 헬퍼를 쓰는 시험이 없어졌다) | - |
| test_readyz_db_outage | graceful_shutdown | SIGTERM 순서·draining | 2(대부분 waitress) + 1 | test_readyz_reports_shutting_down_while_draining (처리 중 요청 대기는 test_http_server_properties) |
| test_readyz_db_outage | three_hanging_dbs | 모두 멈추면 사유 셋 | 1(주입한 시계) | 같은 이름 |
| test_readyz_db_outage | deadline_boundary, measured_worst_case | 마감 경계, 최악 시간 측정 | 지움(3: 측정) | - |
| test_readyz_total_budget | 4개 | 확인·보조 수치가 멈춰도 마감 뒤 사유와 함께 돌아온다 | 1(주입한 시계) | 같은 이름 |
| test_risk_precision | match_is_linear | (속도 측정) | 3(결과 단언만 남김) | test_match_is_linear_in_the_number_of_hits |
| test_trace_export | turn_does_not_wait_for_ingest | 턴이 수집을 호출하지 않는다 | 3(시간 단언 삭제) | 같은 이름 |
| test_trace_export | background_thread_delivers | 백그라운드 전송 | 조건 대기 + 주입 시계 | 같은 이름 |
| test_turn_db_outage_bounds | connection_refused_ends_fast | 연결 거부면 즉시 503 UNAVAILABLE retryable | 3(재접속 유예 0, 시간 단언 삭제) | 같은 이름 |
| test_turn_db_outage_bounds | frozen_db_ends_within_turn_deadline | docker pause | 지움(2) | - |
| test_turn_idempotency_deadline | concurrent_same_turn_id_*, many_concurrent_* | 같은 turn_id 동시성 | 사건(Event, 나머지 응답 수 대기) | 같은 이름 |
| test_turn_idempotency_deadline | deadline_slow_model / deadline_slow_store_load | 마감을 넘기면 NOT_COMMITTED, 늦은 작업은 확정하지 않는다 | 1(주입한 시계) | 같은 이름 |
| test_risk_failsafe, test_audit_fixes | 마감 초과·느린 DB에서도 알림이 먼저 나간다 | 알림은 선별에서 먼저 | 1(주입한 시계) | test_alert_goes_out_even_when_the_turn_deadline_is_exceeded, 위험 선별 시험 |
| test_loader_retry | 적재 지연·재시도 | 재시도 간격은 이미 주입 시계 | 시간 대신 횟수·게이트 | 5곳 |
| test_service, test_public_api, test_readyz_reask_effective, test_http_integration, test_loader_retry | 준비·수신을 기다리는 반복문 | 신호 | 매달림 감시(wait_until) | - |
| pg_container, pg_support | 컨테이너 준비·세션 종료 대기 | 시험 도구 | 매달림 감시(wait_until·pg_stat_activity) | - |
| test_audit_fixes, test_http_integration, test_results_signals_retention | 시각 값(time.time) | 데이터 | 고정 상수(주입 시계 시작값) | - |
| test_orchestrator | same_session_turns_are_serialized | 같은 세션 턴 직렬화 | 사건(첫 호출을 붙잡고 나머지가 모인 뒤 풀음). 거짓 통과만 가능(주석) | 같은 이름 |
| test_results_commit_order_u26 | postgres_sequence_order | 번호 순서 = 커밋 순서 | 사건(B가 끝나지 않았음을 즉시 확인). 거짓 통과만 가능 | 같은 이름 |
| test_setup_env_report | subprocess timeout | 도구 | 매달림 감시 | - |

## 영역 시험에서 지운 것 중 smoke·E2E가 봐야 할 것 (헤드 판단)
- 실제 waitress가 반쯤 열린 연결·유휴 연결을 정해진 시각에 정리하는가, 접속 상한을 넘으면 새 연결을 받지 않는가.
- PostgreSQL이 얼었을 때(docker pause) 턴이 마감 안에 NOT_COMMITTED로 끝나고 풀린 뒤 복구되는가, 접속·문장 시한이 실제로 자르는가.
- 추론 서버가 실제로 3.0초·5.0초에 읽기를 끊는가.
