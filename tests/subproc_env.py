"""하위 프로세스에 주는 최소 환경(RUNTIME R9.1: 시험·잡은 상속받은 환경 변수를 통째로 쓰거나 넘기지 않는다). 허용 목록은 head check-container-inputs 와 같은 최소 집합이다.
시험이 실제로 넘겨야 하는 이름은 min_env(NAME=값) 으로 이름을 밝혀 더한다."""
import os

ALLOWED = ("PATH", "HOME", "LANG", "TMPDIR", "DOCKER_CONFIG", "DOCKER_HOST")


def min_env(**extra: str) -> dict:
    env = {k: os.environ[k] for k in ALLOWED if k in os.environ}
    env.update(extra)
    return env
