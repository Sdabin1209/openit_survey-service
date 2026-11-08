"""min_env 가 Docker 연결 이름을 전달하고 그 밖의 이름은 넘기지 않는지(RUNTIME R9.1, codex C2 검토 2026-11-09). 값은 가짜다."""
from .subproc_env import min_env

DOCKER_NAMES = ("DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "SSH_AUTH_SOCK")


def test_docker_connection_names_are_passed_and_others_are_not(monkeypatch):
    for n in DOCKER_NAMES:
        monkeypatch.setenv(n, "fake-" + n.lower())
    monkeypatch.setenv("VIRTUAL_ENV", "fake")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "fake")
    env = min_env()
    assert all(env[n] == "fake-" + n.lower() for n in DOCKER_NAMES)
    assert "VIRTUAL_ENV" not in env and "SOME_SECRET_TOKEN" not in env
