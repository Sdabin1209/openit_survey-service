"""docker를 부르는 시험이 쓰는 환경 한 곳(스크립트는 scripts/docker-env.sh, 같은 규칙). 사용자 전역 ~/.docker 설정에 기대지 않는다.
왜 / 규칙은 scripts/docker-env.sh 머리말과 같다: DOCKER_CONFIG가 있으면 그대로, 없으면 리포 build/docker-config의 빈 설정({}), 컨텍스트 엔드포인트가 기본 소켓이 아니면 DOCKER_HOST로.
사용자 ~/.docker는 읽지도 고치지도 않는다(컨텍스트는 docker CLI에게 묻는다)."""
import os
import subprocess
from pathlib import Path

DEFAULT_HOST = "unix:///var/run/docker.sock"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "build" / "docker-config"


def docker_env(base=None) -> dict:
    env = dict(os.environ if base is None else base)
    if not env.get("DOCKER_HOST"):
        try:
            host = subprocess.run([env.get("DOCKER", "docker"), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                                  env=env, capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:
            host = ""      # 엔진을 못 물으면 기본 소켓으로 둔다
        if host and host != DEFAULT_HOST:
            env["DOCKER_HOST"] = host
    if not env.get("DOCKER_CONFIG"):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (CONFIG_DIR / "config.json").write_text("{}\n", encoding="utf-8")
        env["DOCKER_CONFIG"] = str(CONFIG_DIR)
    return env
