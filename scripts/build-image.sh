#!/usr/bin/env bash
# 영역 빌드 진입점(계약 boundary-contracts 2.5.0, BuildImageRequest/BuildImageResult, 이 영역의 ADR-0037).
# 사용: bash scripts/build-image.sh <요청 JSON 파일>   (영역 리포 루트에서)
# 표준 출력 마지막 줄 = BuildImageResult JSON 한 줄. 진행 기록은 표준 오류. 종료 0 성공.
# 이웃 라이브러리(boundary-contracts, model-runtime)는 요청의 dependency_sources(소스 트리)만 쓴다: 이웃 리포 경로·빌드 중 네트워크(SSH) 없음.
# 환경 변수: DOCKER(도커 실행 파일, 기본 docker), PYTHON(boundary-contracts가 설치된 파이썬, 기본 python3: 요청 검증용). 비밀 값 없음.
set -euo pipefail
cd "$(dirname "$0")/.."
DOCKER=${DOCKER:-docker}
PYTHON=${PYTHON:-python3}
die() { echo "build-image: $*" >&2; exit 1; }
[ $# -eq 1 ] && [ -f "$1" ] || die "사용: bash scripts/build-image.sh <요청 JSON 파일>"

# 1) 요청을 계약 validator로 검증하고(영역 경계 입력) 이 영역이 쓰는 키가 모두 있는지 본다. 기본값을 영역 안에 두지 않는다(기준표와 어긋나지 않게).
VARS=$("$PYTHON" - "$1" <<'PY'
import json, shlex, sys
try:
    from boundary_contracts import validator
except ImportError:
    sys.exit("build-image: boundary_contracts를 가져올 수 없다. PYTHON 환경 변수로 boundary-contracts가 설치된 파이썬을 준다")
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

# 2) 라벨 값: 문진 스키마 주 버전과 langgraph 버전은 이 영역 자신의 소스에서 읽는다(deploy가 이 영역의 파일 위치를 알 필요가 없다)
SCHEMA_CONTRACT_VERSION=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)
LANGGRAPH_VERSION=$(sed -n 's/^langgraph==//p' requirements.lock | head -1)
[ -n "$SCHEMA_CONTRACT_VERSION" ] && [ -n "$LANGGRAPH_VERSION" ] || die "schema/CONTRACT_VERSION 또는 requirements.lock의 langgraph 고정을 읽지 못했다"

# 3) 빌드: 이웃 소스는 이름 있는 빌드 컨텍스트로 준다(Dockerfile이 pip로 설치). 빌드 출력은 표준 오류로 보낸다.
echo "build-image: $IMAGE_TAG (revision $REVISION, boundary $BOUNDARY_TAG, model-runtime $MODEL_RUNTIME_TAG)" >&2
"$DOCKER" build -t "$IMAGE_TAG" \
  --build-arg COMMIT="$REVISION" --build-arg BOUNDARY_TAG="$BOUNDARY_TAG" --build-arg SCHEMA_CONTRACT_VERSION="$SCHEMA_CONTRACT_VERSION" \
  --build-arg MODEL_RUNTIME_TAG="$MODEL_RUNTIME_TAG" --build-arg LANGGRAPH_VERSION="$LANGGRAPH_VERSION" \
  --build-context "boundary_contracts=$BOUNDARY_SRC" --build-context "model_runtime=$MODEL_RUNTIME_SRC" . >&2

# 4) 결과: 이미지에 실제로 붙은 io.openit.* 라벨을 docker inspect로 읽어 마지막 줄에 낸다
"$DOCKER" image inspect "$IMAGE_TAG" | "$PYTHON" -c '
import json, sys
img = json.load(sys.stdin)[0]
labels = {k: v for k, v in (img["Config"].get("Labels") or {}).items() if k.startswith("io.openit.")}
print(json.dumps({"image": sys.argv[1], "image_id": img["Id"], "labels": labels}, ensure_ascii=False, separators=(",", ":")))' "$IMAGE_TAG"
