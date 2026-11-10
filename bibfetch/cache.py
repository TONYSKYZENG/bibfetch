"""A tiny, dependency-free JSON cache for API responses."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class JsonCache:
    """Persist API responses on disk between runs.

    Keys are hashed so that long OpenAlex filter URLs do not bloat the file.
    The cache is disabled cleanly when ``enabled`` is false or the path is
    ``None``.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        ttl_days: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled and path is not None
        self.path = Path(path).expanduser() if path is not None else None
        self.ttl_seconds = max(0.0, float(ttl_days)) * 86400.0
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False

        if not self.enabled or self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            if isinstance(entries, dict):
                self._entries = entries
        except (OSError, ValueError, TypeError):
            self._entries = {}

    @staticmethod
    def _hash(namespace: str, key: str) -> str:
        digest = hashlib.sha256(f"{namespace}\0{key}".encode("utf-8")).hexdigest()
        return f"{namespace}:{digest}"

    def get(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        hashed = self._hash(namespace, key)
        item = self._entries.get(hashed)
        if not isinstance(item, dict):
            return None
        stored_at = item.get("stored_at", 0)
        try:
            stored_at = float(stored_at)
        except (TypeError, ValueError):
            return None
        if self.ttl_seconds and time.time() - stored_at > self.ttl_seconds:
            self._entries.pop(hashed, None)
            self._dirty = True
            return None
        return item.get("value")

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self._entries[self._hash(namespace, key)] = {
            "stored_at": time.time(),
            "value": value,
        }
        self._dirty = True

    def flush(self) -> None:
        """Atomically write pending changes to disk."""

        if not self.enabled or self.path is None or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "entries": self._entries},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        self._dirty = False
