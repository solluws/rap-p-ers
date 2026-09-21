"""Lazy loading for the generated protocol registries.

The registries are large -- 842 message definitions, 842 raw schemas (the same
key set, one field table per message), 31 services -- and most programs touch
only a handful of them. Storing them as
Python literals meant every field of every message was parsed and built into
dicts before the first request could be sent.

They now live as JSON under ``rootpy/data/``. :class:`LazyRegistry` is a
read-only mapping that behaves exactly like the dict it replaced but defers
reading and decoding the file until something actually looks a key up. Import
stays cheap; the cost is paid once, on first use, by whoever needs it.

``DerivedRegistry`` is the same idea for indexes that can be computed from
another registry rather than stored (see ``SIMPLE_MESSAGES``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping

_DATA_DIR = Path(__file__).resolve().parent / "data"


class LazyRegistry(Mapping):
    """A read-only mapping backed by a JSON file, loaded on first access.

    Implements the full :class:`~collections.abc.Mapping` protocol, so it is a
    drop-in for the plain dicts these registries used to be: subscripting,
    ``in``, ``.get()``, ``.items()``, ``len()`` and iteration all work.
    """

    __slots__ = ("_filename", "_data")

    def __init__(self, filename: str) -> None:
        self._filename = filename
        self._data: Dict[str, Any] | None = None

    def _load(self) -> Dict[str, Any]:
        data = self._data
        if data is None:
            path = _DATA_DIR / self._filename
            try:
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except FileNotFoundError as exc:  # pragma: no cover - packaging bug
                raise RuntimeError(
                    f"rootpy data file {self._filename!r} is missing. The "
                    "package was probably installed without its data files; "
                    "reinstall rootpy."
                ) from exc
            self._data = data
        return data

    # -- Mapping protocol ------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._load()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def __contains__(self, key: object) -> bool:
        return key in self._load()

    def __repr__(self) -> str:
        state = "unloaded" if self._data is None else f"{len(self._data)} entries"
        return f"<LazyRegistry {self._filename} ({state})>"

    # -- conveniences ----------------------------------------------------
    @property
    def loaded(self) -> bool:
        """True once the backing file has been read."""
        return self._data is not None

    def preload(self) -> "LazyRegistry":
        """Force the load now, e.g. to keep it off a latency-sensitive path."""
        self._load()
        return self


class DerivedRegistry(Mapping):
    """A mapping computed from another registry the first time it is used."""

    __slots__ = ("_builder", "_data")

    def __init__(self, builder: Callable[[], Dict[str, Any]]) -> None:
        self._builder = builder
        self._data: Dict[str, Any] | None = None

    def _load(self) -> Dict[str, Any]:
        if self._data is None:
            self._data = self._builder()
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._load()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def __contains__(self, key: object) -> bool:
        return key in self._load()

    def __repr__(self) -> str:
        state = "unloaded" if self._data is None else f"{len(self._data)} entries"
        return f"<DerivedRegistry ({state})>"

    @property
    def loaded(self) -> bool:
        return self._data is not None

    def preload(self) -> "DerivedRegistry":
        self._load()
        return self
