"""장애 시험용 일회용 PostgreSQL 컨테이너. 시험이 직접 띄운 컨테이너만 이름으로 다룬다.

왜 이름(실행마다 고유 uuid)인가: 라벨 `io.openit.dev-postgres=1`이나 포트로 '찾으면' 같은 서버의 다른 영역·세션 컨테이너까지
pause할 수 있다(포트는 부분 문자열 일치도 있었다). 직접 띄우고 그 이름으로만 pause/unpause하면 남의 컨테이너에 닿을 길이 없다.
"""
import subprocess
import time
import uuid

RUN_LABEL = "io.openit.survey-service.test-run"     # 정리용. 이 값(실행별 uuid)이 같은 컨테이너만 우리 것이다
IMAGE = "postgres:16"                               # 운영 PostgreSQL 16과 같은 주 버전(ADR-0002). 없으면 pull하지 않고 SKIP한다


def _docker(*args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def docker_ready() -> bool:
    try:
        return _docker("image", "inspect", IMAGE, timeout=20).returncode == 0
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
    end = time.monotonic() + 40
    while time.monotonic() < end:
        try:
            psycopg.connect(url, connect_timeout=2, autocommit=True).close()
            return name, url
        except Exception:
            time.sleep(0.5)
    stop(name)
    raise RuntimeError("일회용 PostgreSQL이 40초 안에 준비되지 않음")


def state(name: str) -> str:
    """running / paused / (없으면 gone)."""
    r = _docker("inspect", "-f", "{{.State.Status}}", name, timeout=20)
    return r.stdout.strip() if r.returncode == 0 else "gone"


def pause(name: str):
    _docker("pause", name, timeout=20).check_returncode()


def unpause(name: str):
    _docker("unpause", name, timeout=20)


def stop(name: str):
    _docker("rm", "-f", name, timeout=60)               # 이름이 정확히 우리 컨테이너일 때만 부른다
