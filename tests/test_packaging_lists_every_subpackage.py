"""pyproject 의 packages 목록이 survey_service 아래 모든 하위 패키지(__init__.py 가 있는 폴더)를 담는다.
types.contract 를 빼먹어 이미지가 시작하자마자 ModuleNotFoundError 로 죽은 일(2026-11-06). 편집 설치(개발 환경)는 소스를 그대로 읽어 드러나지 않는다."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_subpackage_is_listed():
    listed = set(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"])
    found = {str(p.parent.relative_to(ROOT)).replace("/", ".") for p in (ROOT / "survey_service").rglob("__init__.py") if "__pycache__" not in p.parts}
    assert found - listed == set()
