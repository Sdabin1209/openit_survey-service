"""이웃 라이브러리 핀의 일관성과 이미지 라벨 규칙.

핀: 이웃 라이브러리 태그는 requirements.txt 한 곳에만 있다(빌드 진입점 scripts/build-image.sh는 요청의 versions만 쓰고 기본 태그를 두지 않는다, 계약 2.5.0).
model-runtime 핀은 설치된 패키지의 버전과 같다. 기대값의 출처는 설치된 패키지(`model_runtime.RUNTIME_VERSION`)다: 시험이 숫자를 따로 적지 않는다.

라벨: deploy의 ImageLabels(`io.openit.contracts.survey-schema`는 `^([0-9]+|n/a)$`, `unknown`·`survey-schema-v1.0.0` 같은 문자열 불가)를
이 영역의 이미지 라벨 입력(Dockerfile 기본값, build-image.sh가 읽는 schema/pyproject.toml의 버전)이 지키는지 본다.
"""
import pathlib
import re

import model_runtime

ROOT = pathlib.Path(__file__).resolve().parents[1]
SURVEY_SCHEMA_LABEL = re.compile(r"^([0-9]+|n/a)$")
REVISION_LABEL = re.compile(r"^[0-9a-f]{7,40}$")


def _req_tag(name):
    m = re.search(rf"openit_{name}\.git@(v[0-9.]+)", (ROOT / "requirements.txt").read_text())
    return m.group(1)


def test_model_runtime_pin_agrees_with_the_installed_package():
    assert _req_tag("model-runtime") == "v" + model_runtime.RUNTIME_VERSION


def test_no_pins_outside_requirements_and_no_turn_engine():
    """이웃 태그의 정본은 requirements.txt뿐이다. 진입점·Dockerfile에 기본 태그를 두면 기준표와 어긋난다."""
    assert "turn-engine" not in (ROOT / "requirements.txt").read_text()
    for f in ("scripts/build-image.sh", "Dockerfile"):
        text = (ROOT / f).read_text()
        assert not re.search(r"(BOUNDARY_TAG|MODEL_RUNTIME_TAG)=\$?\{?[^$\s]*v[0-9]+\.[0-9]+\.[0-9]+", text), f
    assert not (ROOT / "scripts/build_wheels.sh").exists()


def test_schema_label_input_is_the_major_of_the_repo_schema_package_not_a_tag_string():
    """라벨 io.openit.contracts.survey-schema는 정수(주 버전)다. 값은 이 이미지가 실제로 쓰는 문진 스키마(리포의 schema/ 패키지)의 버전에서 나온다(U50)."""
    ver = re.search(r'^version = "(\d+)\.\d+\.\d+"', (ROOT / "schema/pyproject.toml").read_text(), re.M).group(1)
    assert SURVEY_SCHEMA_LABEL.match(ver)
    script = (ROOT / "scripts/build-image.sh").read_text()
    assert 'SCHEMA_ACTUAL=$(pkg_version schema openit-survey-schema)' in script and "SURVEY_SCHEMA_MAJOR=${SCHEMA_ACTUAL#v}" in script


def test_dockerfile_does_not_default_labels_to_values_imagelabels_rejects():
    """`unknown` 기본값이 라벨에 남으면 deploy 게시 검사가 거부한다. 빌드 인자를 빠뜨리면 이미지가 만들어지지 않아야 한다."""
    text = (ROOT / "Dockerfile").read_text()
    for arg in ("COMMIT", "SURVEY_SCHEMA_MAJOR", "BOUNDARY_TAG", "MODEL_RUNTIME_TAG"):
        assert not re.search(rf"^ARG {arg}=unknown", text, re.M), arg


def test_smoke_scripts_use_tokens_that_pass_the_minimum_length_rule():
    """smoke.sh가 주는 토큰이 시작 검사(service.MIN_TOKEN_CHARS, ADR-0024)를 통과한다. 이전에는 15자 토큰이라 이미지가 시작하지 못했다."""
    from survey_service.service import MIN_TOKEN_CHARS
    text = (ROOT / "scripts/smoke.sh").read_text()
    literals = set(re.findall(r"SURVEY_[A-Z_]*TOKEN=([A-Za-z0-9._-]+)", text))
    literals |= set(re.findall(r"printf '([A-Za-z0-9._-]+)\\n' > \"\$SECRET_DIR/\w*token\"", text))
    literals |= set(re.findall(r"Bearer (smoke-[A-Za-z0-9-]+)", text))
    assert literals, "토큰 리터럴을 찾지 못했다(시험이 낡았다)"
    short = sorted(t for t in literals if len(t) < MIN_TOKEN_CHARS)
    assert not short, short
