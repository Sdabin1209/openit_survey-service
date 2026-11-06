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
. scripts/docker-env.sh      # 사용자 전역 ~/.docker 설정에 기대지 않는다(DOCKER_CONFIG, DOCKER_HOST)
PYTHON=.venv/bin/python      # 이 영역의 시험 환경(setup-env.sh). 리포 루트 기준 상대 경로: 위에서 루트로 cd했다
die() { echo "build-image: $*" >&2; exit 1; }
# 주입 선언(계약 ContainerInputs 3.5.0, 라벨 io.openit.inputs): 정본은 리포 안 JSON. 라벨로 달기 전에 계약으로 검증하고(R7), 빌드한 이미지의 실행 사용자와 대조한다.
# 왜 대조하는가: 배치 영역은 선언의 숫자 uid:gid 로 파일·쓰기 권한을 준다. 이미지와 다르면 기동에서야 권한 오류가 난다(2026-11-06 DB 비밀 읽기 실패).
inputs_label() {   # inputs_label <선언 파일> <이미지 이름> -> 한 줄 JSON(표준 출력)
  "${PYTHON:-${PY:-python3}}" -c '
import json, sys
from boundary_contracts import validator
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("image") != sys.argv[2]:
    sys.exit("%s 의 image(%s)가 이미지 이름 %s 와 다르다" % (sys.argv[1], d.get("image"), sys.argv[2]))
errs = list(validator("ContainerInputs").iter_errors(d))
if errs:
    sys.exit("%s 가 ContainerInputs 를 어긴다: " % sys.argv[1] + ", ".join("/" + "/".join(map(str, e.absolute_path)) + ":" + e.validator for e in errs[:5]))
print(json.dumps(d, ensure_ascii=False, separators=(",", ":"), sort_keys=True))' "$1" "$2"
}
inputs_check() {   # inputs_check <이미지 태그> <inputs_label 출력>: 다르면 이미지를 지우고 실패
  local u g cu
  # 실행 사용자: 이미지 안 id 로 읽는다. id 가 없는 이미지(distroless)는 이미지 설정 User 가 숫자 uid:gid 일 때만 그 값을 쓴다(이름이면 판정하지 못해 실패)
  if u=$("${DOCKER:-docker}" run --rm --network none --entrypoint id "$1" -u 2>/dev/null) && g=$("${DOCKER:-docker}" run --rm --network none --entrypoint id "$1" -g 2>/dev/null); then :
  else
    cu=$("${DOCKER:-docker}" image inspect "$1" --format '{{.Config.User}}' 2>/dev/null)
    if [[ "$cu" =~ ^([0-9]+):([0-9]+)$ ]]; then u=${BASH_REMATCH[1]}; g=${BASH_REMATCH[2]}
    else "${DOCKER:-docker}" rmi -f "$1" >/dev/null 2>&1; echo "build-image: 이미지 실행 사용자를 읽지 못했다(id 없음, User=$cu): $1" >&2; return 1; fi
  fi
  "${PYTHON:-${PY:-python3}}" -c '
import json, sys
d = json.loads(sys.argv[1]); u, g = int(sys.argv[2]), int(sys.argv[3])
bad = sorted(k for s in ("roles", "jobs") for k, st in d[s].items() if (st["process"]["uid"], st["process"]["gid"]) != (u, g))
sys.exit("주입 선언의 process 가 이미지 실행 사용자 %d:%d 와 다르다: %s" % (u, g, bad) if bad else 0)' "$2" "$u" "$g" \
    || { "${DOCKER:-docker}" rmi -f "$1" >/dev/null 2>&1; echo "build-image: 주입 선언이 이미지와 다르다. 이미지를 지웠다: $1" >&2; return 1; }
}
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
variant = req.get("variant") or ""
if variant not in ("", "db"):
    sys.exit("build-image: 이 영역은 variant를 모른다: " + variant)
need = [("versions", "boundary")] if variant == "db" else \
       [("versions", "boundary"), ("versions", "model_runtime"), ("dependency_sources", "boundary"), ("dependency_sources", "model_runtime")]
missing = [".".join(k) for k in need if not (req.get(k[0]) or {}).get(k[1])]
if missing:
    sys.exit("build-image: 이 영역이 쓰는 키가 요청에 없다: " + ", ".join(missing))
out = {"SRC": req["source_dir"], "REVISION": req["revision"], "BOUNDARY_TAG": req["versions"]["boundary"],
       "MODEL_RUNTIME_TAG": req["versions"].get("model_runtime", ""), "BOUNDARY_SRC": (req.get("dependency_sources") or {}).get("boundary", ""),
       "MODEL_RUNTIME_SRC": (req.get("dependency_sources") or {}).get("model_runtime", ""), "IMAGE_TAG": req["image_tag"],
       "SCHEMA_REQ": req["versions"].get("survey_schema", ""), "VARIANT": variant}
for k, v in out.items():
    print(f"{k}={shlex.quote(v)}")
PY
) || exit 1      # 대입의 종료 코드를 보므로 검증이 실패하면 여기서 멈춘다(eval "$(...)"는 실패를 가린다)
eval "$VARS"
[ "$(cd "$SRC" && pwd -P)" = "$(pwd -P)" ] || die "source_dir($SRC)이 이 리포 루트가 아니다"
if [ "$VARIANT" = db ]; then
  # 데이터베이스 컴포넌트 이미지(계약 D126): 앱 이미지의 이웃 소스·잠금·link는 필요 없다. 선언(db/database.json)을 계약으로 검증해 계정 표를 만들고 라벨 io.openit.database로 단다.
  # 선언을 검증한 계약이 이 호스트 환경(.venv)에 설치된 boundary-contracts다. 라벨의 계약 버전은 그 설치본의 버전이어야 한다(요청과 다르면 라벨이 거짓이 된다).
  BVER=$("$PYTHON" -c 'import importlib.metadata as m;print("v"+m.version("boundary-contracts"))') || die "boundary-contracts 설치 버전을 읽지 못했다"
  [ "$BVER" = "$BOUNDARY_TAG" ] || die "요청 versions.boundary($BOUNDARY_TAG)와 선언을 검증한 설치본의 실제 버전($BVER)이 다르다. 라벨이 거짓이 되므로 이미지를 만들지 않는다"
  "$PYTHON" db/gen_accounts.py db/database.json db/accounts.tsv || die "db/database.json이 계약 DatabaseDeclaration을 어긴다"
  DECL=$("$PYTHON" -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")),ensure_ascii=False,separators=(",",":")))' db/database.json) || die "db/database.json을 읽지 못했다"
  echo "build-image: $IMAGE_TAG (variant db, revision $REVISION, boundary $BOUNDARY_TAG)" >&2
  INPUTS=$(inputs_label db/inputs.json survey-db) || die "db/inputs.json 이 계약 ContainerInputs 를 어긴다"
  "$DOCKER" build -f db/Dockerfile -t "$IMAGE_TAG" --build-arg COMMIT="$REVISION" --build-arg BOUNDARY_TAG="$BOUNDARY_TAG" --build-arg DATABASE_DECLARATION="$DECL" --label "io.openit.inputs=$INPUTS" . >&2 || die "docker build가 실패했다"
  inputs_check "$IMAGE_TAG" "$INPUTS" || exit 1
  "$DOCKER" image inspect "$IMAGE_TAG" | "$PYTHON" -c '
import json, sys
img = json.load(sys.stdin)[0]
labels = {k: v for k, v in (img["Config"].get("Labels") or {}).items() if k.startswith("io.openit.")}
print(json.dumps({"image": sys.argv[1], "image_id": img["Id"], "labels": labels}, ensure_ascii=False, separators=(",", ":")))' "$IMAGE_TAG"
  exit 0
fi
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
SCHEMA_ACTUAL=$(pkg_version schema openit-survey-schema) || exit 1       # 이 이미지가 실제로 쓰는 문진 스키마는 이 리포의 schema/다
[ -z "$SCHEMA_REQ" ] || [ "$SCHEMA_REQ" = "$SCHEMA_ACTUAL" ] || die "요청 versions.survey_schema($SCHEMA_REQ)와 이 리포 schema/의 실제 버전($SCHEMA_ACTUAL)이 다르다. 라벨이 거짓이 되므로 이미지를 만들지 않는다"
[ "$MODEL_RUNTIME_ACTUAL" = "$MODEL_RUNTIME_TAG" ] || die "요청 versions.model_runtime($MODEL_RUNTIME_TAG)과 dependency_sources.model_runtime의 실제 버전($MODEL_RUNTIME_ACTUAL)이 다르다. 라벨이 거짓이 되므로 이미지를 만들지 않는다"

# 2) 라벨 값: 문진 스키마 주 버전과 langgraph 버전은 이 영역 자신의 소스에서 읽는다(deploy가 이 영역의 파일 위치를 알 필요가 없다)
SURVEY_SCHEMA_MAJOR=${SCHEMA_ACTUAL#v}; SURVEY_SCHEMA_MAJOR=${SURVEY_SCHEMA_MAJOR%%.*}     # 라벨 io.openit.contracts.survey-schema = 문진 스키마 패키지의 주 버전(ImageLabels: 정수)
LANGGRAPH_VERSION=$(sed -n 's/^langgraph==//p' requirements.lock | head -1)
[ -n "$SURVEY_SCHEMA_MAJOR" ] && [ -n "$LANGGRAPH_VERSION" ] || die "schema/pyproject.toml의 버전 또는 requirements.lock의 langgraph 고정을 읽지 못했다"

# 노출 선언(계약 Exposure): 정본은 리포 안 deploy-exposure.json. 이미지 라벨 io.openit.exposure(JSON 한 줄 문자열)로 단다(ADR-0041)
EXPOSURE=$("$PYTHON" -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1],encoding="utf-8")),ensure_ascii=False,separators=(",",":")))' deploy-exposure.json) || die "deploy-exposure.json을 읽지 못했다"

# 잡 선언(계약 JobsDeclaration): 정본은 리포 안 deploy-jobs.json. 내보내기 전에 계약으로 검증하고(R7) 이미지 라벨 io.openit.jobs로 단다. 어기면 이미지를 만들지 않는다
JOBS=$("$PYTHON" -c 'import json,sys
from boundary_contracts import validator
d=json.load(open(sys.argv[1],encoding="utf-8"))
errs=[e.message[:200] for e in validator("JobsDeclaration").iter_errors(d)]
if errs: sys.exit("JobsDeclaration 위반: "+"; ".join(errs[:3]))
print(json.dumps(d,ensure_ascii=False,separators=(",",":")))' deploy-jobs.json) || die "deploy-jobs.json이 계약 JobsDeclaration을 어긴다"

INPUTS=$(inputs_label deploy-inputs.json survey-service) || die "deploy-inputs.json 이 계약 ContainerInputs 를 어긴다"

# 2-1) 통신 기반 라이브러리 link(D118)의 소스: BuildImageRequest(계약 3.1.2)의 dependency_sources에는 link 키가 없다(변경 요청 CR-link-001 말미).
#      그래서 requirements.txt가 고정한 태그를 빌드 호스트에서 한 번 받아 이름 있는 빌드 컨텍스트로 준다(Dockerfile 안에서는 네트워크를 쓰지 않는다).
LINK_LINE=$(sed -n 's/^openit-link @ git+\(ssh:\/\/.*\)\.git@\(v[0-9.]*\)$/\1 \2/p' requirements.txt | head -1)
[ -n "$LINK_LINE" ] || die "requirements.txt에서 openit-link 고정 줄을 읽지 못했다"
LINK_TMP=$(mktemp -d); trap 'rm -rf "$LINK_TMP"' EXIT
git clone -q --depth 1 --branch "${LINK_LINE#* }" "${LINK_LINE% *}.git" "$LINK_TMP/link" >&2 || die "link ${LINK_LINE#* }을 받지 못했다(GitHub ssh 접근 확인)"
LINK_ACTUAL=$(pkg_version "$LINK_TMP/link" openit-link) || exit 1
[ "$LINK_ACTUAL" = "${LINK_LINE#* }" ] || die "받은 link의 실제 버전($LINK_ACTUAL)이 requirements.txt 고정(${LINK_LINE#* })과 다르다"

# 3) 빌드: 이웃 소스는 이름 있는 빌드 컨텍스트로 준다(Dockerfile이 pip로 설치). 빌드 출력은 표준 오류로 보낸다.
echo "build-image: $IMAGE_TAG (revision $REVISION, boundary $BOUNDARY_ACTUAL, model-runtime $MODEL_RUNTIME_ACTUAL)" >&2
"$DOCKER" build -t "$IMAGE_TAG" \
  --build-arg COMMIT="$REVISION" --build-arg BOUNDARY_TAG="$BOUNDARY_ACTUAL" --build-arg SURVEY_SCHEMA_MAJOR="$SURVEY_SCHEMA_MAJOR" \
  --build-arg MODEL_RUNTIME_TAG="$MODEL_RUNTIME_ACTUAL" --build-arg LANGGRAPH_VERSION="$LANGGRAPH_VERSION" \
  --label "io.openit.exposure=$EXPOSURE" --label "io.openit.jobs=$JOBS" --label "io.openit.inputs=$INPUTS" --build-context "boundary_contracts=$BOUNDARY_SRC" --build-context "model_runtime=$MODEL_RUNTIME_SRC" --build-context "openit_link=$LINK_TMP/link" . >&2

# 3-0) 주입 선언의 실행 사용자가 이미지와 같은지 확인한다(계약 ContainerInputs 3.5.0). 다르면 이미지를 지우고 실패한다.
inputs_check "$IMAGE_TAG" "$INPUTS" || exit 1

# 3-1) 이미지에 실제로 설치된 버전이 라벨과 같은지 확인한다(U50). 다르면 이미지를 지우고 실패한다.
INSTALLED=$("$DOCKER" run --rm --entrypoint python "$IMAGE_TAG" -c "import importlib.metadata as m; print(m.version('boundary-contracts')); print(m.version('model-runtime')); print(m.version('openit-survey-schema'))") || die "이미지에서 설치된 버전을 읽지 못했다"
if [ "$INSTALLED" != "$(printf '%s\n%s\n%s' "${BOUNDARY_ACTUAL#v}" "${MODEL_RUNTIME_ACTUAL#v}" "${SCHEMA_ACTUAL#v}")" ]; then
  "$DOCKER" rmi -f "$IMAGE_TAG" >&2 || true
  die "이미지에 설치된 버전이 라벨과 다르다(설치: $(echo $INSTALLED), 라벨: ${BOUNDARY_ACTUAL#v} ${MODEL_RUNTIME_ACTUAL#v} ${SCHEMA_ACTUAL#v}). 이미지를 지웠다"
fi

# 4) 결과: 이미지에 실제로 붙은 io.openit.* 라벨을 docker inspect로 읽어 마지막 줄에 낸다
"$DOCKER" image inspect "$IMAGE_TAG" | "$PYTHON" -c '
import json, sys
img = json.load(sys.stdin)[0]
labels = {k: v for k, v in (img["Config"].get("Labels") or {}).items() if k.startswith("io.openit.")}
print(json.dumps({"image": sys.argv[1], "image_id": img["Id"], "labels": labels}, ensure_ascii=False, separators=(",", ":")))' "$IMAGE_TAG"
