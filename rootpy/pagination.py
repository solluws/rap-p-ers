from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Iterable, List, Optional, TypeVar

T=TypeVar("T")
@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    cursor: Optional[Any] = None
    raw: Any = None
    @property
    def has_more(self) -> bool: return self.cursor is not None

class AsyncPager(Generic[T]):
    def __init__(self, fetch: Callable[[Optional[Any]], Awaitable[Page[T]]], *, max_pages: Optional[int]=None):
        self.fetch=fetch; self.max_pages=max_pages
    async def pages(self):
        cursor=None; count=0
        while True:
            page=await self.fetch(cursor); yield page; count += 1
            if page.cursor is None or (self.max_pages is not None and count >= self.max_pages): return
            cursor=page.cursor
    async def flatten(self) -> List[T]:
        out=[]
        async for page in self.pages(): out.extend(page.items)
        return out
    def __aiter__(self):
        async def gen():
            async for page in self.pages():
                for item in page.items: yield item
        return gen()
