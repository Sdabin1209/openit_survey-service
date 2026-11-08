"""장애 시험용 일회용 PostgreSQL 컨테이너. 시험이 직접 띄운 컨테이너만 이름으로 다룬다.

왜 이름(실행마다 고유 uuid)인가: 라벨 `io.openit.dev-postgres=1`이나 포트로 '찾으면' 같은 서버의 다른 영역·세션 컨테이너까지
pause할 수 있다(포트는 부분 문자열 일치도 있었다). 직접 띄우고 그 이름으로만 pause/unpause하면 남의 컨테이너에 닿을 길이 없다.
"""
from .subproc_env import min_env
from .timing import HANG_GUARD_SECONDS
import subprocess
import uuid

RUN_LABEL = "io.openit.survey-service.test-run"     # 정리용. 이 값(실행별 uuid)이 같은 컨테이너만 우리 것이다
# 운영 PostgreSQL 16과 같은 주 버전(ADR-0002). 없으면 pull하지 않고 SKIP한다. 기반 이미지는 태그가 아니라 digest로 고정한다(Docker 공식 권고, 공통 규칙 '빌드 때 고정'). 2026-11-05에 이 PC의 같은 태그 RepoDigest를 읽은 값이다. 올리는 법: `docker pull postgres:16` 뒤 `docker image inspect postgres:16 --format '{{index .RepoDigests 0}}'` 값으로 이 한 곳을 바꾼다.
IMAGE = "postgres:16@sha256:1beaa3084eee2676f20a9cd9928f0bf2c5bf674f8e49f81017912183d377c61c"


def _docker(*args, timeout=HANG_GUARD_SECONDS):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, env=min_env())


def docker_ready() -> bool:
    try:
        return _docker("image", "inspect", IMAGE).returncode == 0
    except Exception:
        return False


def run_container(*extra, run_id=None) -> str:
    """고유 이름의 컨테이너를 띄우고 이름을 돌려준다. --rm이라 stop하면 사라진다."""
    run_id = run_id or uuid.uuid4().hex[:12]
    name = f"survey-service-test-{run_id}"
    r = _docker("run", "-d", "--rm", "--name", name, "--label", f"{RUN_LABEL}={run_id}", *extra)
    if r.returncode != 0:
        raise RuntimeError("docker run 실패: " + r.stderr.strip()[:200])
    return name


def start_postgres() -> tuple[str, str]:
    """(컨테이너 이름, 접속 URL). 호스트 포트는 Docker가 고르고 127.0.0.1에만 연다. 일회용이라 trust 인증(비밀 없음)."""
    name = run_container("-p", "127.0.0.1::5432", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=t", IMAGE)
    port = _docker("port", name, "5432/tcp").stdout.strip().splitlines()[0].rsplit(":", 1)[1]
    url = f"postgresql://postgres@127.0.0.1:{port}/t"
    import psycopg

    def ready() -> bool:
        try:
            psycopg.connect(url, autocommit=True).close()
            return True
        except Exception:
            return False
    from .timing import wait_until
    if wait_until(ready):        # 일회용 컨테이너가 접속을 받을 때까지(정상은 수 초, 매달림 감시는 HANG_GUARD_SECONDS)
        return name, url
    stop(name)
    raise RuntimeError("일회용 PostgreSQL이 준비되지 않음")


def stop(name: str):
    _docker("rm", "-f", name)               # 이름이 정확히 우리 컨테이너일 때만 부른다
