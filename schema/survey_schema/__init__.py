"""openit 문진 스키마 로더.

정수 버전(문진 스키마의 CONTRACT_VERSION)별로 스키마를 낸다. 여러 정수 버전을 동시에 지원하는 소비자는
`load_schema(name, version=N)`으로 버전마다 얻는다. 이 패키지가 설치되어 있으면 리포지토리 사본이 필요 없다.

- 설치본(wheel): survey_schema/v<N>/{schemas/*.json, CONTRACT_VERSION, COMPONENTS.yaml}
- 소스 트리(개발, `pip install -e`): schema/ (현행 버전) 또는 schema/v<N>/
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

__all__ = ["load_schema", "schema_dir", "contract_version_dir", "available_versions", "current_version",
           "list_schemas", "DEFAULT_VERSION"]

DEFAULT_VERSION = 1
_PKG = Path(__file__).resolve().parent
_SOURCE_ROOT = _PKG.parent  # 소스 트리에서는 schema/


def _read_int(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").strip())


@lru_cache(maxsize=None)
def _version_map() -> dict[int, Path]:
    """정수 버전 -> 그 버전의 디렉터리(schemas/와 CONTRACT_VERSION이 있는 곳)."""
    found: dict[int, Path] = {}
    for d in _PKG.glob("v[0-9]*"):  # 설치본
        if (d / "schemas").is_dir() and (d / "CONTRACT_VERSION").exists():
            found[_read_int(d / "CONTRACT_VERSION")] = d
    if (_SOURCE_ROOT / "schemas").is_dir() and (_SOURCE_ROOT / "CONTRACT_VERSION").exists():  # 소스 트리
        found.setdefault(_read_int(_SOURCE_ROOT / "CONTRACT_VERSION"), _SOURCE_ROOT)
        for d in _SOURCE_ROOT.glob("v[0-9]*"):
            if (d / "schemas").is_dir() and (d / "CONTRACT_VERSION").exists():
                found.setdefault(_read_int(d / "CONTRACT_VERSION"), d)
    return found


def available_versions() -> list[int]:
    return sorted(_version_map())


def current_version() -> int:
    """이 패키지가 낸 가장 높은 정수 버전."""
    return max(_version_map())


def contract_version_dir(version: int = DEFAULT_VERSION) -> Path:
    """그 버전의 디렉터리. schemas/, CONTRACT_VERSION, COMPONENTS.yaml이 있다."""
    try:
        return _version_map()[int(version)]
    except KeyError:
        raise KeyError(f"지원하지 않는 문진 스키마 정수 버전: {version} (있는 것: {available_versions()})") from None


def schema_dir(version: int = DEFAULT_VERSION) -> Path:
    """<계약명>.json이 있는 디렉터리. model-runtime의 load(schema_dir=...)에 그대로 넘길 수 있다(상위에 CONTRACT_VERSION)."""
    return contract_version_dir(version) / "schemas"


def list_schemas(version: int = DEFAULT_VERSION) -> list[str]:
    return sorted(p.stem for p in schema_dir(version).glob("*.json"))


def load_schema(name: str, version: int = DEFAULT_VERSION) -> dict:
    """계약 이름(예: 'TurnTrace')의 JSON Schema(standalone, 내부 $ref만 가진다)."""
    path = schema_dir(version) / f"{name}.json"
    if not path.exists():
        raise KeyError(f"알 수 없는 문진 스키마 계약: {name} (정수 버전 {version})")
    return json.loads(path.read_text(encoding="utf-8"))
