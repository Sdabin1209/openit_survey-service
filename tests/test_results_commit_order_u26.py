"""U26 남은 한계: update_seq가 쓰기(번호 받은) 순서이고 커밋 순서가 아니면, PostgreSQL에서 두 연결이 번호를 받고 거꾸로 커밋할 때
이어 읽는 쪽(커서)이 늦게 커밋된 작은 번호를 영구히 건너뛴다. 번호 순서 = 커밋 순서여야 한다 (ADR-0034, 트랜잭션 수준 advisory lock으로 번호 부여를 직렬화).
SQLite는 BEGIN IMMEDIATE가 쓰기 잠금을 커밋까지 쥐므로 번호 순서 = 커밋 순서다(아래 시험이 같은 장면으로 확인)."""
import threading
import time

import pytest

from survey_service.store import effects
from survey_service.store.postgres import PgSessionStore
from survey_service.store.sqlite import SqliteSessionStore

from .pg_support import pg_url
from .sample_bank import sample_bank


class PauseAtCommit:
    """저장소 연결을 감싸 COMMIT 문 앞에서 멈춘다: 번호를 받은 뒤 커밋하기 전의 구간을 시험이 붙잡는다."""

    def __init__(self, inner):
        self.inner, self.reached, self.go = inner, threading.Event(), threading.Event()

    def execute(self, sql, *a, **kw):
        if sql.strip().upper() == "COMMIT":
            self.reached.set()
            assert self.go.wait(20)
        return self.inner.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _seqs(store):
    return {r[0]: r[3] for r in store.list_sessions(limit=100)}


def _scenario(store_a, store_b):
    """A가 번호를 받고 COMMIT 직전에 멈춘 사이 B가 쓰기를 끝내려 한다. 커밋이 끝난 순서와 번호 순서를 돌려준다."""
    for s in (store_a,):
        s.create("SA", "r", effects.initial_state("SA", sample_bank(), "Q1.D01"))
        s.create("SB", "r", effects.initial_state("SB", sample_bank(), "Q1.D01"))
    pause = PauseAtCommit(store_a._db)
    store_a._db = pause
    done = []
    ta = threading.Thread(target=lambda: (store_a.append_dispatch("SA", {"succeeded": True}), done.append("A")))
    tb = threading.Thread(target=lambda: (store_b.append_dispatch("SB", {"succeeded": True}), done.append("B")))
    ta.start()
    assert pause.reached.wait(10)                     # A는 번호를 받았고 아직 커밋하지 않았다
    tb.start()
    tb.join(1.0)                                      # 직렬화되어 있으면 B는 A가 커밋할 때까지 기다린다
    pause.go.set()
    ta.join(20)
    tb.join(20)
    store_a._db = pause.inner
    seq = _seqs(store_b)
    return done, seq


def test_postgres_sequence_order_equals_commit_order(request):
    url = pg_url(request)
    a, b = PgSessionStore(url), PgSessionStore(url)
    try:
        done, seq = _scenario(a, b)
        assert sorted(done) == ["A", "B"]
        order_by_seq = sorted(("SA", "SB"), key=lambda s: seq[s])
        assert [s[1] for s in order_by_seq] == done, (done, seq)          # 번호 순서 = 커밋이 끝난 순서
    finally:
        a.close(); b.close()


def test_sqlite_sequence_order_equals_commit_order(tmp_path):
    path = str(tmp_path / "s.db")
    a, b = SqliteSessionStore(path), SqliteSessionStore(path)             # 같은 파일, 연결 둘
    try:
        done, seq = _scenario(a, b)
        assert sorted(done) == ["A", "B"]
        assert [s[1] for s in sorted(("SA", "SB"), key=lambda s: seq[s])] == done
    finally:
        a.close(); b.close()
