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
