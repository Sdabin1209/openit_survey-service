"""이웃 라이브러리 핀의 일관성과 이미지 라벨 규칙.

핀: requirements.txt, scripts/build_wheels.sh의 기본 태그, 설치된 패키지의 버전이 같은 model-runtime을 가리킨다. 한 곳만 올리면 이미지(build_wheels)와
개발 환경(requirements)이 다른 버전이 된다. 기대값의 출처는 설치된 패키지(`model_runtime.RUNTIME_VERSION`)다: 시험이 숫자를 따로 적지 않는다.

라벨: deploy의 ImageLabels(`io.openit.contracts.survey-schema`는 `^([0-9]+|n/a)$`, `unknown`·`survey-schema-v1.0.0` 같은 문자열 불가)를
이 영역의 이미지 라벨 입력(Dockerfile 기본값, build_wheels.sh가 내는 VERSIONS.env)이 지키는지 본다.
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


def _script_tag(var):
    return re.search(rf"^{var}=\$\{{{var}:-(v[0-9.]+)\}}", (ROOT / "scripts/build_wheels.sh").read_text(), re.M).group(1)


def test_model_runtime_pins_agree_with_the_installed_package():
    installed = "v" + model_runtime.RUNTIME_VERSION
    assert _req_tag("model-runtime") == installed
    assert _script_tag("MODEL_RUNTIME_TAG") == installed


def test_other_library_pins_agree_between_requirements_and_build_script():
    assert _req_tag("boundary-contracts") == _script_tag("BOUNDARY_TAG")
    assert "turn-engine" not in (ROOT / "requirements.txt").read_text() and "TURN_ENGINE_TAG" not in (ROOT / "scripts/build_wheels.sh").read_text()   # D109: 코드 의존 없음


def test_schema_label_input_is_an_integer_not_a_tag_string():
    ver = (ROOT / "schema/CONTRACT_VERSION").read_text().strip()
    assert SURVEY_SCHEMA_LABEL.match(ver)
    script = (ROOT / "scripts/build_wheels.sh").read_text()
    assert "SCHEMA_CONTRACT_VERSION=$SCHEMA_INT" in script and "SCHEMA_INT=$(tr -d '[:space:]' < schema/CONTRACT_VERSION)" in script


def test_dockerfile_does_not_default_labels_to_values_imagelabels_rejects():
    """`unknown` 기본값이 라벨에 남으면 deploy 게시 검사가 거부한다. 빌드 인자를 빠뜨리면 이미지가 만들어지지 않아야 한다."""
    text = (ROOT / "Dockerfile").read_text()
    for arg in ("COMMIT", "SCHEMA_CONTRACT_VERSION", "BOUNDARY_TAG", "MODEL_RUNTIME_TAG"):
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
