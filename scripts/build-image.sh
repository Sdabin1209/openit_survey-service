#!/usr/bin/env bash
# 영역 빌드 진입점(계약 boundary-contracts 2.5.0, BuildImageRequest/BuildImageResult, 이 영역의 ADR-0037).
# 사용: bash scripts/build-image.sh <요청 JSON 파일>   (영역 리포 루트에서)
# 표준 출력 마지막 줄 = BuildImageResult JSON 한 줄. 진행 기록은 표준 오류. 종료 0 성공.
# 이웃 라이브러리(boundary-contracts, model-runtime)는 요청의 dependency_sources(소스 트리)만 쓴다: 이웃 리포 경로·빌드 중 네트워크(SSH) 없음.
# 환경 변수: DOCKER(도커 실행 파일, 기본 docker)뿐이다. 요청 검증용 파이썬은 이 영역 자신의 .venv(scripts/setup-env.sh가 만든 것)로 정해져 있다:
# deploy가 파이썬 경로를 넘기면 deploy가 영역의 환경을 아는 것이다(구조 규칙 1). 비밀 값 없음.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}
PYTHON=.venv/bin/python      # 이 영역의 시험 환경(setup-env.sh). 리포 루트 기준 상대 경로: 위에서 루트로 cd했다
die() { echo "build-image: $*" >&2; exit 1; }
[ -x "$PYTHON" ] || die "$PYTHON 이 없다. 먼저 bash scripts/setup-env.sh 로 이 영역의 환경을 만든다"
[ $# -eq 1 ] && [ -f "$1" ] || die "사용: bash scripts/build-image.sh <요청 JSON 파일>"

# 1) 요청을 계약 validator로 검증하고(영역 경계 입력) 이 영역이 쓰는 키가 모두 있는지 본다. 기본값을 영역 안에 두지 않는다(기준표와 어긋나지 않게).
VARS=$("$PYTHON" - "$1" <<'PY'
import json, shlex, sys
try:
    from boundary_contracts import validator
except ImportError:
    sys.exit("build-image: .venv에서 boundary_contracts를 가져올 수 없다. bash scripts/setup-env.sh 를 다시 실행한다")
req = json.load(open(sys.argv[1], encoding="utf-8"))
errs = list(validator("BuildImageRequest").iter_errors(req))
if errs:
    sys.exit("build-image: BuildImageRequest를 어긴 요청: " + errs[0].message[:200])
if req.get("variant"):
    sys.exit("build-image: 이 영역은 variant를 모른다: " + req["variant"])
need = [("versions", "boundary"), ("versions", "model_runtime"), ("dependency_sources", "boundary"), ("dependency_sources", "model_runtime")]
missing = [".".join(k) for k in need if not (req.get(k[0]) or {}).get(k[1])]
if missing:
    sys.exit("build-image: 이 영역이 쓰는 키가 요청에 없다: " + ", ".join(missing))
out = {"SRC": req["source_dir"], "REVISION": req["revision"], "BOUNDARY_TAG": req["versions"]["boundary"],
       "MODEL_RUNTIME_TAG": req["versions"]["model_runtime"], "BOUNDARY_SRC": req["dependency_sources"]["boundary"],
       "MODEL_RUNTIME_SRC": req["dependency_sources"]["model_runtime"], "IMAGE_TAG": req["image_tag"]}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
PY
) || exit 1      # 대입의 종료 코드를 보므로 검증이 실패하면 여기서 멈춘다(eval "$(...)"는 실패를 가린다)
eval "$VARS"
[ "$(cd "$SRC" && pwd -P)" = "$(pwd -P)" ] || die "source_dir($SRC)이 이 리포 루트가 아니다"
for d in "$BOUNDARY_SRC" "$MODEL_RUNTIME_SRC"; do [ -d "$d" ] || die "dependency_sources 경로가 디렉터리가 아니다: $d"; done

# 1-1) 라벨 정직성(U50): 라벨은 요청의 versions가 아니라 **실제로 설치할 소스 트리의 버전**(pyproject.toml)에서 찍는다. 요청 버전과 다르면 라벨이 거짓이 되므로 이미지를 만들지 않고 실패한다.
pkg_version() {   # pkg_version <소스 트리> <패키지 이름> -> vX.Y.Z
  local name ver
  name=$(sed -n 's/^name = "\(.*\)"$/\1/p' "$1/pyproject.toml" | head -1); ver=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$1/pyproject.toml" | head -1)
  [ "$name" = "$2" ] && [ -n "$ver" ] || die "$1 가 $2 의 소스 트리가 아니거나 버전을 읽지 못했다(pyproject.toml)"
  echo "v$ver"
}
BOUNDARY_ACTUAL=$(pkg_version "$BOUNDARY_SRC" boundary-contracts) || exit 1
MODEL_RUNTIME_ACTUAL=$(pkg_version "$MODEL_RUNTIME_SRC" model-runtime) || exit 1
[ "$BOUNDARY_ACTUAL" = "$BOUNDARY_TAG" ] || die "요청 versions.boundary($BOUNDARY_TAG)와 dependency_sources.boundary의 실제 버전($BOUNDARY_ACTUAL)이 다르다. 라벨이 거짓이 되므로 이미지를 만들지 않는다"
[ "$MODEL_RUNTIME_ACTUAL" = "$MODEL_RUNTIME_TAG" ] || die "요청 versions.model_runtime($MODEL_RUNTIME_TAG)과 dependency_sources.model_runtime의 실제 버전($MODEL_RUNTIME_ACTUAL)이 다르다. 라벨이 거짓이 되므로 이미지를 만들지 않는다"

# 2) 라벨 값: 문진 스키마 주 버전과 langgraph 버전은 이 영역 자신의 소스에서 읽는다(deploy가 이 영역의 파일 위치를 알 필요가 없다)
SCHEMA_CONTRACT_VERSION=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)
LANGGRAPH_VERSION=$(sed -n 's/^langgraph==//p' requirements.lock | head -1)
[ -n "$SCHEMA_CONTRACT_VERSION" ] && [ -n "$LANGGRAPH_VERSION" ] || die "schema/CONTRACT_VERSION 또는 requirements.lock의 langgraph 고정을 읽지 못했다"

# 노출 선언(계약 Exposure): 정본은 리포 안 deploy-exposure.json. 이미지 라벨 io.openit.exposure(JSON 한 줄 문자열)로 단다(ADR-0041)
EXPOSURE=$("$PYTHON" -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")),ensure_ascii=False,separators=(",",":")))' deploy-exposure.json) || die "deploy-exposure.json을 읽지 못했다"

# 3) 빌드: 이웃 소스는 이름 있는 빌드 컨텍스트로 준다(Dockerfile이 pip로 설치). 빌드 출력은 표준 오류로 보낸다.
echo "build-image: $IMAGE_TAG (revision $REVISION, boundary $BOUNDARY_ACTUAL, model-runtime $MODEL_RUNTIME_ACTUAL)" >&2
"$DOCKER" build -t "$IMAGE_TAG" \
  --build-arg COMMIT="$REVISION" --build-arg BOUNDARY_TAG="$BOUNDARY_ACTUAL" --build-arg SCHEMA_CONTRACT_VERSION="$SCHEMA_CONTRACT_VERSION" \
  --build-arg MODEL_RUNTIME_TAG="$MODEL_RUNTIME_ACTUAL" --build-arg LANGGRAPH_VERSION="$LANGGRAPH_VERSION" \
  --label "io.openit.exposure=$EXPOSURE" --build-context "boundary_contracts=$BOUNDARY_SRC" --build-context "model_runtime=$MODEL_RUNTIME_SRC" . >&2

# 3-1) 이미지에 실제로 설치된 버전이 라벨과 같은지 확인한다(U50). 다르면 이미지를 지우고 실패한다.
INSTALLED=$("$DOCKER" run --rm --entrypoint python "$IMAGE_TAG" -c "import importlib.metadata as m; print(m.version('boundary-contracts')); print(m.version('model-runtime'))") || die "이미지에서 설치된 버전을 읽지 못했다"
if [ "$INSTALLED" != "$(printf '%s\n%s' "${BOUNDARY_ACTUAL#v}" "${MODEL_RUNTIME_ACTUAL#v}")" ]; then
  "$DOCKER" rmi -f "$IMAGE_TAG" >&2 || true
  die "이미지에 설치된 버전이 라벨과 다르다(설치: $(echo $INSTALLED), 라벨: ${BOUNDARY_ACTUAL#v} ${MODEL_RUNTIME_ACTUAL#v}). 이미지를 지웠다"
fi

# 4) 결과: 이미지에 실제로 붙은 io.openit.* 라벨을 docker inspect로 읽어 마지막 줄에 낸다
"$DOCKER" image inspect "$IMAGE_TAG" | "$PYTHON" -c '
import json, sys
img = json.load(sys.stdin)[0]
labels = {k: v for k, v in (img["Config"].get("Labels") or {}).items() if k.startswith("io.openit.")}
print(json.dumps({"image": sys.argv[1], "image_id": img["Id"], "labels": labels}, ensure_ascii=False, separators=(",", ":")))' "$IMAGE_TAG"
