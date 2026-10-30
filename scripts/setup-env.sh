#!/usr/bin/env bash
# 왜: 영역 환경 준비의 정본은 이 파일 하나다. 새 머신으로 옮길 때 README마다 설치법이 달라 어긋났고,
#     오래된 .venv(옛 계약 버전이 남아 conformance 모듈을 못 찾음)도 있었다. 핀은 requirements.txt/lock이 정본이다.
set -euo pipefail
# 계약 boundary-contracts 2.6.0 SetupEnvReport: 표준 출력 마지막 줄에 결과 JSON 한 줄을 낸다. 진행 기록(pip 출력 포함)은 모두 표준 오류로 보낸다(ADR-0039).
START=$SECONDS
exec 3>&1 1>&2      # 이후 표준 출력은 표준 오류로, 보고만 3번(원래 표준 출력)으로 낸다

# 어느 폴더에서 실행해도 이 리포 루트 기준으로 동작한다(스크립트 위치 기준).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

report() {   # report <종료 코드>: 종료할 때 한 번만 낸다(성공·실패 모두)
  local rc=$1 status=ok
  [ "$rc" = 0 ] || status=fail
  printf '{"area":"survey-service","status":"%s","exit_code":%d,"seconds":%d,"environment":{"kind":"python-venv","path":".venv","test_command":[".venv/bin/python","-m","pytest"]}}\n' \
    "$status" "$rc" "$((SECONDS - START))" >&3
}
trap 'report $?' EXIT
fail() { echo "setup-env 실패: $*" >&2; exit 1; }

command -v python3 >/dev/null || fail "python3가 없다(3.10 이상 필요)"
[ -f requirements.txt ] && [ -f requirements.lock ] || fail "requirements.txt 또는 requirements.lock이 없다"

if [ ! -x .venv/bin/python ]; then
  echo "[setup-env] .venv 생성"
  python3 -m venv .venv || fail "python3 -m venv 실패(python3-venv 패키지 확인)"
else
  echo "[setup-env] 기존 .venv 재사용"
fi

# 왜: 태그 설치한 이웃 라이브러리는 같은 이름의 옛 버전이 남으면 핀과 달라질 수 있어 먼저 지우고 핀대로 다시 깐다.
#     이름은 requirements.txt의 git 줄(…/openit_<이름>.git@태그)에서 읽는다(여기에 버전을 적지 않는다).
libs=$(sed -n 's#^git+ssh://.*/openit_\([A-Za-z0-9_-]*\)\.git@.*#\1#p' requirements.txt | tr '_' '-')
if [ -n "$libs" ]; then
  # shellcheck disable=SC2086
  .venv/bin/python -m pip uninstall -y $libs >/dev/null 2>&1 || true
fi

install() {
  .venv/bin/python -m pip install -c requirements.lock -r requirements.txt -e .
}

# 왜: 일시적 네트워크 오류는 한 번 재시도하면 대부분 풀린다. 그래도 실패면 숨기지 않고 비0으로 끝낸다.
if ! install; then
  echo "[setup-env] 설치 실패, 10초 뒤 한 번 재시도" >&2
  sleep 10
  install || fail "pip install 두 번 실패(네트워크, GitHub ssh 접근, 또는 핀 충돌). 위 pip 출력을 확인"
fi

# 왜: 시험(tests/test_structure.py 5번)이 types/ 를 다시 생성해 비교하므로 생성 도구(requirements-tools.txt: datamodel-code-generator)도 시험 명령에 필요한 환경이다.
#     런타임 의존과 섞이지 않게 별도 가상환경 .venv-tools 에 설치한다(scripts/gen-types.sh 와 같은 곳).
if [ -f requirements-tools.txt ]; then
  if [ ! -x .venv-tools/bin/python ]; then
    echo "[setup-env] .venv-tools 생성(타입 생성 도구)"
    python3 -m venv .venv-tools || fail "python3 -m venv .venv-tools 실패"
  fi
  .venv-tools/bin/python -m pip install -q -r requirements-tools.txt || fail "타입 생성 도구 설치 실패(requirements-tools.txt)"
fi

echo "[setup-env] 완료. 시험: .venv/bin/python -m pytest"
