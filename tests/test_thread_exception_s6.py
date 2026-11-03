"""S6: 서버 루프 스레드의 종료 경쟁(닫힌 소켓에서 select가 EBADF)이 스레드 예외 경고를 만들고, run_server가 그것을 없앤다는 것을 결정적으로 보인다.

select를 가짜로 바꿔 EBADF를 항상 일으킨다(타이밍에 기대지 않는다). 안쪽 시험은 별도 pytest 프로세스에서 `-W error::pytest.PytestUnhandledThreadExceptionWarning`로 돌려
경고가 나는지(옛 경로: waitress의 run을 그대로 스레드로) 안 나는지(run_server)를 종료 코드로 본다."""
import subprocess
import sys
import textwrap

INNER = textwrap.dedent('''
    import errno, select, threading
    from waitress import create_server
    from survey_service.server import run_server


    def test_loop_thread_with_closed_socket(monkeypatch):
        h = create_server(lambda e, s: (s("200 OK", []), [b"x"])[1], host="127.0.0.1", port=0)
        h._survey_closed = True                     # close_server가 닫기를 요청한 뒤다
        def bad_select(*a, **k):
            raise OSError(errno.EBADF, "Bad file descriptor")
        monkeypatch.setattr(select, "select", bad_select)
        target = h.run if "{variant}" == "old" else (lambda: run_server(h))
        t = threading.Thread(target=target)
        t.start(); t.join(10)
        assert not t.is_alive()
        monkeypatch.undo()
        h.close()
''')


def run_inner(tmp_path, variant):
    f = tmp_path / f"test_inner_{variant}.py"
    f.write_text(INNER.replace("{variant}", variant), encoding="utf-8")
    return subprocess.run([sys.executable, "-m", "pytest", str(f), "-q", "-p", "no:cacheprovider", "-W", "error::pytest.PytestUnhandledThreadExceptionWarning"],
                          capture_output=True, text=True, cwd=str(tmp_path))


def test_old_loop_path_raises_the_thread_exception_warning(tmp_path):
    r = run_inner(tmp_path, "old")
    assert r.returncode != 0 and "PytestUnhandledThreadExceptionWarning" in r.stdout + r.stderr and "Bad file descriptor" in r.stdout + r.stderr


def test_run_server_path_has_no_thread_exception(tmp_path):
    r = run_inner(tmp_path, "new")
    assert r.returncode == 0, r.stdout + r.stderr
