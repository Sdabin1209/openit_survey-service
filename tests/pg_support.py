"""PostgreSQL 시험 보조. TEST_DATABASE_URL이 없으면 SKIP(통과로 세지 않는다). 시험마다 새 스키마(search_path)를 만들어 격리한다."""
import os
import uuid

import pytest


def pg_url(request) -> str:
    base = os.environ.get("TEST_DATABASE_URL")
    if not base:
        pytest.skip("TEST_DATABASE_URL 없음: PostgreSQL 시험 건너뜀")
    import psycopg
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")

    def drop():
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f"DROP SCHEMA {schema} CASCADE")
    request.addfinalizer(drop)
    return base + ("&" if "?" in base else "?") + f"options=-csearch_path%3D{schema}"


def kill_connection(conn) -> None:
    """서버 쪽에서 그 연결(PgConn)의 세션을 끊는다(서버 재시작과 같은 결과)."""
    import time
    import psycopg
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(%s)", (conn.kill_backend_pid(),))
    time.sleep(0.2)


def pg_link() -> dict:
    """PgOutbox·PgAlertStore에 wiring(db.open_stores)이 주입하는 연결 클래스와 드라이버 오류 튜플."""
    from survey_service.components import session_store
    return {"connect": session_store.PgConn, "driver_errors": session_store.DRIVER_ERRORS}


def pg_link() -> dict:
    """PgOutbox·PgAlertStore에 주입하는 접속 수단(조립 계층 wiring/db.py가 하는 일과 같다). 컴포넌트끼리 import하지 않으므로 생성자 인자로 받는다."""
    from survey_service.components import session_store
    return {"connect": session_store.PgConn, "driver_errors": session_store.DRIVER_ERRORS}
