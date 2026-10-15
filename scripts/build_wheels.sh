#!/usr/bin/env bash
# 이웃 라이브러리를 Git 태그 시점의 소스로 꺼내(git archive) wheel을 만든다. docker 안에서는 ssh가 어려워서 밖에서 만든다.
# 문진 스키마 패키지는 이 리포지토리의 schema/ 에서 만든다. 결과: build/wheels/*.whl 과 build/wheels/VERSIONS.env
set -euo pipefail
cd "$(dirname "$0")/.."
BOUNDARY_TAG=${BOUNDARY_TAG:-v1.3.0}
TURN_ENGINE_TAG=${TURN_ENGINE_TAG:-v0.2.0}
MODEL_RUNTIME_TAG=${MODEL_RUNTIME_TAG:-v0.4.0}
PIP=${PIP:-.venv/bin/pip}
rm -rf build && mkdir -p build/wheels build/src
for spec in "boundary-contracts:$BOUNDARY_TAG" "turn-engine:$TURN_ENGINE_TAG" "model-runtime:$MODEL_RUNTIME_TAG"; do
  name=${spec%%:*}; tag=${spec#*:}
  mkdir -p "build/src/$name"
  git -C "../$name" archive "$tag" | tar -x -C "build/src/$name"
  $PIP wheel -q --no-deps --no-build-isolation -w build/wheels "build/src/$name"
done
$PIP wheel -q --no-deps --no-build-isolation -w build/wheels ./schema
$PIP wheel -q --no-deps --no-build-isolation -w build/wheels .
SCHEMA_INT=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)
cat > build/wheels/VERSIONS.env <<EOF
BOUNDARY_TAG=$BOUNDARY_TAG
TURN_ENGINE_TAG=$TURN_ENGINE_TAG
MODEL_RUNTIME_TAG=$MODEL_RUNTIME_TAG
SCHEMA_CONTRACT_VERSION=$SCHEMA_INT
EOF
ls build/wheels
