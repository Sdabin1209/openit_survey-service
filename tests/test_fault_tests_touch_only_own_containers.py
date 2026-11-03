"""L25(ADR-0019): 장애 시험은 자기가 띄운 컨테이너만 이름으로 pause/unpause한다. 같은 서버의 다른 영역이 띄운 개발용 PostgreSQL(`io.openit.dev-postgres=1`)을 라벨로 찾아 멈추지 않는다.

현재 시험에는 docker pause 호출이 없다(U421: 실제 시간·컨테이너 대신 주입한 시계와 가짜 매달림으로 본다). 앞으로 넣는 시험이 옛 방식으로 돌아가지 않게 지킨다.
정적 시험: tests/ 와 scripts/ 안의 코드(주석·문서 문자열 제외)에 그 라벨로 컨테이너를 고르는 호출이 없고, pause/unpause는 pg_container가 낸 이름으로만 부른다.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "io.openit.dev-postgres"


def _string_literals(path: Path):
    """코드 안의 문자열 상수(함수·모듈 docstring은 뺀다)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            doc_ids.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            yield node.value


def test_no_test_selects_containers_by_the_shared_dev_postgres_label():
    hits = []
    for p in sorted(list((ROOT / "tests").glob("*.py")) + list((ROOT / "scripts").glob("*.py"))):
        if p.name == Path(__file__).name:
            continue
        if any(LABEL in lit for lit in _string_literals(p)):
            hits.append(p.name)
    assert hits == []


def test_pause_and_unpause_only_name_containers_the_test_started():
    for p in sorted((ROOT / "tests").glob("*.py")):
        if p.name == Path(__file__).name:
            continue
        for lit in _string_literals(p):
            if lit in ("pause", "unpause"):
                # pause를 부르는 파일은 pg_container로 띄운 이름만 쓴다(이 파일이 이름을 만드는 곳)
                assert "pg_container" in p.read_text(encoding="utf-8") or p.name == "pg_container.py", p.name
