#!/usr/bin/env bash
# 왜: types/ 는 schemas/ 에서만 만든다(DESIGN.md 3절). 도구는 별도 가상환경(.venv-tools)에 requirements-tools.txt 버전으로 설치한다(런타임 의존이 아니다).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -x .venv-tools/bin/python ] || python3 -m venv .venv-tools
.venv-tools/bin/python -m pip install -q -r requirements-tools.txt
.venv-tools/bin/python scripts/gen_types.py
