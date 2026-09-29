"""Bounded thread-pool fan-out for per-translation HTTP work."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def map_ordered(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[tuple[T, R | None, Exception | None]]:
    """Run fn over items with up to `workers` threads; results keep input order, errors are returned not raised."""
    items = list(items)

    def run(item: T) -> tuple[T, R | None, Exception | None]:
        try:
            return item, fn(item), None
        except Exception as exc:  # noqa: BLE001 - callers report per-item failures
            return item, None, exc

    if workers <= 1 or len(items) <= 1:
        return [run(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(run, items))
