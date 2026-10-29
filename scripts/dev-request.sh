#!/usr/bin/env bash
# 개발·스모크용: requirements.txt에 고정된 태그의 이웃 소스 트리를 받아(git clone --depth 1 --branch) 빌드 요청(BuildImageRequest) JSON을 만든다.
# 운영 빌드는 deploy가 요청을 만든다. 이 스크립트는 scripts/build-image.sh를 개발자가 직접 시험할 때만 쓴다.
# 사용: bash scripts/dev-request.sh <작업 디렉터리> <image_tag>   -> 표준 출력에 요청 파일 경로
# REPO_BASE: 이웃 리포 위치(기본 원격 git@github.com:Sdabin1209, 로컬 태그 시점 소스를 쓰려면 openit_<이름>.git 사본이 있는 폴더)
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 2 ] || { echo "사용: bash scripts/dev-request.sh <작업 디렉터리> <image_tag>" >&2; exit 2; }
WORK=$(cd "$1" && pwd); IMAGE_TAG=$2
REPO_BASE=${REPO_BASE:-git@github.com:Sdabin1209}
tag_of() { sed -n "s|.*openit_$1\.git@\(v[0-9][0-9.]*\).*|\1|p" requirements.txt | head -1; }
BC=$(tag_of boundary-contracts); MR=$(tag_of model-runtime)
[ -n "$BC" ] && [ -n "$MR" ] || { echo "requirements.txt에서 boundary-contracts·model-runtime 태그를 읽지 못했다" >&2; exit 1; }
rm -rf "$WORK/boundary_contracts" "$WORK/model_runtime"
git clone -q --depth 1 --branch "$BC" "$REPO_BASE/openit_boundary-contracts.git" "$WORK/boundary_contracts" >&2
git clone -q --depth 1 --branch "$MR" "$REPO_BASE/openit_model-runtime.git" "$WORK/model_runtime" >&2
rm -rf "$WORK/boundary_contracts/.git" "$WORK/model_runtime/.git"
REV=$(git rev-parse --short HEAD)
SS=v$(sed -n 's/^version = "\(.*\)"/\1/p' schema/pyproject.toml | head -1)      # 이 리포 schema/의 실제 버전(요청과 달라야 할 이유가 없다)
python3 - "$WORK" "$PWD" "$REV" "$BC" "$MR" "$IMAGE_TAG" "$SS" <<'PY' > "$WORK/request.json"
import json, sys
work, src, rev, bc, mr, tag, ss = sys.argv[1:]
print(json.dumps({"source_dir": src, "revision": rev, "versions": {"boundary": bc, "model_runtime": mr, "survey_schema": ss},
                  "dependency_sources": {"boundary": f"{work}/boundary_contracts", "model_runtime": f"{work}/model_runtime"}, "image_tag": tag}))
PY
echo "$WORK/request.json"
