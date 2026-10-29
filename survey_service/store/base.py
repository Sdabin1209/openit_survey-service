"""Session Store 인터페이스. 구현은 SQLite(기본)와 메모리(시험)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class TurnRecord:
    input_hash: str
    committed: bool
    result: Optional[dict]


class SessionStorePort(Protocol):
    def ping(self) -> None:
        """열려 있고 읽을 수 있는지. 안 되면 StoreError."""

    def load(self, session_id: str) -> dict:
        """SessionLoadResult(OK / NOT_FOUND / DB_ERROR). DB 오류는 예외가 아니라 DB_ERROR 값으로."""

    def create(self, session_id: str, release_id: str, state: dict) -> int:
        """새 세션을 만든다. 이미 있거나 쓸 수 없으면 StoreError. 만든 revision을 돌려준다."""

    def commit(self, request: dict, *, turn_id: str, input_hash: str) -> dict:
        """SessionCommitResult. 쓰기 전체를 한 번에 반영하거나 하나도 반영하지 않는다.
        같은 트랜잭션에서 (세션, 턴 ID)를 '반영됨'으로 표시한다."""

    def append_log(self, session_id: str, entry: dict) -> None:
        """대화 로그에 덧붙인다. 실패하면 StoreError."""

    def append_dispatch(self, session_id: str, record: dict) -> None:
        """전달 기록에 덧붙인다. 실패하면 StoreError."""

    def release_id_of(self, session_id: str) -> Optional[str]:
        """세션이 묶인 릴리스 ID."""

    def get_turn(self, session_id: str, turn_id: str) -> Optional[TurnRecord]: ...

    def save_turn_result(self, session_id: str, turn_id: str, input_hash: str, result: dict) -> None:
        """턴 결과를 멱등 기록에 남긴다(세션이 없어도 남길 수 있다)."""

    def list_idle(self, before: float) -> list[str]:
        """마지막 기록이 before(epoch 초)보다 오래된 IN_PROGRESS 세션 ID. 유휴 만료에 쓴다."""

    def list_sessions(self, *, status: Optional[str] = None, updated_after: Optional[float] = None, session_ids: Optional[list] = None,
                      limit: int = 50, after: Optional[tuple] = None) -> list[tuple]:
        """결과 조회용 목록: (session_id, status, updated_at, update_seq) 목록을 update_seq(쓸 때마다 매기는 단조 증가 번호) 오름차순으로 최대 limit개.
        updated_after는 epoch 초(초과만, 시각으로 거르기만 한다), after는 마지막으로 본 update_seq(그보다 큰 것만), session_ids는 후보 제한. 순서에 벽시계를 쓰지 않는다(U26, ADR-0034)."""

    def list_ended_before(self, before: float) -> list[str]:
        """끝난(ENDED_*) 세션 중 마지막 기록이 before(epoch 초)보다 오래된 세션 ID. 보존 기간 정리에 쓴다."""

    def delete_sessions(self, session_ids: list) -> int:
        """세션 문서와 그 턴 기록을 지운다. 지운 세션 수."""

    def open_release_ids(self) -> set[str]:
        """아직 끝나지 않은(진행 중·일시 중지) 세션이 묶인 릴리스 ID 집합."""
