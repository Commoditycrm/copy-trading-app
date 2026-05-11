"""Server-Sent Events stream of order changes for the logged-in user.

EventSource (the browser API) doesn't support custom headers, so we accept the
JWT access token as a `?token=` query param. The token is short-lived (30 min)
and bound to the user, so leakage in a URL is bounded — but for production-
grade hardening, swap to a one-time stream-token issued via an authenticated
POST.
"""
import asyncio
import json
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.core.security import decode_token
from app.database import SessionLocal
from app.models.user import User
from app.services import events

router = APIRouter(prefix="/api", tags=["events"])

HEARTBEAT_SECONDS = 20


@router.get("/events")
async def stream(request: Request, token: str = Query(...)):
    try:
        payload = decode_token(token)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="wrong_token")
    user_id = uuid.UUID(payload["sub"])

    # Validate user is still active.
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user_inactive")

    async def gen():
        # Initial hello so the client knows the stream is alive.
        yield f": connected user={user_id}\n\n"
        feed = events.subscribe(user_id)
        feed_iter = feed.__aiter__()
        next_event_task = asyncio.create_task(feed_iter.__anext__())
        try:
            while True:
                if await request.is_disconnected():
                    break
                done, _ = await asyncio.wait(
                    {next_event_task}, timeout=HEARTBEAT_SECONDS
                )
                if next_event_task in done:
                    try:
                        event = next_event_task.result()
                    except StopAsyncIteration:
                        break
                    yield f"data: {json.dumps(event)}\n\n"
                    next_event_task = asyncio.create_task(feed_iter.__anext__())
                else:
                    # Heartbeat to keep proxies / load balancers from killing the connection.
                    yield ": heartbeat\n\n"
        finally:
            next_event_task.cancel()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
