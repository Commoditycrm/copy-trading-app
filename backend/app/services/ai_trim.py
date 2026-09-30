"""AI trimming: an OpenRouter model decides how a Discord position is exited.

The alternative exit ENGINE to the trim ladder, chosen per trader
(``TraderSettings.discord_exit_engine``). With it selected, the auto-trim sweep
calls ``sweep`` here instead of firing ladder rungs, so the two never sell the
same contracts. A Discord author's own exit alert still runs as it always did —
this replaces the automatic engine, not the trader's signal source.

── What the model is allowed to do ──────────────────────────────────────────
It answers with one of four actions, and ``validate`` holds it to them:

  hold        nothing
  trim        sell a percentage of what is still held
  exit        sell everything held
  raise_stop  move the protective stop UP (optionally alongside a trim)

It can never buy, never sell more than is held, never lower a stop, and never
set one at or above the current price (that is not a stop, it is a sale the
broker would refuse). Anything malformed, late, or unreachable is a HOLD. The
reply is advisory input to our own rules, not an instruction.

── When it is asked ─────────────────────────────────────────────────────────
Not every sweep: every 15s per position is ~240 paid calls an hour for a price
that mostly hasn't moved. It is asked on a position's first sweep, then again
once the price has moved ``discord_ai_move_pct`` from the last price it saw,
and never more often than ``discord_ai_min_interval_s``.

── Suggest or execute ───────────────────────────────────────────────────────
``discord_ai_mode``: "suggest" records the decision for the trader to approve
(and supersedes any older one still waiting); "auto" executes it. Either way it
is PAPER unless the trader's Discord live trading is on — the same switch that
gates every other Discord order.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation

log = logging.getLogger(__name__)

ACTIONS = ("hold", "trim", "exit", "raise_stop")

# A suggestion priced off a mark this old is no longer the decision it was.
SUGGESTION_TTL = timedelta(minutes=10)

_CENT = Decimal("0.01")

# Shown against each call on the OpenRouter dashboard.
_TITLE = "Copy Trading - AI trimming"

# OpenRouter structured output: the model must answer in exactly this shape.
DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "sell_pct", "stop_price", "reason"],
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "sell_pct": {
            "type": "number",
            "description": "For trim: percent of the CURRENTLY HELD quantity to sell (1-100). 0 otherwise.",
        },
        "stop_price": {
            "type": ["number", "null"],
            "description": "New protective stop price per share, or null to leave the stop alone.",
        },
        "reason": {"type": "string", "description": "One or two sentences, plain language."},
    },
}

SYSTEM_PROMPT = """You manage the EXIT of one open options position for a trader.
You are called when the price has moved meaningfully. Decide exactly one action:

- "hold": do nothing.
- "trim": sell sell_pct percent of the contracts CURRENTLY held.
- "exit": sell everything held.
- "raise_stop": only move the protective stop up (set stop_price).

You may also set stop_price together with "trim" to protect what remains.

Hard rules enforced after you answer (an answer that breaks them is clipped or
ignored): you can never buy or add; a stop can only move UP, never down, and must
be below the current price; you cannot sell more than is held.

Weigh the gain from entry, how far price has come off its peak, time to expiry,
and the trader's own instructions. Prefer protecting realized gains over hoping
for more when expiry is close. Answer only with the JSON object requested."""


# ── validation ───────────────────────────────────────────────────────────────

@dataclass
class Decision:
    action: str
    sell_qty: Decimal = Decimal(0)
    new_stop: Decimal | None = None
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.sell_qty > 0 or self.new_stop is not None


def _num(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def validate(raw: dict, held: Decimal, mark: Decimal, current_stop: Decimal | None) -> Decision:
    """Hold the model's answer to what it is allowed to do. Never raises."""
    if not isinstance(raw, dict):
        return Decision("hold", notes=["reply was not an object — held"])
    action = raw.get("action")
    reason = str(raw.get("reason") or "")[:600]
    if action not in ACTIONS:
        return Decision("hold", reason=reason, notes=[f"unknown action {action!r} — held"])
    d = Decision(action, reason=reason)

    if action == "exit":
        d.sell_qty = held
    elif action == "trim":
        pct = _num(raw.get("sell_pct"))
        if pct is None or pct <= 0:
            d.notes.append("trim with no sell_pct — nothing sold")
        else:
            if pct > 100:
                d.notes.append(f"sell_pct {pct} capped at 100")
                pct = Decimal(100)
            # Up to a whole contract, like the ladder: 30% of 1 must not be 0.
            qty = (held * pct / Decimal(100)).to_integral_value(rounding=ROUND_CEILING)
            d.sell_qty = min(qty, held)
            if d.sell_qty >= held:
                d.action = "exit"

    stop = _num(raw.get("stop_price"))
    if stop is not None and action != "exit" and d.sell_qty < held:
        stop = stop.quantize(_CENT, rounding=ROUND_DOWN)
        if stop <= 0:
            d.notes.append("stop_price not positive — ignored")
        elif stop >= mark:
            d.notes.append(f"stop {stop} is at or above the price {mark} — ignored")
        elif current_stop is not None and stop <= current_stop:
            d.notes.append(f"stop {stop} would not raise the current {current_stop} — ignored")
        else:
            d.new_stop = stop

    if not d.actionable:
        if d.action != "hold":
            d.notes.append(f"{d.action} had nothing left to do — held")
        d.action = "hold"
    elif d.action == "raise_stop" or (d.action == "hold" and d.new_stop is not None):
        d.action = "raise_stop"
    return d


# ── cadence ──────────────────────────────────────────────────────────────────

def due(last_mark: Decimal | None, last_at: datetime | None, mark: Decimal,
        now: datetime, move_pct: Decimal, min_interval_s: int) -> bool:
    """Whether to ask the model about this position now. Pure."""
    if last_at is None or last_mark is None or last_mark <= 0:
        return True
    if now - last_at < timedelta(seconds=max(0, int(min_interval_s or 0))):
        return False
    moved = abs(mark - last_mark) / last_mark * Decimal(100)
    return moved >= (move_pct if move_pct is not None else Decimal(5))


# ── the model call ───────────────────────────────────────────────────────────

class ModelError(Exception):
    pass


def contract_label(guard) -> str:
    right = (getattr(guard.option_right, "value", guard.option_right) or "").upper()[:1]
    strike = format(Decimal(str(guard.option_strike)), "f").rstrip("0").rstrip(".") \
        if guard.option_strike is not None else ""
    exp = guard.option_expiry.isoformat() if guard.option_expiry else ""
    return f"{guard.symbol} {strike}{right} {exp}".strip()


def build_messages(ts, guard, mark: Decimal, held: Decimal, peak: Decimal | None,
                   history: list, now: datetime) -> list[dict]:
    from app.services import market_hours  # noqa: PLC0415

    entry = guard.entry_price
    gain = ((mark - entry) / entry * Decimal(100)).quantize(_CENT) if entry else None
    dte = (guard.option_expiry - market_hours.now_et().date()).days if guard.option_expiry else None
    state = {
        "contract": contract_label(guard),
        "now_et": market_hours.now_et().strftime("%Y-%m-%d %H:%M"),
        "regular_session_open": market_hours.in_regular_session(),
        "days_to_expiry": dte,
        "entry_price": str(entry) if entry is not None else None,
        "current_price": str(mark),
        "gain_pct_from_entry": str(gain) if gain is not None else None,
        "peak_price_seen": str(peak) if peak is not None else None,
        "contracts_held": str(held),
        "current_stop_price": str(guard.stop_price) if guard.stop_price is not None else None,
        "recent_decisions": [
            {"at": h.created_at.isoformat(timespec="minutes"), "price": str(h.mark),
             "action": h.action, "status": h.status, "reason": h.reason[:200]}
            for h in history
        ],
    }
    user = "Position state:\n" + json.dumps(state, indent=2)
    instructions = (getattr(ts, "discord_ai_instructions", None) or "").strip()
    if instructions:
        user += "\n\nThe trader's instructions (follow them within the hard rules):\n" + instructions[:2000]
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def ask(model: str, messages: list[dict]) -> dict:
    """One OpenRouter chat completion, parsed to the decision dict. Raises ModelError."""
    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    s = get_settings()
    if not s.openrouter_api_key:
        raise ModelError("OPENROUTER_API_KEY is not set on the server")
    try:
        resp = httpx.post(
            f"{s.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {s.openrouter_api_key}",
                # Header values must be plain ASCII; httpx refuses anything else.
                "X-Title": _TITLE,
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 400,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "exit_decision", "strict": True, "schema": DECISION_SCHEMA},
                },
            },
            timeout=s.openrouter_timeout_s,
        )
    except httpx.HTTPError as exc:
        raise ModelError(f"OpenRouter unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise ModelError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"unexpected OpenRouter reply: {resp.text[:300]}") from exc
    return parse_reply(content)


# The model list barely changes; one fetch an hour spares every page load.
_MODELS_TTL_S = 3600
_models_cache: tuple[float, list[dict]] | None = None


def list_models() -> list[dict]:
    """OpenRouter models that can answer in our JSON schema, cheapest-first labels.

    Only models advertising ``structured_outputs`` are offered: ``ask`` sends a
    strict json_schema, and a model without it tends to answer in prose — which
    becomes a HOLD every time. Batch variants are dropped (not real-time).
    Raises ModelError if OpenRouter can't be reached and nothing is cached.
    """
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from app.config import get_settings  # noqa: PLC0415

    global _models_cache
    now = time.monotonic()
    if _models_cache and now - _models_cache[0] < _MODELS_TTL_S:
        return _models_cache[1]
    s = get_settings()
    try:
        resp = httpx.get(f"{s.openrouter_base_url.rstrip('/')}/models", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        if _models_cache:
            return _models_cache[1]     # stale beats empty
        raise ModelError(f"couldn't list OpenRouter models: {exc}") from exc

    models = []
    for m in data:
        mid = m.get("id") or ""
        if not mid or mid.endswith(":batch"):
            continue
        if "structured_outputs" not in (m.get("supported_parameters") or []):
            continue
        pricing = m.get("pricing") or {}
        models.append({
            "id": mid,
            "name": m.get("name") or mid,
            # Per MILLION tokens, which is how prices are quoted everywhere else.
            "prompt_per_m": _per_million(pricing.get("prompt")),
            "completion_per_m": _per_million(pricing.get("completion")),
        })
    models.sort(key=lambda m: m["name"].lower())
    _models_cache = (now, models)
    return models


def _per_million(raw) -> str | None:
    d = _num(raw)
    if d is None or d < 0:
        return None
    return format((d * 1_000_000).quantize(_CENT).normalize(), "f")


def parse_reply(content) -> dict:
    """The JSON object in a reply, tolerating a fenced or chatty wrapper."""
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ModelError(f"no JSON object in reply: {text[:200]}")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ModelError(f"reply was not valid JSON: {text[:200]}") from exc


# ── the sweep ────────────────────────────────────────────────────────────────

_WORKING = ("pending", "submitted", "accepted", "partially_filled", "retry_pending")


def _position_for(positions, guard):
    for p in positions:
        if ((p.symbol or "").upper() == guard.symbol
                and p.option_strike == guard.option_strike
                and p.option_right == guard.option_right
                and p.option_expiry == guard.option_expiry
                and (p.quantity or 0) != 0):
            return p
    return None


def _has_working_exit(db, guard) -> bool:
    """An AI sell for this guard is still at the broker — don't pile another on."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415
    from app.models.order import Order, OrderStatus  # noqa: PLC0415

    working = [OrderStatus(v) for v in _WORKING]
    return db.execute(
        select(Order.id).join(AiTrimDecision, AiTrimDecision.order_id == Order.id).where(
            AiTrimDecision.guard_id == guard.id, Order.status.in_(working),
        ).limit(1)
    ).first() is not None


def _recent(db, guard, n: int = 3) -> list:
    from sqlalchemy import select  # noqa: PLC0415

    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415

    return list(db.execute(
        select(AiTrimDecision).where(AiTrimDecision.guard_id == guard.id)
        .order_by(AiTrimDecision.created_at.desc()).limit(n)
    ).scalars())


def sweep(db, user, ts, live_acct, adapter, guards_, positions, mark_for) -> None:
    """Consider every live guard of one trader. Caller holds the sweep lock."""
    for guard in guards_:
        try:
            pos = _position_for(positions, guard)
            mark = mark_for(positions, guard)
            if pos is None or mark is None or guard.entry_price is None:
                continue
            consider(db, user, ts, live_acct, adapter, guard, pos, mark)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("ai-trim: failed on %s", guard.symbol)


def consider(db, user, ts, live_acct, adapter, guard, pos, mark: Decimal,
             now: datetime | None = None, ask_fn=None):
    """Ask the model about one position if it is due, and act on the answer."""
    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    held = abs(Decimal(str(pos.quantity)))
    history = _recent(db, guard)
    last = history[0] if history else None
    if not due(last.mark if last else None, last.created_at if last else None, mark, now,
               _num(getattr(ts, "discord_ai_move_pct", None)) or Decimal(5),
               getattr(ts, "discord_ai_min_interval_s", 60) or 60):
        return None
    if _has_working_exit(db, guard):
        return None

    peak = max([mark] + [h.mark for h in history])
    model = getattr(ts, "discord_ai_model", None) or "anthropic/claude-sonnet-5.5"
    mode = getattr(ts, "discord_ai_mode", "suggest") or "suggest"
    row = AiTrimDecision(
        user_id=user.id, guard_id=guard.id, symbol=guard.symbol,
        contract=contract_label(guard), model=model, mode=mode,
        mark=mark, entry_price=guard.entry_price, held=held,
        action="hold", status="hold",
    )
    db.add(row)
    try:
        raw = (ask_fn or ask)(model, build_messages(ts, guard, mark, held, peak, history, now))
    except ModelError as exc:
        row.status, row.notes = "error", str(exc)[:1000]
        db.commit()
        log.warning("ai-trim: %s — %s", guard.symbol, exc)
        return row

    decision = validate(raw, held, mark, guard.stop_price)
    row.raw_response = raw if isinstance(raw, dict) else {"reply": str(raw)[:2000]}
    _record(row, decision)
    log.info("ai-trim: %s @ %s — %s (%s)", guard.symbol, mark, decision.action, decision.reason[:120])

    if not decision.actionable:
        db.commit()
        return row
    if mode == "auto":
        execute(db, user, ts, live_acct, adapter, guard, pos, row, decision)
    else:
        _supersede_pending(db, guard, row)
        row.status = "suggested"
    db.commit()
    _publish(user, row)
    return row


def _record(row, decision: Decision) -> None:
    row.action = decision.action
    row.sell_qty = decision.sell_qty
    row.new_stop_price = decision.new_stop
    row.reason = decision.reason
    row.notes = "; ".join(decision.notes) or None


def _supersede_pending(db, guard, keep) -> None:
    from sqlalchemy import update  # noqa: PLC0415

    from app.models.ai_trim_decision import AiTrimDecision  # noqa: PLC0415

    db.execute(
        update(AiTrimDecision)
        .where(AiTrimDecision.guard_id == guard.id, AiTrimDecision.status == "suggested",
               AiTrimDecision.id != keep.id)
        .values(status="superseded")
    )


def execute(db, user, ts, live_acct, adapter, guard, pos, row, decision: Decision) -> None:
    """Carry out a validated decision. Paper unless Discord live trading is on."""
    held = abs(Decimal(str(pos.quantity)))
    row.decided_at = datetime.now(timezone.utc)
    if not getattr(ts, "discord_live_trading", False):
        row.status = "paper"
        row.notes = "; ".join(filter(None, [
            row.notes, "paper mode — nothing sent to the broker",
        ]))
        return

    if decision.sell_qty > 0:
        from app.api.discord_sources import _cancel_stop_order  # noqa: PLC0415
        from app.services import discord_stop_orders  # noqa: PLC0415
        from app.services.pnl_poller import place_exit  # noqa: PLC0415

        # A resting stop reserves its contracts; the broker would refuse the
        # sale. The stop reconciler re-places one sized to what is left.
        if guard.stop_order_id:
            discord_stop_orders.release(db, guard, _cancel_stop_order(db, user))
        qty = min(decision.sell_qty, held)
        order = place_exit(db, user, live_acct, adapter, pos, qty, partial=qty < held)
        row.order_id = getattr(order, "id", None)
    if decision.new_stop is not None:
        guard.stop_price = decision.new_stop
    row.status = "executed"


def approve(db, user, row, positions, adapter, live_acct, ts, mark_for) -> None:
    """Execute a suggestion the trader approved, re-checked against NOW.

    The position may have changed since the model answered — a trim may have
    filled, the stop may have moved — so the stored answer is validated again
    against the current holding before anything is sent.
    """
    from app.models.discord_position_guard import DiscordPositionGuard  # noqa: PLC0415

    if row.status != "suggested":
        raise ValueError(f"this decision is {row.status}, not waiting for approval")
    if datetime.now(timezone.utc) - row.created_at > SUGGESTION_TTL:
        row.status = "expired"
        db.commit()
        raise ValueError("this suggestion is more than 10 minutes old — the price has moved on")
    guard = db.get(DiscordPositionGuard, row.guard_id)
    if guard is None or guard.closed_at is not None:
        raise ValueError("the position's ladder has closed")
    pos = _position_for(positions, guard)
    mark = mark_for(positions, guard)
    if pos is None or mark is None:
        raise ValueError("the position is no longer held")
    held = abs(Decimal(str(pos.quantity)))
    # Re-express the stored decision as a reply and validate it against today.
    replay = {
        "action": row.action,
        "sell_pct": float(min(Decimal(100), row.sell_qty / row.held * 100)) if row.held else 0,
        "stop_price": float(row.new_stop_price) if row.new_stop_price is not None else None,
        "reason": row.reason,
    }
    if row.action == "exit":
        replay["sell_pct"] = 100
    decision = validate(replay, held, mark, guard.stop_price)
    if not decision.actionable:
        row.status = "expired"
        row.notes = "; ".join(filter(None, [row.notes, "nothing left to do at approval"]))
        db.commit()
        raise ValueError("nothing left to do — " + ("; ".join(decision.notes) or "already done"))
    execute(db, user, ts, live_acct, adapter, guard, pos, row, decision)
    if row.status == "executed":
        row.status = "approved"
    db.commit()
    _publish(user, row)


def _publish(user, row) -> None:
    try:
        from app.services import events  # noqa: PLC0415

        events.publish(user.id, {
            "type": "discord.ai_decision", "id": str(row.id), "symbol": row.symbol,
            "action": row.action, "status": row.status,
        })
    except Exception:  # noqa: BLE001
        log.debug("ai-trim: publish failed", exc_info=True)


__all__ = ["ACTIONS", "Decision", "approve", "ask", "consider", "due", "sweep", "validate"]
