"""U47(1): 노출 선언 라벨 io.openit.exposure (계약 Exposure, ADR-0041). 정본은 리포 안 deploy-exposure.json이고 시험이 코드의 라우트와 일치를 본다.
deploy/config/Caddyfile의 survey-service 블록과 어긋나면 코드가 정본이다(어긋남은 ADR-0041에 적었다)."""
import json
import re
import subprocess
import shutil
from pathlib import Path

import pytest
from boundary_contracts import validator

from survey_service.api import PublicApi

ROOT = Path(__file__).resolve().parents[1]
TABLE = json.loads((ROOT / "deploy-exposure.json").read_text(encoding="utf-8"))
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _regex(pattern: str):
    return re.compile("^" + re.escape(pattern).replace(r"\*", "[^/]+") + "$")


def _declared(method: str, path: str):
    """표에서 (메서드, 구체 경로)에 맞는 첫 route의 auth.kind. 없으면 None."""
    for r in TABLE["routes"]:
        if any(_regex(p).match(path) for p in r["paths"]) and (not r.get("methods") or method in r["methods"]):
            return r["auth"]["kind"]
    return None


def test_table_is_a_valid_exposure_for_this_service():
    assert list(validator("Exposure").iter_errors(TABLE)) == [] and TABLE["service"] == "survey-service"


def test_every_route_the_code_serves_is_declared_with_the_right_kind():
    # 구체 경로로 푼 코드 라우트(api.py의 _route + service.py의 /healthz·/readyz·SSE). 새 라우트를 더하면 이 목록과 표를 함께 고쳐야 한다(아래 시험이 소스에서 놓친 것을 잡는다).
    served = {
        ("GET", "/healthz"): "none", ("GET", "/readyz"): "none",
        ("POST", "/v1/sessions"): "device-token", ("POST", "/v1/sessions/X/turns"): "device-token", ("POST", "/v1/sessions/X/end"): "device-token",
        ("GET", "/v1/sessions/X/state"): "device-token", ("POST", "/v1/sessions/X/staff-call/retry"): "device-token", ("POST", "/v1/sessions/X/staff-call/report"): "device-token",
        ("POST", "/v1/sessions/X/intervention"): "account",
        ("GET", "/v1/staff/alerts"): "account", ("GET", "/v1/staff/alerts/stream"): "account", ("GET", "/v1/staff/sessions/X"): "account",
        ("GET", "/v1/staff/push-config"): "account", ("POST", "/v1/staff/push-subscriptions"): "account",
        ("POST", "/v1/staff/push-subscriptions/unsubscribe"): "account", ("DELETE", "/v1/staff/push-subscriptions"): "account",
        # 서비스는 결과·위험 신호 경로를 자기 토큰으로 내지만 게이트웨이 밖으로는 내지 않는다
        ("POST", "/v1/sessions/X/risk-signals"): "denied", ("GET", "/v1/results"): "denied", ("GET", "/v1/results/X"): "denied",
    }
    for (method, path), kind in served.items():
        assert _declared(method, path) == kind, (method, path, _declared(method, path))
        if path.startswith("/v1/") and path != "/v1/staff/alerts/stream":
            assert PublicApi._route(method, path) is not None, (method, path)         # 코드가 정말 그 경로를 안다


def test_source_routes_are_all_in_the_served_list():
    """api.py·service.py 소스에서 /v1 경로 리터럴과 정규식 라우트를 읽어 표가 덮는지 본다: 코드에 라우트를 더하고 표를 안 고치면 실패한다."""
    src = (ROOT / "survey_service/api.py").read_text(encoding="utf-8") + (ROOT / "survey_service/service.py").read_text(encoding="utf-8")
    literals = set(re.findall(r'path == "(/v1/[^"]+)"', src))
    patterns = re.findall(r'fullmatch\(r"(/v1/[^"]+)"', src)
    concrete = set(literals)
    for p in patterns:
        p = p.replace("([^/]+)", "X")
        alts = re.search(r"\(([a-z|]+)\)", p)
        for a in (alts.group(1).split("|") if alts else [None]):
            concrete.add(re.sub(r"\([a-z|]+\)", a, p) if a else p)
    assert concrete >= {"/v1/results", "/v1/staff/alerts", "/v1/sessions", "/v1/sessions/X/state", "/v1/sessions/X/staff-call/retry"}      # 소스 읽기가 동작한다
    for path in sorted(concrete):
        assert any(_declared(m, path) is not None for m in METHODS), f"코드 라우트가 노출 표에 없다: {path}"


def test_denied_paths_are_not_exposed_with_any_method():
    for path in ("/v1/sessions/X/risk-signals", "/v1/results", "/v1/results/X"):
        assert {_declared(m, path) for m in METHODS} == {"denied"}


def test_label_is_added_by_build_image_and_matches_the_file():
    if not shutil.which("docker") or not (ROOT / ".venv/bin/python").exists():
        pytest.skip("docker 또는 .venv 없음")
    import os
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="exposure-"))
    tag = f"openit-survey-service-test:exposure-{os.getpid()}"
    try:
        req = subprocess.run(["bash", str(ROOT / "scripts/dev-request.sh"), str(work), tag], capture_output=True, text=True, cwd=ROOT)
        if req.returncode != 0:
            pytest.skip("이웃 소스를 받지 못했다: " + req.stderr[-200:])
        r = subprocess.run(["bash", str(ROOT / "scripts/build-image.sh"), req.stdout.strip()], capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr[-600:]
        result = json.loads(r.stdout.strip().splitlines()[-1])
        assert list(validator("BuildImageResult").iter_errors(result)) == []
        label = result["labels"]["io.openit.exposure"]
        assert json.loads(label) == TABLE and list(validator("Exposure").iter_errors(json.loads(label))) == []
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
