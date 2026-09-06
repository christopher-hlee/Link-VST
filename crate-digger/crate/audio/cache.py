"""A small decoded-audio cache.

Chopping is interactive — you nudge the sensitivity and expect the slices to
move immediately. Decoding a five-minute record on every keystroke makes that
feel broken, so keep the last few decodes in memory.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import decode

MAX_ENTRIES = 6


class AudioCache:
    def __init__(self, max_entries: int = MAX_ENTRIES):
        self.max_entries = max_entries
        self._store: OrderedDict[tuple, tuple[np.ndarray, int]] = OrderedDict()
        self._lock = threading.Lock()

    def load(self, path: Path | str, *, sr: int | None = None) -> tuple[np.ndarray, int]:
        path = Path(path)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            stamp = 0
        key = (str(path), stamp, sr)

        with self._lock:
            hit = self._store.get(key)
            if hit is not None:
                self._store.move_to_end(key)
                return hit

        data, rate = decode.load(path, sr=sr, mono=True)

        with self._lock:
            self._store[key] = (data, rate)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)
        return data, rate

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


CACHE = AudioCache()
