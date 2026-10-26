"""빌드할 때 소스 트리의 schemas/, CONTRACT_VERSION, COMPONENTS.yaml을 survey_schema/v<정수 버전>/ 아래로 복사한다.

원본은 이 디렉터리의 schemas/ 하나뿐이다(복사본을 리포지토리에 두지 않는다). 새 정수 버전 N을 더할 때는
schema/vN/ 디렉터리(schemas/, CONTRACT_VERSION, COMPONENTS.yaml)를 만든다. 빌드는 둘 다 찾아 포함한다.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = Path(__file__).resolve().parent


def _version_sources():
    """(정수 버전, 소스 디렉터리) 목록. 현행 버전은 schema/ 자체, 그 밖의 버전은 schema/v<N>/."""
    out = {}
    current = int((HERE / "CONTRACT_VERSION").read_text(encoding="utf-8").strip())
    out[current] = HERE
    for d in sorted(HERE.glob("v[0-9]*")):
        if (d / "CONTRACT_VERSION").exists():
            out[int((d / "CONTRACT_VERSION").read_text(encoding="utf-8").strip())] = d
    return out


class BuildPy(build_py):
    def run(self):
        super().run()
        for ver, src in _version_sources().items():
            dest = Path(self.build_lib) / "survey_schema" / f"v{ver}"
            if dest.exists():
                shutil.rmtree(dest)
            (dest / "schemas").mkdir(parents=True)
            for f in sorted((src / "schemas").glob("*.json")):
                shutil.copy2(f, dest / "schemas" / f.name)
            if (src / "data").is_dir():
                (dest / "data").mkdir()
                for f in sorted((src / "data").glob("*.json")):
                    shutil.copy2(f, dest / "data" / f.name)
            for name in ("CONTRACT_VERSION", "COMPONENTS.yaml"):
                if (src / name).exists():
                    shutil.copy2(src / name, dest / name)


setup(cmdclass={"build_py": BuildPy})
