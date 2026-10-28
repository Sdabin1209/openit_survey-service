#!/usr/bin/env bash
# 이웃 라이브러리를 Git 태그 시점의 소스로 꺼내(git archive) wheel을 만든다. docker 안에서는 ssh가 어려워서 밖에서 만든다.
# 문진 스키마 패키지는 이 리포지토리의 schema/ 에서 만든다. 결과: build/wheels/*.whl 과 build/wheels/VERSIONS.env
set -euo pipefail
cd "$(dirname "$0")/.."
BOUNDARY_TAG=${BOUNDARY_TAG:-v2.2.2}
MODEL_RUNTIME_TAG=${MODEL_RUNTIME_TAG:-v0.9.0}
PIP=${PIP:-.venv/bin/pip}
# 왜: 태그를 원격에 올리기 전(헤드가 E2E 뒤에 push)에도 같은 스크립트로 이미지를 만들어 볼 수 있게 저장소 위치만 바꿀 수 있다. 기본은 원격이다.
REPO_BASE=${REPO_BASE:-git@github.com:Sdabin1209}
rm -rf build && mkdir -p build/wheels build/src
for spec in "boundary-contracts:$BOUNDARY_TAG" "model-runtime:$MODEL_RUNTIME_TAG"; do
  name=${spec%%:*}; tag=${spec#*:}
  # 원격 태그에서 받는다(이웃 폴더를 읽지 않는다). 리포 이름은 openit_<name>
  git clone -q --depth 1 --branch "$tag" "$REPO_BASE/openit_$name.git" "build/src/$name"
  rm -rf "build/src/$name/.git"
  $PIP wheel -q --no-deps --no-build-isolation -w build/wheels "build/src/$name"
done
$PIP wheel -q --no-deps --no-build-isolation -w build/wheels ./schema
$PIP wheel -q --no-deps --no-build-isolation -w build/wheels .
SCHEMA_INT=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)
LANGGRAPH_VERSION=$(sed -n 's/^langgraph==//p' requirements.lock | head -1)
[ -n "$LANGGRAPH_VERSION" ] || { echo "requirements.lock에 langgraph== 고정이 없다"; exit 1; }
cat > build/wheels/VERSIONS.env <<EOF
BOUNDARY_TAG=$BOUNDARY_TAG
MODEL_RUNTIME_TAG=$MODEL_RUNTIME_TAG
SCHEMA_CONTRACT_VERSION=$SCHEMA_INT
LANGGRAPH_VERSION=$LANGGRAPH_VERSION
EOF
ls build/wheels
