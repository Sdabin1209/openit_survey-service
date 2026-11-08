"""RUNTIME R10.1 + 계약 DATA_OWNERSHIP 백업 행: 실제 restic + 표준 restic rest-server(컨테이너)로 백업 -> 스냅숏 -> 복구 잡 -> 복구한 DB에서 행 확인.
이 시험이 띄우는 것: rest-server 컨테이너 하나(이름 survey-service-restsrv-<무작위>)와 restic 바이너리를 꺼내려고 잠깐 만드는 컨테이너 하나. 둘 다 이 시험이 지운다.
PostgreSQL(TEST_DATABASE_URL)과 docker가 없으면 건너뛴다(통과로 세지 않는다)."""
from .subproc_env import min_env
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid

import pytest

from survey_service.components import link_backup as bk
from survey_service.components.session_store import PgSessionStore

from .pg_support import pg_url
from .timing import HANG_GUARD_SECONDS, fake_clock, wait_until

USER, PW = "backupuser", "pw-restic-canary-survey"


# 시험이 쓰는 외부 이미지는 :latest가 아니라 digest로 고정한다(태그는 옮겨 가 시험 결과가 날마다 달라질 수 있다). 2026-11-05에 이 PC에 있는 같은 태그의 RepoDigest를 읽은 값.
# 올리는 법: `docker pull <이름>:latest` 뒤 `docker image inspect <이름>:latest --format '{{index .RepoDigests 0}}'` 값으로 아래 두 상수를 바꾸고 이 시험을 다시 돌린다.
RESTIC_IMAGE = "restic/restic@sha256:9940e6c7421ab3a21ee9fdc4e91596d04c792b600d31b102696c75cbf3ac4481"          # 이미지 생성 2026-07-05
REST_SERVER_IMAGE = "restic/rest-server@sha256:d2aff06f47eb38637dff580c3e6bce4af98f386c396a25d32eb6727ec96214a5"  # 이미지 생성 2025-05-31


def _docker(*a, check=True):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check, timeout=HANG_GUARD_SECONDS, env=min_env())


@pytest.fixture(scope="module")
def restic_dir(tmp_path_factory):
    if shutil.which("docker") is None or _docker("info", check=False).returncode != 0:
        pytest.skip("docker 없음")
    d = tmp_path_factory.mktemp("restic")
    name = f"survey-service-restic-extract-{uuid.uuid4().hex[:8]}"
    _docker("create", "--name", name, RESTIC_IMAGE)
    try:
        _docker("cp", f"{name}:/usr/bin/restic", str(d / "restic"))
    finally:
        _docker("rm", "-f", name, check=False)
    (d / "restic").chmod(0o755)
    return str(d)


@pytest.fixture
def rest_url(tmp_path):
    data = tmp_path / "repo"
    data.mkdir(); data.chmod(0o777)
    (data / ".htpasswd").write_text(f"{USER}:{{SHA}}{base64.b64encode(hashlib.sha1(PW.encode()).digest()).decode()}\n")      # rest-server의 htpasswd 인증(append-only를 쓰지 않는다: forget --prune이 필요하다)
    (data / ".htpasswd").chmod(0o644)
    name = f"survey-service-restsrv-{uuid.uuid4().hex[:8]}"
    _docker("run", "-d", "--name", name, "-p", "127.0.0.1::8000", "-v", f"{data}:/data", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "rest-server", REST_SERVER_IMAGE, "--path", "/data")
    try:
        assert wait_until(lambda: "Authentication enabled" in _docker("logs", name, check=False).stdout + _docker("logs", name, check=False).stderr)
        port = _docker("port", name, "8000/tcp").stdout.split(":")[-1].strip()
        yield f"http://127.0.0.1:{port}/survey-service"      # 평문 http는 loopback에서만 허용된다(R1.1)
        _docker("stop", "-t", "2", name, check=False)      # 서버가 멈춘 뒤에 본다(쓰는 중인 파일이 없게)
        foreign = [str(f) for f in [data, *data.rglob("*")] if f.lstat().st_uid != os.getuid()]
        assert foreign == [], f"컨테이너가 다른 사용자(root) 소유 파일을 남겼다: {foreign[:3]}"      # 남기면 pytest가 임시 폴더를 지우지 못한다(PytestWarning rm_rf가 쌓인다)
    finally:
        _docker("rm", "-f", name, check=False)


def _env(tmp_path, restic_dir, rest_url, db_url, name):
    (tmp_path / "cred").write_text(f"{USER}:{PW}"); (tmp_path / "pw").write_text("repo-password-canary")
    (tmp_path / name).write_text(db_url)
    return {"OPENIT_BACKUP_URL": rest_url, "OPENIT_BACKUP_CREDENTIALS_FILE": str(tmp_path / "cred"), "OPENIT_BACKUP_REPO_PASSWORD_FILE": str(tmp_path / "pw"),
            "SURVEY_DATABASE_URL_FILE": str(tmp_path / name), "SURVEY_SESSION_RETENTION_DAYS": "90", "PATH": f"{restic_dir}:/usr/bin:/bin"}


def _doc(sid):
    from survey_service.components.session_store import initial_state
    from .sample_bank import sample_bank
    return initial_state(sid, sample_bank(), "Q1.D01")


def test_backup_then_restore_round_trip_with_real_restic_and_rest_server(request, tmp_path, restic_dir, rest_url, monkeypatch):
    src_url, dst_url = pg_url(request), pg_url(request)
    for k in ("SURVEY_DATABASE_URL", "SURVEY_DATABASE_URL_FILE", "OPENIT_BACKUP_CREDENTIALS", "OPENIT_BACKUP_CREDENTIALS_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    src = PgSessionStore(src_url, fake_clock())
    dst = PgSessionStore(dst_url, fake_clock())       # 마이그레이션이 빈 표를 만든다(복구 대상은 빈 DB)
    try:
        for i in range(3):
            src.create(f"S{i}", "rel-1", _doc(f"S{i}"), ("general-intake", "일반 문진표"))
        before = {sid: src.load(sid)["state"] for sid in ("S0", "S1", "S2")}
        env_src = _env(tmp_path, restic_dir, rest_url, src_url, "src_url")
        out, rc = bk.run(env_src)
        assert rc == 0 and out["status"] == "ok" and len(out["snapshot_id"]) >= 8 and out["bytes"] > 0, out
        out2, rc2 = bk.run(env_src)                    # 다시 실행: 새 스냅숏이 하나 더 생긴다(멱등), forget --prune은 90일 안의 스냅숏을 지우지 않는다
        assert rc2 == 0 and out2["snapshot_id"] != out["snapshot_id"]
        env_dst = _env(tmp_path, restic_dir, rest_url, dst_url, "dst_url")
        res, rcr = bk.restore(env_dst, snapshot="latest")
        assert rcr == 0 and res["status"] == "ok" and res["snapshot_id"] and res["bytes"] >= 3, res
        for sid, doc in before.items():                 # 복구한 DB의 행이 원본과 같다
            assert dst.load(sid)["state"] == doc
        assert dst.question_set_of("S1") == ("general-intake", "일반 문진표") and dst.release_id_of("S2") == "rel-1"
        dst.create("S-new", "rel-1", _doc("S-new"))      # 순번이 이어진다: 새 세션의 update_seq가 복구한 최댓값보다 크다
        seqs = {sid: seq for sid, _st, _ts, seq in dst.list_sessions(limit=50)}
        assert seqs["S-new"] > max(seqs[s] for s in ("S0", "S1", "S2"))
        again, rc3 = bk.restore(env_dst, snapshot="latest")      # 비어 있지 않은 DB에는 복구하지 않는다(재시도해도 같다: 75가 아니라 1)
        assert rc3 == 1 and again["status"] == "failed" and again["error"]["code"] == "FAILED_PRECONDITION" and again["error"]["retryable"] is False
        specific, rc4 = bk.restore(dict(env_dst, SURVEY_DATABASE_URL_FILE=str(_blank_db(request, tmp_path))), snapshot=out["snapshot_id"])
        assert rc4 == 0 and specific["status"] == "ok"      # 스냅숏 ID로도 복구한다
        missing, rc5 = bk.restore(env_dst, snapshot="deadbeefdeadbeef")
        assert rc5 == 75 and missing["error"]["retryable"] is True      # 없는 스냅숏 = 저장소에서 읽지 못함(UNAVAILABLE)
    finally:
        src.close(); dst.close()


def _blank_db(request, tmp_path):
    url = pg_url(request)
    PgSessionStore(url, fake_clock()).close()
    f = tmp_path / "blank_url"
    f.write_text(url)
    return f
