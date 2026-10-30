"""HTTP 서버(survey_service.server.serve가 돌려주는 waitress 서버)를 시작·종료·조회하는 시험 보조."""
import threading

from survey_service.server import close_server


def start_server(h) -> None:
    threading.Thread(target=h.run, daemon=True).start()


def server_port(h) -> int:
    return h.effective_port


def stop_server(h) -> None:
    close_server(h, 10.0)


def connection_limit(h) -> int:
    return h.adj.connection_limit


def listen_backlog(h) -> int:
    return h.adj.backlog
