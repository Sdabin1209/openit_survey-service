"""적재된 릴리스를 들고 있는 레지스트리. Orchestrator에는 ReleaseProvider로 보인다."""
from __future__ import annotations

import threading
from typing import Iterable, Optional

from ..errors import ReleaseUnavailable
from ..ports import LoadedRelease


class ReleaseRegistry:
    def __init__(self) -> None:
        """턴을 처리하는 동안에는 이웃(I4)을 부르지 않는다: 여기에 없는 릴리스는 ReleaseUnavailable이다.
        다시 띄운 뒤 진행 중 세션의 릴리스는 ReleaseLoader.ensure_loaded가 턴 밖에서 미리 적재한다."""
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedRelease] = {}
        self._active: Optional[str] = None

    def register(self, release: LoadedRelease, *, activate: bool = False) -> None:
        with self._lock:
            self._loaded[release.release_id] = release
            if activate:
                self._active = release.release_id

    def activate(self, release_id: str) -> None:
        with self._lock:
            if release_id not in self._loaded:
                raise ReleaseUnavailable("적재되지 않은 릴리스는 활성으로 할 수 없다")
            self._active = release_id

    def active(self) -> Optional[LoadedRelease]:
        with self._lock:
            return self._loaded.get(self._active) if self._active else None

    def active_id(self) -> Optional[str]:
        with self._lock:
            return self._active

    def loaded_ids(self) -> list[str]:
        with self._lock:
            return list(self._loaded)

    def get(self, release_id: str) -> LoadedRelease:
        with self._lock:
            rel = self._loaded.get(release_id)
        if rel is None:
            raise ReleaseUnavailable("릴리스가 적재되어 있지 않다")
        return rel

    def gc(self, in_use: Iterable[str], keep: int) -> list[str]:
        """활성이 아니고 열린 세션이 쓰지 않는 릴리스를 내린다. 그런 릴리스 중 최근 keep개는 남긴다. 내린 ID를 돌려준다."""
        in_use = set(in_use)
        with self._lock:
            candidates = [r for r in self._loaded if r != self._active and r not in in_use]
            drop = candidates[: max(0, len(candidates) - keep)]
            for r in drop:
                del self._loaded[r]
            return drop
