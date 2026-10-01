"""Per-user UI preferences — currently configurable table columns.

GET returns the whole column-prefs bucket; PUT upserts one table's config
(visibility, order, widths). Kept tiny and generic so new UI prefs need no new
endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.database import get_db
from app.models.ui_prefs import UserUiPrefs
from app.models.user import User

router = APIRouter(prefix="/api/ui-prefs", tags=["ui-prefs"])


class ColumnConfig(BaseModel):
    # Column ids in display order; ids omitted here keep their default slot.
    order: list[str] = Field(default_factory=list)
    # Column ids hidden by the user.
    hidden: list[str] = Field(default_factory=list)
    # Column id → pixel width.
    widths: dict[str, int] = Field(default_factory=dict)
    # Column ids the user has dragged — a "leading" column (Channel) keeps the
    # user's slot instead of being put back first.
    moved: list[str] = Field(default_factory=list)


def _get_row(db: Session, user_id) -> UserUiPrefs | None:
    return db.execute(
        select(UserUiPrefs).where(UserUiPrefs.user_id == user_id)
    ).scalar_one_or_none()


@router.get("/columns")
def get_columns(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    """The user's per-table column config: {tableId: {order, hidden, widths}}."""
    row = _get_row(db, user.id)
    return {"columns": (row.column_prefs if row and row.column_prefs else {})}


@router.put("/columns/{table_id}")
def put_columns(
    table_id: str,
    cfg: ColumnConfig,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Upsert one table's column config. Stored under ``table_id`` in the
    per-user bucket; other tables are untouched."""
    row = _get_row(db, user.id)
    if row is None:
        row = UserUiPrefs(user_id=user.id, column_prefs={})
        db.add(row)
    # Reassign (not mutate in place) so SQLAlchemy flags the JSONB dirty.
    prefs = dict(row.column_prefs or {})
    prefs[table_id] = cfg.model_dump()
    row.column_prefs = prefs
    db.commit()
    return {"ok": True, "columns": row.column_prefs}
