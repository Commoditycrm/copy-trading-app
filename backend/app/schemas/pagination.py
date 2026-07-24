"""Shared pagination envelope for list endpoints.

One consistent shape so the frontend's <Pagination> component / hook can drive
every server-paginated table the same way:

    {"items": [...], "total": 1234, "limit": 50, "offset": 100}

`total` is the count of ALL rows matching the current filters (not just this
page), so the UI can render "showing 101–150 of 1234" and page controls.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
