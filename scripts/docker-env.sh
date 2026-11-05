#!/usr/bin/env bash
# docker를 부르는 모든 스크립트가 source하는 환경 한 곳(시험은 tests/docker_env.py, 같은 규칙). 사용자 전역 ~/.docker 설정(자격 도우미 credsStore)에 기대지 않게 한다.
# 왜: 이 영역이 받는 기반 이미지는 공개 이미지를 digest로 고정한 것이라 레지스트리 자격이 필요 없다. 전역 설정의 자격 도우미가 응답하지 않으면 `docker build`가 "error getting credentials"로 실패한다.
#     표준 환경 변수 DOCKER_CONFIG로 리포 안 빈 설정({})을 주면 도우미를 부르지 않는다(Docker 공식). 호출자가 DOCKER_CONFIG를 이미 주었으면 그대로 쓴다.
# 왜 DOCKER_HOST: DOCKER_CONFIG를 바꾸면 사용자의 docker 컨텍스트(다른 소켓·원격 엔진)도 안 보인다. 기본 소켓이 아닐 때만 그 엔드포인트를 DOCKER_HOST로 넘긴다(컨텍스트는 docker CLI에게 묻고, ~/.docker를 읽지도 고치지도 않는다).
_docker_env_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ -z "${DOCKER_HOST:-}" ]; then
  _docker_env_host=$("${DOCKER:-docker}" context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
  if [ -n "$_docker_env_host" ] && [ "$_docker_env_host" != "unix:///var/run/docker.sock" ]; then export DOCKER_HOST="$_docker_env_host"; fi
fi
if [ -z "${DOCKER_CONFIG:-}" ]; then
  mkdir -p "$_docker_env_root/build/docker-config" && printf '{}\n' > "$_docker_env_root/build/docker-config/config.json"
  export DOCKER_CONFIG="$_docker_env_root/build/docker-config"
fi
unset _docker_env_root _docker_env_host
