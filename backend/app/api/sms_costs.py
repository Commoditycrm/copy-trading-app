"""Admin → System → SMS costs: Twilio usage and spending (services/sms_costs).

Admin only. Twilio is read from the backend — credentials never reach the
browser — and cached; ?refresh=true is honoured at most every 5 minutes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.database import get_db
from app.models.user import User

router = APIRouter(prefix="/api/admin/system", tags=["admin", "system"])


@router.get("/sms-costs")
def sms_costs(refresh: bool = False, db: Session = Depends(get_db), _: User = Depends(require_admin)) -> dict[str, Any]:
    from app.services import sms_costs as svc  # noqa: PLC0415

    return svc.read(db, refresh=refresh)
