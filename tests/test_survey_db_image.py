"""D126: survey-db 이미지(db/): 선언(DatabaseDeclaration)대로 계정·권한·접속 규칙·TLS가 이미지 안에서 만들어진다. 실제 postgres:16 위에서 컨테이너를 띄워 verify-full로 접속해 본다.
docker 또는 고정 digest의 postgres 이미지(tests/pg_container.py의 IMAGE)가 없으면 건너뛴다(통과로 세지 않는다). 시험이 띄운 컨테이너와 만든 이미지는 이 시험이 지운다."""
import datetime
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from boundary_contracts import validator
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .pg_container import IMAGE as PG_IMAGE      # 고정 digest 한 곳(db/Dockerfile의 FROM과 같은 값)
from .timing import HANG_GUARD_SECONDS, wait_until

ROOT = Path(__file__).resolve().parent.parent
HOST = "survey-db"          # 서버 인증서의 이름(SAN). 접속은 이 이름으로 verify-full, 주소는 hostaddr


def _docker(*a, check=True, timeout=300):
    return subprocess.run(["docker", *a], capture_output=True, text=True, check=check, timeout=timeout)


@pytest.fixture(scope="module")
def have_docker():
    if shutil.which("docker") is None or _docker("image", "inspect", PG_IMAGE, check=False).returncode != 0:
        pytest.skip(f"docker 또는 {PG_IMAGE} 이미지 없음")


def _pki(d: Path):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = lambda: ec.generate_private_key(ec.SECP256R1())
    name = lambda cn: x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    rk, sk = key(), key()
    root = (x509.CertificateBuilder().subject_name(name("db-test-root")).issuer_name(name("db-test-root")).public_key(rk.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(rk, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(name(HOST)).issuer_name(root.subject).public_key(sk.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False).sign(rk, hashes.SHA256()))
    pem = lambda o: o.public_bytes(serialization.Encoding.PEM)
    certs = d / "certs"; certs.mkdir()
    (certs / "tls.crt").write_bytes(pem(leaf))
    (certs / "tls.key").write_bytes(sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (certs / "tls.key").chmod(0o644)      # 마운트된 키가 느슨한 권한이어도 진입점이 복사해 0600으로 만든다
    (d / "root.pem").write_bytes(pem(root))
    return certs, d / "root.pem"


def _build(tmp_path, tag, declaration, mutate=None):
    """임시 컨텍스트에 db/를 복사하고 선언(accounts.tsv)을 만들어 이미지를 빌드한다. mutate(경로)로 초기화 스크립트를 일부러 어길 수 있다."""
    ctx = tmp_path / f"ctx-{tag}"
    shutil.copytree(ROOT / "db", ctx / "db", ignore=shutil.ignore_patterns("accounts.tsv"))
    (ctx / "db" / "database.json").write_text(json.dumps(declaration), encoding="utf-8")
    subprocess.run([sys.executable, str(ctx / "db" / "gen_accounts.py"), str(ctx / "db" / "database.json"), str(ctx / "db" / "accounts.tsv")], check=True)
    if mutate:
        rel, fn = mutate if isinstance(mutate, tuple) else ("initdb.d/10-areadb.sh", mutate)
        fn(ctx / "db" / rel)
    _docker("build", "-q", "-f", str(ctx / "db" / "Dockerfile"), "-t", tag, "--build-arg", "COMMIT=abcdef1", "--build-arg", "BOUNDARY_TAG=v3.5.0",
            "--build-arg", f"DATABASE_DECLARATION={json.dumps(declaration, separators=(',', ':'))}", str(ctx))
    return tag


def _swap(old, new, file=None):
    def mutate(path):
        s = path.read_text(encoding="utf-8"); assert old in s, old
        path.write_text(s.replace(old, new), encoding="utf-8")
    return (file, mutate) if file else mutate


DECL = {"engine": "postgresql", "major": 16, "port": 5432, "accounts": [
    {"name": "svc_owner", "database": "svcdb", "privilege": "owner", "url_env": ["SURVEY_DATABASE_URL_FILE"]},
    {"name": "svc_dml", "database": "svcdb", "privilege": "dml", "url_env": ["SURVEY_DATABASE_URL_FILE"]}]}
PW = {"svc_owner": "owner-pw-canary-1", "svc_dml": "dml-pw-canary-2"}


class Db:
    def __init__(self, tmp_path, image):
        self.certs, self.root = _pki(tmp_path)
        sec = tmp_path / "secrets"; sec.mkdir(); sec.chmod(0o750)
        for n, pw in PW.items():
            (sec / n).write_text(pw + "\n"); (sec / n).chmod(0o640)
        (tmp_path / "pgpw").write_text("superuser-pw-canary-3"); (tmp_path / "pgpw").chmod(0o644)
        self.name = f"survey-service-dbtest-{uuid.uuid4().hex[:8]}"
        _docker("run", "-d", "--name", self.name, "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/run", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                *[v for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID") for v in ("--cap-add", cap)], "-p", "127.0.0.1::5432",   # deploy 와 같은 실행 조건(읽기 전용 루트, /tmp·/run tmpfs)
                 "-v", f"{self.certs}:/certs:ro", "-v", f"{sec}:/run/secrets/db:ro", "-v", f"{tmp_path / 'pgpw'}:/run/secrets/pgpw:ro",
                "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/pgpw", image)
        self.port = None

    def up(self):
        self.port = _docker("port", self.name, "5432/tcp").stdout.split(":")[-1].strip()
        return wait_until(lambda: self._try("svc_owner", "svcdb") is not None, timeout=120)

    def dsn(self, user, db, **kw):
        base = dict(host=HOST, hostaddr="127.0.0.1", port=self.port, user=user, password=PW.get(user, "x"), dbname=db, sslmode="verify-full", sslrootcert=str(self.root), connect_timeout=5)
        base.update(kw)
        return base

    def connect(self, user, db, **kw):
        return psycopg.connect(autocommit=True, **self.dsn(user, db, **kw))

    def _try(self, user, db):
        try:
            return self.connect(user, db)
        except Exception:
            return None

    def close(self):
        _docker("rm", "-f", self.name, check=False)


def check(db: Db):
    """계약 D126 동작 전부를 한 컨테이너에서 단언한다. 어기면 AssertionError."""
    with db.connect("svc_owner", "svcdb") as o:
        o.execute("CREATE TABLE t (id serial PRIMARY KEY, v text)")      # owner는 자기 DB 소유: 스키마 변경 가능
        o.execute("INSERT INTO t(v) VALUES ('x')")
        assert o.execute("SELECT datdba::regrole::text FROM pg_database WHERE datname='svcdb'").fetchone()[0] == "svc_owner"
        assert o.execute("SHOW ssl").fetchone()[0] == "on" and o.execute("SHOW max_connections").fetchone()[0] == "50"
        assert o.execute("SHOW password_encryption").fetchone()[0] == "scram-sha-256"
        attrs = o.execute("SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolcanlogin FROM pg_roles WHERE rolname IN ('svc_owner','svc_dml') ORDER BY 1").fetchall()
        assert attrs == [("svc_dml", False, False, False, False, True), ("svc_owner", False, False, False, False, True)]
        public_connect = o.execute("SELECT count(*) FROM pg_database d, aclexplode(d.datacl) a WHERE d.datname IN ('svcdb','postgres') AND a.grantee = 0 AND a.privilege_type = 'CONNECT'").fetchone()[0]
        assert public_connect == 0                                          # PUBLIC CONNECT 회수
    with db.connect("svc_dml", "svcdb") as d:                               # dml: 표 읽기·쓰기, 시퀀스 사용 (owner가 만든 표에도 기본 권한으로)
        d.execute("INSERT INTO t(v) VALUES ('y')")
        assert d.execute("SELECT count(*) FROM t").fetchone()[0] == 2
        d.execute("UPDATE t SET v = 'z' WHERE id = 1"); d.execute("DELETE FROM t WHERE id = 2")
        for ddl in ("CREATE TABLE u (x int)", "DROP TABLE t", "ALTER TABLE t ADD COLUMN w int", "TRUNCATE t", "CREATE ROLE evil", "CREATE DATABASE evildb"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                d.execute(ddl)
    with db.connect("svc_owner", "svcdb") as o:
        o.execute("CREATE TABLE t2 (id serial PRIMARY KEY, v text)")        # 나중에 만든 표에도 dml의 기본 권한이 따라간다
    with db.connect("svc_dml", "svcdb") as d:
        d.execute("INSERT INTO t2(v) VALUES ('later')")
    for who in ("svc_owner", "svc_dml"):                                    # 선언 밖 데이터베이스에는 붙지 못한다
        with pytest.raises(psycopg.OperationalError):
            db.connect(who, "postgres")
    with pytest.raises(psycopg.OperationalError):                           # 평문 접속은 거절(pg_hba에 평문 host 줄이 없다)
        db.connect("svc_owner", "svcdb", sslmode="disable")
    with pytest.raises(psycopg.OperationalError):                           # 틀린 암호
        db.connect("svc_owner", "svcdb", password="wrong")
    hba = _docker("exec", db.name, "cat", "/var/lib/postgresql/data/pg_hba.conf").stdout
    assert "trust" not in hba and "hostssl" in hba and not any(l.startswith("host ") for l in hba.splitlines())
    r = _docker("exec", db.name, "psql", "-h", "127.0.0.1", "-U", "svc_owner", "-d", "svcdb", "-w", "-c", "SELECT 1", check=False)      # 컨테이너 안 루프백에서 암호 없이: 거절
    assert r.returncode != 0


@pytest.fixture
def running(have_docker, tmp_path):
    made = []

    def make(mutate=None, wait_up=True):
        tag = f"survey-service-dbtest:{uuid.uuid4().hex[:8]}"
        _build(tmp_path, tag, DECL, mutate)
        db = Db(tmp_path / f"run{len(made)}" if (tmp_path / f"run{len(made)}").mkdir() is None else tmp_path, tag)
        made.append((db, tag))
        db.port = _docker("port", db.name, "5432/tcp").stdout.split(":")[-1].strip()
        assert not wait_up or db.up(), _docker("logs", db.name, check=False).stdout[-800:] + _docker("logs", db.name, check=False).stderr[-800:]
        return db
    yield make
    for db, tag in made:
        db.close(); _docker("rmi", "-f", tag, check=False)


def _health(db: Db, want: str, timeout=150):
    """Docker HEALTHCHECK 상태가 want가 될 때까지 본다(시간 상한은 멈춤 방지용이지 측정값이 아니다)."""
    state = lambda: _docker("inspect", "-f", "{{.State.Health.Status}}", db.name).stdout.strip()
    ok = wait_until(lambda: state() == want, timeout=timeout)
    return ok, state()


def test_declared_accounts_privileges_tls_and_access_rules_on_a_real_container(running):
    db = running()
    check(db)
    ok, st = _health(db, "healthy")
    assert ok, st                                  # HEALTHCHECK: 선언 계정으로 TLS 접속이 된다


@pytest.mark.parametrize("mutation", [
    _swap("ssl=on", "ssl=off", "entrypoint.sh"),                                  # 서버 TLS가 꺼지면 hostssl 줄이 맞지 않아 접속이 안 된다
    _swap("sslmode=require", "sslmode=disable", "healthcheck.sh"),               # 검사가 TLS를 안 쓰면 pg_hba(hostssl)가 거절한다
    _swap('"$SECRETS/$name"', '"$SECRETS/$name.nope"', "healthcheck.sh"),         # 선언 계정의 암호를 못 읽으면 접속이 안 된다
], ids=["tls-off", "check-without-tls", "check-cannot-read-secret"])
def test_healthcheck_turns_unhealthy_when_the_connection_check_breaks(running, mutation):
    db = running(mutation, wait_up=False)
    ok, st = _health(db, "unhealthy", timeout=240)
    assert ok, st




@pytest.mark.parametrize("mutation", [
    _swap("NOSUPERUSER", "SUPERUSER"),                               # 계정이 슈퍼유저
    _swap("hostssl all   all        0.0.0.0/0     scram-sha-256", "host    all   all        0.0.0.0/0     trust"),      # 평문 + trust
    _swap("REVOKE ALL ON DATABASE %I FROM PUBLIC", "SELECT 1 -- %I"),      # PUBLIC CONNECT 회수 제거
    _swap("GRANT USAGE, SELECT ON SEQUENCES TO %I", "GRANT SELECT ON TABLES TO %I"),      # dml 시퀀스 기본 권한 제거: serial 열 INSERT가 거절된다
], ids=["superuser", "plaintext-trust", "public-connect", "dml-sequence"])
def test_mutated_initialisation_is_caught_by_the_checks(running, mutation):
    db = running(mutation)
    with pytest.raises((AssertionError, psycopg.Error, pytest.fail.Exception)):
        check(db)


def test_build_image_variant_db_labels_and_declaration(have_docker, tmp_path):
    """scripts/build-image.sh 변형 db: 요청을 BuildImageRequest로 검증하고, 라벨 io.openit.database가 DatabaseDeclaration을 지키며 service/revision/boundary 라벨이 붙는다."""
    import importlib.metadata as m
    if not (ROOT / ".venv/bin/python").exists():
        pytest.skip(".venv 없음")
    tag = f"survey-service-db:test-{uuid.uuid4().hex[:8]}"
    req = {"source_dir": str(ROOT), "revision": "abcdef1", "versions": {"boundary": "v" + m.version("boundary-contracts")}, "variant": "db", "image_tag": tag}
    (tmp_path / "req.json").write_text(json.dumps(req))
    try:
        r = subprocess.run(["bash", str(ROOT / "scripts/build-image.sh"), str(tmp_path / "req.json")], capture_output=True, text=True, cwd=ROOT, timeout=600)
        assert r.returncode == 0, r.stderr[-800:]
        out = json.loads(r.stdout.strip().splitlines()[-1])
        assert list(validator("BuildImageResult").iter_errors(out)) == []
        labels = out["labels"]
        assert labels["io.openit.service"] == "survey-db" and labels["io.openit.variant"] == "db" and labels["io.openit.revision"] == "abcdef1"
        decl = json.loads(labels["io.openit.database"])
        assert list(validator("DatabaseDeclaration").iter_errors(decl)) == [] and decl["accounts"][0]["privilege"] == "owner" and decl["accounts"][0]["url_env"] == ["SURVEY_DATABASE_URL_FILE"]
        assert list(validator("ImageLabels").iter_errors(labels)) == []
        bad = subprocess.run(["bash", str(ROOT / "scripts/build-image.sh"), str(tmp_path / "req.json")], capture_output=True, text=True, cwd=ROOT, timeout=60,
                             env={**os.environ, "DOCKER": "false"})
        assert bad.returncode != 0
    finally:
        _docker("rmi", "-f", tag, check=False)


def test_declaration_must_have_an_owner_for_every_database():
    from importlib import util
    spec = util.spec_from_file_location("gen_accounts", ROOT / "db" / "gen_accounts.py"); mod = util.module_from_spec(spec); spec.loader.exec_module(mod)
    bad = {"engine": "postgresql", "major": 16, "port": 5432, "accounts": [{"name": "a", "database": "d", "privilege": "dml", "url_env": ["X_FILE"]}]}
    with pytest.raises(SystemExit):
        mod.rows(bad)
    assert mod.rows(json.loads((ROOT / "db" / "database.json").read_text(encoding="utf-8"))) == [("survey", "survey", "owner")]
