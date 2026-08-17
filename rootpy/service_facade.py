from __future__ import annotations

from typing import Dict, List

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported lazily -- see rootpy/__init__.py
    from .structured_api import StructuredAPI, StructuredService


class RootServiceFacade:

    def __init__(self, api: "StructuredAPI") -> None:
        self._api = api

    def names(self) -> List[str]:
        return self._api.services()

    def describe(self, name: str = None):
        return self._api.describe(name)

    def get(self, name: str) -> "StructuredService":
        return getattr(self._api, name)

    def __getattr__(self, name: str) -> "StructuredService":
        return getattr(self._api, name)

    def __dir__(self):
        return sorted(
            set(super().__dir__())
            | set(self.names())
        )
