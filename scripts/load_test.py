#!/usr/bin/env python3
"""동시 요청 부하 측정(FAULT_MATRIX N2·N3). pytest 수집 대상이 아니다(scripts/ 아래, 이름이 test_로 시작하지 않는다).

PostgreSQL(LOAD_DATABASE_URL)을 붙인 App을 같은 프로세스에서 실제 서버(survey_service.server.serve, waitress)로 띄우고(가짜 모델, 가짜 릴리스 읽기/Trace 수집),
N개 세션이 동시에 발화를 보낼 때의 성공률, 턴 지연 p50/p95/max, 상태 코드별 건수, PostgreSQL 연결 수 최대, 서버 스레드 수·RSS 증가를 표로 낸다.
실제 LLM 지연은 포함되지 않는다(--model-delay로 가짜 모델에 고정 지연을 줄 수 있고, 그 값은 가정이다).

사용: LOAD_DATABASE_URL=<compose.dev.yaml의 PostgreSQL URL, docs/runbook.md> .venv/bin/python scripts/load_test.py [--steps 1,10,50,100] [--turns 3]
      [--max-inflight 32] [--model-delay 0] [--deadline 8]
비밀(접속 문자열)은 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOKEN, STAFF = "load-api-token-0123", "load-staff-token-0123"       # 토큰 최소 16자(ADR-0024)
RUN = format(int(time.time()) % 10**8, "x")       # 실행마다 다른 세션 ID(같은 DB를 다시 써도 멱등 재생으로 빨라지지 않게)


def rss_mb() -> float:
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


def build_app(args, tmp: Path):
    from survey_service.wiring.app import App
    from survey_service.settings import Settings
    from tests.fakes import FakeReleaseReader, FakeTraceIngest
    from tests.sample_bank import sample_bank
    from tests.test_release_loader import INSTALLED
    os.environ["SURVEY_DATABASE_URL"] = os.environ["LOAD_DATABASE_URL"]
    os.environ["SURVEY_API_TOKEN"], os.environ["SURVEY_STAFF_API_TOKEN"] = TOKEN, STAFF
    reader = FakeReleaseReader(tmp / "gate")
    reader.add_release("rel-1", sample_bank(), action_rules_version=INSTALLED["action_rules_version"], model_runtime_version=INSTALLED["model_runtime_version"])
    reader.set_pointer("rel-1")
    env = {"SURVEY_WORK_DIR": str(tmp / "w"), "SURVEY_RELEASE_READER": "x:y", "SURVEY_TRACE_INGEST": "x:y", "SURVEY_MODEL_BACKEND": "fake",
           "SURVEY_SESSION_IDLE_SECONDS": "0", "SURVEY_API_MAX_INFLIGHT": str(args.max_inflight), "SURVEY_TURN_DEADLINE_SECONDS": str(args.deadline)}
    app = App(Settings.from_env(env), reader=reader, ingest=FakeTraceIngest())
    app.start()
    if args.model_delay > 0:                               # 가정된 고정 지연(실제 LLM 지연이 아니다)
        sess = app.registry.active().session
        for name in ("extract", "respond"):
            real = getattr(sess, name)
            setattr(sess, name, (lambda real: lambda req: (time.sleep(args.model_delay), real(req))[1])(real))
    return app


def post(port, path, body):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    t = time.monotonic()
    try:
        c.request("POST", path, json.dumps(body).encode(), {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        return r.status, data, time.monotonic() - t
    except Exception as e:                                 # 연결 거부·시간 초과
        return type(e).__name__, {}, time.monotonic() - t
    finally:
        c.close()


def pg_connections(stop, out):
    import psycopg
    with psycopg.connect(os.environ["LOAD_DATABASE_URL"], autocommit=True) as c:
        while not stop.is_set():
            n = c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()").fetchone()[0]
            out["pg"] = max(out.get("pg", 0), n)
            out["threads"] = max(out.get("threads", 0), threading.active_count())
            out["rss"] = max(out.get("rss", 0.0), rss_mb())
            time.sleep(0.05)


def run_step(app, port, n, turns, tag):
    lat, codes = [], {}
    start_codes = {}
    barrier = threading.Barrier(n)
    lock = threading.Lock()

    def client(i):
        sid = f"L{RUN}-{tag}-{n}-{i}"
        st, _, _ = post(port, "/v1/sessions", {"session_id": sid, "kind": "SESSION_START"})
        with lock:
            start_codes[st] = start_codes.get(st, 0) + 1
        barrier.wait(60)
        for k in range(turns):
            st, body, dt = post(port, f"/v1/sessions/{sid}/turns", {"session_id": sid, "kind": "UTTERANCE", "utterance": "안 피워요", "turn_id": f"u{k}"})
            key = st
            if st == 200 and (body.get("turn_failure_signal") or {}).get("write_committed") == "NOT_COMMITTED":
                key = "NOT_COMMITTED"
            with lock:
                lat.append(dt)
                codes[key] = codes.get(key, 0) + 1
    stats: dict = {}
    stop = threading.Event()
    samp = threading.Thread(target=pg_connections, args=(stop, stats), daemon=True)
    samp.start()
    rss0, thr0 = rss_mb(), threading.active_count()
    t0 = time.monotonic()
    ths = [threading.Thread(target=client, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(300)
    wall = time.monotonic() - t0
    stop.set()
    samp.join(5)
    total = sum(codes.values())
    ok = codes.get(200, 0)
    return {"n": n, "turns": total, "ok": ok, "p50": pct(lat, 50), "p95": pct(lat, 95), "max": max(lat) if lat else float("nan"),
            "codes": codes, "start_codes": start_codes, "pg_conn_max": stats.get("pg"), "threads_max": stats.get("threads"), "thr0": thr0,
            "rss0": rss0, "rss_max": stats.get("rss"), "wall": wall, "tps": total / wall if wall else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="1,10,50,100")
    ap.add_argument("--turns", type=int, default=3, help="세션마다 순차로 보내는 발화 수")
    ap.add_argument("--max-inflight", type=int, default=32, help="SURVEY_API_MAX_INFLIGHT (기본 서비스 값 32)")
    ap.add_argument("--model-delay", type=float, default=0.0, help="가짜 모델 호출마다 고정 지연(초, 가정값)")
    ap.add_argument("--deadline", type=float, default=8.0)
    args = ap.parse_args()
    if not os.environ.get("LOAD_DATABASE_URL"):
        print("LOAD_DATABASE_URL이 필요하다", file=sys.stderr)
        return 2
    import logging
    logging.disable(logging.WARNING)
    from survey_service.server import close_server, serve
    with tempfile.TemporaryDirectory() as td:
        app = build_app(args, Path(td))
        httpd = serve(app, 0)
        port = httpd.effective_port
        threading.Thread(target=httpd.run, daemon=True).start()
        print(f"# max_inflight={args.max_inflight} model_delay={args.model_delay}s deadline={args.deadline}s turns/session={args.turns} cpus={os.cpu_count()}")
        print("| N | 요청 | 200 | p50 s | p95 s | max s | 상태 코드별 | PG 연결 최대 | 스레드 시작→최대 | RSS MB 시작→최대 | 처리량 req/s |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for i, n in enumerate(int(x) for x in args.steps.split(",")):
            r = run_step(app, port, n, args.turns, i)
            codes = ", ".join(f"{k}:{v}" for k, v in sorted(r["codes"].items(), key=lambda kv: str(kv[0])))
            if r["start_codes"] != {200: r["n"]}:
                print(f"  (세션 시작 응답: {r['start_codes']})")
            print(f"| {r['n']} | {r['turns']} | {r['ok']} | {r['p50']:.2f} | {r['p95']:.2f} | {r['max']:.2f} | {codes} | {r['pg_conn_max']} | "
                  f"{r['thr0']}→{r['threads_max']} | {r['rss0']:.0f}→{r['rss_max']:.0f} | {r['tps']:.1f} |", flush=True)
        close_server(httpd, 10.0)
        app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
