"""적재된 릴리스를 들고 있는 레지스트리. Orchestrator에는 ReleaseProvider로 보인다."""
from __future__ import annotations

import threading
from typing import Callable, Iterable, Optional

from ..errors import ReleaseUnavailable
from ..ports import LoadedRelease


class ReleaseRegistry:
    def __init__(self, resolver: Optional[Callable[[str], LoadedRelease]] = None) -> None:
        """resolver: 레지스트리에 없는 릴리스를 다시 적재하는 함수(서비스를 다시 띄운 뒤 진행 중 세션용)."""
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedRelease] = {}
        self._active: Optional[str] = None
        self._resolver = resolver

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
        if rel is not None:
            return rel
        if self._resolver is None:
            raise ReleaseUnavailable("릴리스가 적재되어 있지 않다")
        try:
            rel = self._resolver(release_id)
        except ReleaseUnavailable:
            raise
        except Exception as e:
            raise ReleaseUnavailable(f"릴리스를 다시 적재하지 못했다: {type(e).__name__}") from e
        with self._lock:
            self._loaded[release_id] = rel
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
