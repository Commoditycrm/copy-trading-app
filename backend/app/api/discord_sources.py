"""Inbound Discord alert sources — Step 1: connection management only.

A trader connects a Discord channel (via their OWN bot token) that Kopyaa will,
in later phases, read trade alerts from and act on. This router ONLY manages the
connection lifecycle: create + verify, list, update/toggle, re-verify, delete.
No message reading, parsing, or order placement lives here.

Separate from the OUTBOUND webhook broadcast (api/settings.py PATCH
/settings/trader + services/discord_alerts.py), which posts the trader's own
fills TO Discord. Different direction, different storage.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_trader
from app.database import get_db
from app.models.discord_alert_source import DiscordAlertSource
from app.models.user import User
from app.schemas.discord import DiscordSourceIn, DiscordSourceOut, DiscordSourceUpdateIn
from app.services.crypto import decrypt_json, encrypt_json
from app.services.discord_reader import DiscordVerifyError, verify_bot_channel

router = APIRouter(prefix="/api/discord-sources", tags=["discord-sources"])


def _get_owned(db: Session, user: User, source_id: uuid.UUID) -> DiscordAlertSource:
    src = db.get(DiscordAlertSource, source_id)
    if src is None or src.user_id != user.id:
        raise HTTPException(404, "not_found")
    return src


@router.get("", response_model=list[DiscordSourceOut])
def list_sources(
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> list[DiscordAlertSource]:
    return list(
        db.execute(
            select(DiscordAlertSource)
            .where(DiscordAlertSource.user_id == user.id)
            .order_by(DiscordAlertSource.created_at.desc())
        ).scalars()
    )


@router.post("", response_model=DiscordSourceOut, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: DiscordSourceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> DiscordAlertSource:
    # Verify the bot token + channel access with Discord BEFORE storing, so a
    # trader can't save a connection that won't work.
    try:
        info = verify_bot_channel(payload.bot_token, payload.channel_id)
    except DiscordVerifyError as exc:
        raise HTTPException(400, f"discord_verify_failed: {exc}")

    src = DiscordAlertSource(
        user_id=user.id,
        label=payload.label.strip(),
        encrypted_credentials=encrypt_json({"bot_token": payload.bot_token.strip()}),
        channel_id=payload.channel_id.strip(),
        channel_name=info.get("channel_name"),
        guild_id=info.get("guild_id"),
        is_enabled=True,
        status="connected",
        last_error=None,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


@router.patch("/{source_id}", response_model=DiscordSourceOut)
def update_source(
    source_id: uuid.UUID,
    payload: DiscordSourceUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> DiscordAlertSource:
    src = _get_owned(db, user, source_id)
    if payload.label is not None:
        src.label = payload.label.strip()
    if payload.is_enabled is not None:
        src.is_enabled = payload.is_enabled
    if payload.bot_token is not None:
        # Rotate the token → re-verify against the existing channel.
        try:
            info = verify_bot_channel(payload.bot_token, src.channel_id)
        except DiscordVerifyError as exc:
            raise HTTPException(400, f"discord_verify_failed: {exc}")
        src.encrypted_credentials = encrypt_json({"bot_token": payload.bot_token.strip()})
        src.channel_name = info.get("channel_name")
        src.guild_id = info.get("guild_id")
        src.status = "connected"
        src.last_error = None
    db.commit()
    db.refresh(src)
    return src


@router.post("/{source_id}/verify", response_model=DiscordSourceOut)
def verify_source(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
) -> DiscordAlertSource:
    """Re-check a stored connection against Discord and update its status."""
    src = _get_owned(db, user, source_id)
    token = decrypt_json(src.encrypted_credentials).get("bot_token", "")
    try:
        info = verify_bot_channel(token, src.channel_id)
        src.status = "connected"
        src.last_error = None
        src.channel_name = info.get("channel_name")
        src.guild_id = info.get("guild_id")
    except DiscordVerifyError as exc:
        src.status = "error"
        src.last_error = str(exc)[:480]
    db.commit()
    db.refresh(src)
    return src


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_trader),
):
    # No `-> None` return annotation on purpose: this module uses
    # `from __future__ import annotations`, which would make FastAPI resolve the
    # annotation into a response model and trip the 204 "no body" assertion.
    src = _get_owned(db, user, source_id)
    db.delete(src)
    db.commit()
