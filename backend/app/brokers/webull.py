"""Direct Webull (OpenAPI) adapter — real-time signal AND subscriber execution.

Talks to Webull's official OpenAPI (``webull-openapi-python-sdk``) for a
single account the USER owns, authenticated with that account owner's
``app_key``/``app_secret``.

Two roles, one adapter
----------------------
* **Trader** — the real-time fill SIGNAL. A master trader connects their Webull
  account directly and we stream their fills over gRPC in ~seconds (vs
  SnapTrade's minutes); see ``services.webull_listener``. Uses the reads:
  ``verify_connection``, ``get_positions``, ``get_balance_snapshot``,
  ``get_pnl_snapshot``.
* **Subscriber** — mirror EXECUTION, with no SnapTrade in the path. The copy
  engine places, polls and cancels the subscriber's mirror orders through
  ``place_order`` / ``get_order`` / ``cancel_order`` here, and
  ``services.webull_subscriber_reconciler`` syncs their fills every 30s
  (subscribers get no live listener — those are trader-only).

``get_positions`` is load-bearing for BOTH roles: it is the broker-side truth
the close path re-clamps against. See its docstring for why an option row
without resolvable contract terms is skipped rather than returned.

Gating
------
Inert unless ``settings.webull_direct_enabled`` is true: ``adapter_for``
only routes ``BrokerName.WEBULL`` here when the flag is on, and no such
accounts exist until a user connects one. Default OFF ⇒ zero change to
existing SnapTrade/Alpaca/IBKR behaviour.

Credentials shape (Fernet-encrypted in ``broker_accounts.encrypted_credentials``)::

    {
      "app_key":    "<the account owner's Webull app key>",
      "app_secret": "<the account owner's Webull app secret>",
      "account_id": "<Webull account_id, NOT the account number>",
      "region_id":  "us"
    }

The Webull SDK is imported LAZILY inside methods, so importing this module
never requires the SDK to be installed — only environments that actually
use direct Webull need it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerPosition,
    ConnectionInfo,
)
from app.models.order import (
    InstrumentType,
    OptionRight,
    OrderSide,
    OrderStatus,
    OrderType,
)

log = logging.getLogger(__name__)

# Webull order_status → our OrderStatus. The SDK's canonical set is
# SUBMITTED / PARTIAL_FILLED / FILLED / CANCELLED / FAILED (trade/common/
# order_status.py); the extra keys are defensive against REST/stream variants
# (matches services.webull_listener._WEBULL_STATUS).
_STATUS_MAP: dict[str, OrderStatus] = {
    "SUBMITTED": OrderStatus.SUBMITTED,
    "PENDING": OrderStatus.SUBMITTED,
    "PENDING_SUBMIT": OrderStatus.SUBMITTED,
    "WORKING": OrderStatus.ACCEPTED,
    "ACCEPTED": OrderStatus.ACCEPTED,
    "QUEUED": OrderStatus.ACCEPTED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "CANCELED": OrderStatus.CANCELED,
    "FAILED": OrderStatus.REJECTED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}

_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
})


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _first_dict(d: Any, *keys: str) -> dict:
    """First element of whichever of ``keys`` holds a non-empty list of dicts
    (or the dict itself if the key holds one). ``{}`` when nothing matches — so
    a following ``_first(...)`` on the result is always safe."""
    if not isinstance(d, dict):
        return {}
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v[0]
    return {}


# ── option-contract terms on a position row ─────────────────────────────────
# Webull's /openapi/assets/positions returns option holdings with the contract
# terms EITHER as flat fields on the row, OR nested under an option/contract
# object, OR encoded only in the symbol (OCC). We try all three, in that order,
# because getting these wrong is not cosmetic: every close/trim the copy engine
# places on a subscriber's Webull account is matched against
# (symbol, expiry, strike, right) by ``order_retry.live_closeable_quantity``.
# A position whose terms are None matches NOTHING, the broker reads as flat, and
# the mirror close is dropped as a "dangling entry" — the subscriber is stranded
# holding a contract the trader has already exited. So parse defensively and,
# when we truly cannot resolve the terms, say so loudly (see get_positions).

# Nested containers a Webull option row may hide its contract terms in.
_OPTION_CONTAINER_KEYS = ("option", "option_info", "optionInfo", "contract",
                          "option_contract", "derivative", "instrument")

# OCC 21-char option symbol, e.g. AAPL250620C00220000. Spaces are stripped
# before matching, so the padded form ("AAPL  250620C00220000") also lands here.
_OCC_RE = re.compile(r"^([A-Z][A-Z.]{0,5})(\d{6})([CP])(\d{8})$")


def _parse_occ(s: str) -> tuple[str, date, Decimal, OptionRight] | None:
    """OCC option symbol → (underlying root, expiry, strike, right), or None."""
    m = _OCC_RE.match(str(s or "").upper().replace(" ", ""))
    if not m:
        return None
    root, yymmdd, cp, strike_str = m.groups()
    try:
        expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    return (
        root,
        expiry,
        Decimal(strike_str) / Decimal(1000),
        OptionRight.CALL if cp == "C" else OptionRight.PUT,
    )


def _looks_like_occ(s: str) -> bool:
    return bool(_OCC_RE.match(str(s or "").upper().replace(" ", "")))


def _as_date(v: Any) -> date | None:
    """Webull expiry values arrive as 'YYYY-MM-DD', a full ISO timestamp, an
    8-digit 'YYYYMMDD', or an epoch-millis int. Accept all of them."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit() and len(v) > 8):
        # Epoch seconds or millis.
        try:
            ts = float(v)
            if ts > 1e11:       # millis
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    s = str(v).strip()
    if len(s) == 8 and s.isdigit():          # YYYYMMDD
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _as_right(v: Any) -> "OptionRight | None":
    r = str(v or "").strip().upper()
    if r.startswith("C"):
        return OptionRight.CALL
    if r.startswith("P"):
        return OptionRight.PUT
    return None


def _option_terms(
    row: dict, symbol: str
) -> tuple[str | None, date | None, Decimal | None, "OptionRight | None"]:
    """Resolve (underlying root, expiry, strike, right) for one option position.

    Looks in three places, preferring the most explicit:
      1. flat fields on the position row;
      2. a nested option/contract object (see ``_OPTION_CONTAINER_KEYS``);
      3. the OCC symbol itself, which also supplies the underlying root.

    Any element may come back None — the caller decides what to do about it.
    """
    # Candidate dicts: the row itself, then any nested option container.
    sources: list[dict] = [row]
    for key in _OPTION_CONTAINER_KEYS:
        v = row.get(key)
        if isinstance(v, dict):
            sources.append(v)
        elif isinstance(v, list):
            sources.extend(x for x in v if isinstance(x, dict))
    # Some responses wrap the real leg in items/legs.
    for key in ("items", "legs", "option_legs"):
        v = row.get(key)
        if isinstance(v, list):
            sources.extend(x for x in v if isinstance(x, dict))

    expiry = strike = right = root = None
    for src in sources:
        if expiry is None:
            expiry = _as_date(_first(
                src, "option_expire_date", "optionExpireDate", "expiration_date",
                "expirationDate", "expire_date", "expireDate", "exp_date", "expiry",
            ))
        if strike is None:
            # option_exercise_price is what Webull ACTUALLY sends on a position
            # leg — confirmed against a live account (2026-09-18). The
            # strike_price spellings below are the ORDER-side naming, which is
            # what _build_option_order writes; they are kept because the same
            # helper reads order-shaped payloads too.
            strike = _dec(_first(
                src, "option_exercise_price", "optionExercisePrice",
                "exercise_price", "exercisePrice",
                "strike_price", "strikePrice", "strike",
            ))
        if right is None:
            right = _as_right(_first(
                src, "option_type", "optionType", "option_right", "put_call",
                "putCall", "call_or_put", "right",
            ))
        if root is None:
            r = _first(src, "underlying_symbol", "underlyingSymbol",
                       "unsymbol", "under_symbol", "root_symbol")
            if r:
                root = str(r).upper()

    # OCC fallback — fills in whatever the explicit fields didn't give us, and
    # is the only source of the underlying root when Webull reports the option's
    # own ticker as `symbol`.
    occ = _parse_occ(symbol)
    if occ is None:
        for key in ("option_symbol", "optionSymbol", "occ_symbol", "ticker",
                    "instrument_symbol"):
            occ = _parse_occ(str(row.get(key) or ""))
            if occ:
                break
    if occ is not None:
        occ_root, occ_exp, occ_strike, occ_right = occ
        root = root or occ_root
        expiry = expiry or occ_exp
        strike = strike if strike is not None else occ_strike
        right = right or occ_right

    return root, expiry, strike, right


def _suppress_sdk_file_logger(api_client: Any) -> None:
    """Stop the Webull SDK from creating ``./webull_trade_sdk.log``.

    ``TradeClient.__init__`` calls ``_init_logger`` which attaches a
    ``TimedRotatingFileHandler`` writing that file in the process CWD — but our
    hardened container runs with a read-only root filesystem, so the write dies
    with ``[Errno 30] Read-only file system`` and takes the connect/verify call
    down with it. ``_init_logger`` only sets up its handlers when BOTH
    ``_stream_logger_set`` and ``_file_logger_set`` are falsy, so pre-marking one
    True makes it skip file logging entirely. We use our own logging anyway, and
    this also avoids the SDK's unbounded log growth + app_key-in-file leak.
    """
    try:
        api_client._stream_logger_set = True  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def _is_writable_dir(path: str) -> bool:
    """True if ``path`` exists (or can be created) and is writable."""
    try:
        os.makedirs(path, exist_ok=True)
        return os.access(path, os.W_OK)
    except Exception:  # noqa: BLE001
        return False


def _resolve_token_base() -> str:
    """The base dir the SDK persists Webull tokens under. Prefers
    WEBULL_OPENAPI_TOKEN_DIR (default ``/data/webull_token`` — a durable volume
    in the prod container), but falls back to a writable temp dir when that base
    can't be written. Without the fallback, running direct Webull OUTSIDE the
    container (localhost / bare metal, where ``/data`` is on the read-only root)
    dies with ``ERROR_STORAGE_TOKEN [Errno 30] Read-only file system: '/data'``
    the moment the SDK tries to store a token. Prod is unaffected — ``/data`` is
    writable there, so the configured base is used as-is.
    """
    base = os.getenv("WEBULL_OPENAPI_TOKEN_DIR", "/data/webull_token")
    if _is_writable_dir(base):
        return base
    fallback = os.path.join(tempfile.gettempdir(), "webull_token")
    log.warning(
        "webull token dir %r is not writable; falling back to %r. Set "
        "WEBULL_OPENAPI_TOKEN_DIR to a durable writable path (a mounted volume) "
        "in production so tokens survive restarts.",
        base, fallback,
    )
    return fallback


def set_per_account_token_dir(api_client: Any, app_key: str | None) -> None:
    """Give each app_key its OWN token file so multiple Webull accounts don't
    collide. The SDK saves the verified token under a FIXED filename
    (``token.txt``) in one directory — so a second account (different app_key)
    loads the FIRST account's token → ``417 INVALID_TOKEN``. We point each
    app_key at its own subdirectory under the (durable) base token dir.
    ``set_token_dir`` takes priority over the WEBULL_OPENAPI_TOKEN_DIR env var.
    """
    base = _resolve_token_base()
    key_hash = hashlib.blake2b((app_key or "").encode(), digest_size=8).hexdigest()
    target = f"{base.rstrip('/')}/{key_hash}"
    # Pre-create the per-key dir so the SDK's write lands in an existing,
    # writable directory (some SDK versions don't makedirs before writing).
    try:
        os.makedirs(target, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        api_client.set_token_dir(target)
    except Exception:  # noqa: BLE001
        pass


# Cached TradeClient per app_key. TradeClient.__init__ runs the SDK's token
# flow (Create Token → 2FA). A new adapter is built per request (balance poll
# every ~30s, connect, close_reconciler), so building a fresh client each time
# re-ran the token flow and — during the pre-verify window — produced a fresh
# 2FA prompt every ~30s. We reuse ONE client per app_key so the token flow runs
# once per TTL; once the trader verifies (token → NORMAL, persisted on the
# shared volume) every path loads that token and never re-prompts. Mirrors
# services.webull_listener._webull_trade_client.
_TRADE_CLIENT_TTL_S = 1800.0
_trade_clients: dict[str, Any] = {}          # app_key -> (client, built_at)
_trade_client_lock = threading.Lock()


def trade_client_for(app_key: str, app_secret: str, region_id: str = "us") -> Any:
    """THE cached Webull TradeClient for an app_key — the adapter and the trader
    listener both go through here.

    They used to keep separate caches of the same thing, which meant the token
    flow ran twice per app_key per TTL and there were two places to reason about
    auth. Every extra run is a real cost: ``TradeClient.__init__`` calls Webull's
    auth endpoint, and hammering it is what produced repeated 2FA challenges and
    eventually a VERIFY_FAILURE_EXCEED_LIMIT lockout that stopped order detection
    outright.
    """
    from webull.core.client import ApiClient          # noqa: PLC0415
    from webull.trade.trade_client import TradeClient  # noqa: PLC0415

    now = time.monotonic()
    with _trade_client_lock:
        cached = _trade_clients.get(app_key)
        if cached is not None and (now - cached[1]) < _TRADE_CLIENT_TTL_S:
            return cached[0]
        from app.config import get_settings  # noqa: PLC0415
        _s = get_settings()
        api_client = ApiClient(
            app_key, app_secret, region_id,
            # Don't inherit the SDK's 5-minute block waiting for the owner to
            # authorise the token — see the config comment. Fail fast; every
            # caller here retries.
            token_check_duration_seconds=_s.webull_token_check_duration_seconds,
            token_check_interval_seconds=_s.webull_token_check_interval_seconds,
        )
        _suppress_sdk_file_logger(api_client)
        set_per_account_token_dir(api_client, app_key)   # isolate token per app_key
        client = TradeClient(api_client)   # token flow runs HERE — once per TTL
        _trade_clients[app_key] = (client, now)
        return client


def invalidate_trade_client(app_key: str | None) -> None:
    """Drop the cached client so the next call rebuilds it (re-auths). Call only
    on AUTH failures — NOT on 429s: a throttle means throttled, not bad auth, and
    rebuilding would re-hit the token endpoint and make it worse."""
    with _trade_client_lock:
        _trade_clients.pop(app_key, None)

# Cached DataClient per app_key, same rationale as the TradeClient above (its
# __init__ also runs ClientInitializer/token setup).
_data_clients: dict[str, Any] = {}           # app_key -> (client, built_at)
_data_client_lock = threading.Lock()

# Market data is a SEPARATE Webull entitlement from trading: an app_key approved
# for the Trading API is not necessarily approved for quotes. When a quote call
# fails for a reason that won't fix itself (403 / no permission / not
# subscribed), we stop asking for a while instead of spending a failed HTTP call
# per mirror on every fanout — a 50-subscriber option close would otherwise fire
# 50 pointless requests and 50 log lines. Transient failures are NOT suppressed;
# only entitlement-shaped ones.
_QUOTE_DISABLED_TTL_S = 300.0
_quotes_disabled_until: dict[str, float] = {}   # app_key -> monotonic deadline
_quotes_disabled_lock = threading.Lock()

_ENTITLEMENT_MARKERS = (
    "permission", "not subscribed", "no subscription", "unauthorized",
    "forbidden", "not authorized", "not entitled", "access denied",
    " 403", "403,", "status: 403",
)


def _looks_like_missing_entitlement(msg: str) -> bool:
    m = msg.lower()
    return any(k in m for k in _ENTITLEMENT_MARKERS)


class WebullAdapter(BrokerAdapter):
    """One instance per Webull BrokerAccount. Credentials held in-memory only."""

    name = "webull"

    # A Webull MARKET order is pinned to the CORE session (see _session), so it
    # cannot trade pre/post-market — it just queues for the open. The copy engine
    # re-routes such mirrors as flagged marketable limits.
    requires_extended_hours_limit = True

    def __init__(self, credentials: dict[str, Any]):
        super().__init__(credentials)
        self.app_key = credentials.get("app_key")
        self.app_secret = credentials.get("app_secret")
        self.account_id = credentials.get("account_id")
        self.region_id = credentials.get("region_id", "us")

    # ── client construction (lazy SDK import, cached per app_key) ─────────
    def _trade_client(self):
        return trade_client_for(self.app_key, self.app_secret, self.region_id)

    def _data_client(self):
        """Market-data client. Separate from the TradeClient because Webull
        gates quotes behind their own entitlement — see _quotes_disabled_until."""
        from webull.core.client import ApiClient        # noqa: PLC0415
        from webull.data.data_client import DataClient  # noqa: PLC0415

        now = time.monotonic()
        with _data_client_lock:
            cached = _data_clients.get(self.app_key)
            if cached is not None and (now - cached[1]) < _TRADE_CLIENT_TTL_S:
                return cached[0]
            api_client = ApiClient(self.app_key, self.app_secret, self.region_id)
            _suppress_sdk_file_logger(api_client)   # DataClient writes its own ./webull_data_sdk.log
            set_per_account_token_dir(api_client, self.app_key)
            client = DataClient(api_client)
            _data_clients[self.app_key] = (client, now)
            return client

    # ── market data (best-effort; drives marketable-limit pricing) ───────
    def _quotes_available(self) -> bool:
        with _quotes_disabled_lock:
            until = _quotes_disabled_until.get(self.app_key or "")
        return until is None or time.monotonic() >= until

    def _note_quote_failure(self, what: str, exc: BaseException) -> None:
        """Back off from the quote API when the failure is an entitlement problem
        rather than a blip, so one un-entitled app_key can't turn every mirror
        into a wasted HTTP call."""
        msg = str(exc)
        if _looks_like_missing_entitlement(msg):
            with _quotes_disabled_lock:
                _quotes_disabled_until[self.app_key or ""] = (
                    time.monotonic() + _QUOTE_DISABLED_TTL_S
                )
            log.warning(
                "webull %s: market data appears not entitled for this app_key "
                "(%s). Suppressing quote calls for %.0fs; mirrors will fall back "
                "to trader-anchored pricing. Enable the Market Data API at "
                "developer.webull.com to get quote-priced limits.",
                what, msg[:200], _QUOTE_DISABLED_TTL_S,
            )
        else:
            log.warning("webull %s failed: %s", what, msg[:200])

    @staticmethod
    def _snapshot_rows(body: Any) -> list[dict]:
        if isinstance(body, list):
            return [r for r in body if isinstance(r, dict)]
        if isinstance(body, dict):
            for key in ("data", "snapshots", "items", "quotes"):
                v = body.get(key)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
            return [body]
        return []

    def get_stock_latest_price(self, symbol: str) -> "Decimal | None":
        """Last traded price for a stock, or None when unavailable. Used to price
        a marketable limit (copy_engine._marketable_stock_limit) — the caller
        treats None as 'leave the order alone', so failing is never fatal."""
        if not self._quotes_available():
            return None
        try:
            res = self._data_client().market_data.get_snapshot(
                symbol.upper(), "US_STOCK", extend_hour_required=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._note_quote_failure("get_snapshot", exc)
            return None
        if getattr(res, "status_code", None) != 200:
            return None
        for row in self._snapshot_rows(res.json() or {}):
            px = _dec(_first(row, "last_price", "lastPrice", "close", "price", "trade_price"))
            if px is not None and px > 0:
                return px
        return None

    def get_option_latest_quote(
        self, occ_symbol: str
    ) -> tuple["Decimal | None", "Decimal | None"]:
        """(bid, ask) for an OCC option symbol — the exact form Webull's option
        snapshot endpoint takes (e.g. AAPL260619C00220000). Either side may be
        None on a one-sided book; (None, None) when the quote is unavailable, at
        which point the caller falls back to trader-anchored pricing."""
        if not self._quotes_available():
            return (None, None)
        try:
            res = self._data_client().option_market_data.get_option_snapshot(
                occ_symbol.upper().replace(" ", ""), "US_OPTION",
            )
        except Exception as exc:  # noqa: BLE001
            self._note_quote_failure("get_option_snapshot", exc)
            return (None, None)
        if getattr(res, "status_code", None) != 200:
            return (None, None)
        for row in self._snapshot_rows(res.json() or {}):
            bid = _dec(_first(row, "bid_price", "bidPrice", "bid"))
            ask = _dec(_first(row, "ask_price", "askPrice", "ask"))
            # Some shapes nest the top of book under bid/ask LISTS.
            if bid is None:
                bid = _dec(_first(_first_dict(row, "bid_list", "bidList", "bids"), "price"))
            if ask is None:
                ask = _dec(_first(_first_dict(row, "ask_list", "askList", "asks"), "price"))
            if bid is not None or ask is not None:
                return (bid, ask)
        return (None, None)

    # ── reads (used by the direct-Webull trader path) ────────────────────
    def list_accounts(self, with_balances: bool = False) -> list[dict[str, Any]]:
        """Every account these API keys can trade, for the connect-time picker.

        One Webull app_key reaches ALL accounts under that login — Cash, Margin,
        IRA, Futures — and the one we trade is chosen purely by the ``account_id``
        stored in the credentials. That id is not the account number shown in the
        Webull app, so asking a user to type it is asking them to guess: a
        plausible-but-wrong value passes verification and every mirror then trades
        in the wrong account. Hence the picker, and hence ``with_balances`` —
        equity is what actually lets someone tell their funded account from an
        empty one.

        Balances are best-effort per account (one extra call each); an account
        whose balance can't be read is still returned, just without figures.
        """
        trade = self._trade_client()
        res = trade.account_v2.get_account_list()
        if getattr(res, "status_code", None) != 200:
            raise RuntimeError(
                f"Webull get_account_list failed: {getattr(res, 'status_code', '?')}"
            )
        rows = res.json() or []
        out: list[dict[str, Any]] = []
        for a in rows:
            if not isinstance(a, dict):
                continue
            acct_id = _first(a, "account_id", "accountId")
            if not acct_id:
                continue
            entry: dict[str, Any] = {
                "account_id": str(acct_id),
                "account_number": (
                    str(_first(a, "account_number", "accountNumber") or "") or None
                ),
                "account_type": (
                    str(_first(a, "account_type", "accountType", "type") or "") or None
                ),
                "currency": (
                    str(_first(a, "currency", "account_currency") or "") or None
                ),
                "total_equity": None,
                "buying_power": None,
            }
            if with_balances:
                # Priced through the SAME trade client — get_account_balance
                # takes the account id as an argument, so there is no need (and
                # no reason) to build a second adapter per account.
                try:
                    bres = trade.account_v2.get_account_balance(entry["account_id"])
                    if getattr(bres, "status_code", None) == 200:
                        bal = self._parse_balance(bres.json() or {})
                        entry["total_equity"] = bal.get("total_equity")
                        entry["buying_power"] = bal.get("buying_power")
                        entry["currency"] = bal.get("currency") or entry["currency"]
                except Exception:  # noqa: BLE001
                    log.warning(
                        "webull list_accounts: balance read failed for %s",
                        entry["account_id"], exc_info=True,
                    )
            out.append(entry)
        return out

    def verify_connection(self) -> ConnectionInfo:
        """Confirm the keys authenticate and the configured account_id exists.
        Raises with a user-surfaceable message on failure."""
        trade = self._trade_client()
        res = trade.account_v2.get_account_list()
        if getattr(res, "status_code", None) != 200:
            raise RuntimeError(f"Webull get_account_list failed: {getattr(res, 'status_code', '?')}")
        accounts = [a for a in (res.json() or []) if isinstance(a, dict)]
        by_id = {
            str(_first(a, "account_id", "accountId")): a
            for a in accounts if _first(a, "account_id", "accountId")
        }
        if self.account_id and str(self.account_id) not in by_id:
            raise RuntimeError(
                f"Webull account_id {self.account_id} not found for these keys "
                f"(available: {sorted(by_id)})"
            )
        chosen = by_id.get(str(self.account_id)) if self.account_id else None
        # Surface the human-readable ACCOUNT NUMBER, not the opaque account_id.
        # This lands in broker_accounts.broker_account_number, which is display
        # only — and showing the number the user recognises from the Webull app
        # is how they notice at a glance that the wrong account got linked.
        # Falls back to the id when Webull doesn't supply a number.
        display = None
        if chosen is not None:
            display = str(_first(chosen, "account_number", "accountNumber") or "") or None
        return ConnectionInfo(
            broker_account_id=(
                display or (str(self.account_id) if self.account_id else None)
            ),
            supports_fractional=False,   # Webull US options/stocks: whole units in copy path
            extra={"region_id": self.region_id},
        )

    def get_positions(self) -> list[BrokerPosition]:
        """Live positions for this account.

        This is the BROKER-SIDE source of truth for "what does the subscriber
        actually hold", so it is load-bearing for the copy path — not just a
        display read. ``order_retry.live_closeable_quantity`` matches a mirror
        CLOSE against these rows by
        ``(instrument_type, symbol, option_expiry, option_strike, option_right)``;
        anything it can't match reads as FLAT, and the copy engine then drops the
        close as a dangling entry. So for OPTIONS we resolve the full contract
        terms (see ``_option_terms``) and report ``symbol`` as the UNDERLYING
        ROOT — the same shape ``BrokerOrderRequest`` carries — rather than the
        option's own ticker.

        An option row whose terms we cannot resolve is logged at ERROR and
        SKIPPED rather than returned half-populated: a row with None terms is
        indistinguishable from "not held" to every caller, so surfacing it would
        silently claim the subscriber is flat. Skipping makes the gap visible in
        the logs instead of turning into a stranded position.
        """
        trade = self._trade_client()
        res = trade.account_v2.get_account_position(self.account_id)
        if getattr(res, "status_code", None) != 200:
            log.warning("webull get_account_position failed: %s", getattr(res, "status_code", "?"))
            return []
        body = res.json() or {}
        # Response is either a list of positions or a dict wrapping one.
        rows = body if isinstance(body, list) else (
            body.get("positions") or body.get("holdings") or body.get("items") or []
        )
        out: list[BrokerPosition] = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            sym = str(_first(p, "symbol", "ticker") or "").upper()
            cat = str(_first(p, "category", "asset_type", "instrument_type") or "").upper()
            # Category is authoritative when present ("OPTION" / "US_OPTION");
            # an OCC-shaped symbol is the fallback for responses that omit it.
            is_opt = "OPTION" in cat or _looks_like_occ(sym)
            qty = _dec(_first(p, "quantity", "position", "units")) or Decimal(0)
            # Signed: short positions come back with a direction flag on some
            # brokers; default to long unless explicitly marked short.
            direction = str(_first(p, "direction", "side", "position_side") or "").upper()
            if direction in ("SHORT", "SELL") and qty > 0:
                qty = -qty
            # position_id is Webull's unique handle for this holding; the bare
            # ticker is the last resort because it is not unique across the
            # option contracts of one underlying.
            broker_symbol = str(
                _first(p, "instrument_id", "position_id", "broker_symbol", "symbol")
                or sym
            )

            expiry = strike = right = None
            if is_opt:
                root, expiry, strike, right = _option_terms(p, sym)
                if expiry is None or strike is None or right is None:
                    log.error(
                        "webull get_positions: SKIPPING option position %s (qty=%s) — "
                        "could not resolve contract terms from the broker response "
                        "(expiry=%s strike=%s right=%s). A close for this contract "
                        "would be dropped as 'nothing held'. Row keys: %s",
                        sym or broker_symbol, qty, expiry, strike, right,
                        sorted(p.keys()),
                    )
                    continue
                # The copy path compares against the UNDERLYING, not the OCC
                # ticker — so normalise. Falls back to the raw symbol only if we
                # somehow resolved terms without a root.
                sym = (root or sym).upper()

            out.append(BrokerPosition(
                broker_symbol=broker_symbol,
                symbol=sym,
                instrument_type=InstrumentType.OPTION if is_opt else InstrumentType.STOCK,
                quantity=qty,
                avg_entry_price=_dec(_first(p, "cost_price", "avg_price", "average_cost")),
                current_price=_dec(_first(p, "last_price", "market_price", "price")),
                market_value=_dec(_first(p, "market_value", "market_val")),
                unrealized_pnl=_dec(_first(p, "unrealized_pnl", "unrealized_profit_loss", "open_pnl")),
                cost_basis=_dec(_first(p, "cost_basis", "total_cost", "cost")),
                option_expiry=expiry,
                option_strike=strike,
                option_right=right,
            ))
        return out

    @staticmethod
    def _parse_balance(body: dict[str, Any]) -> dict[str, Any]:
        """Webull balance body → the adapter-agnostic snapshot shape that
        ``_refresh_balance_into`` consumes. Split out so ``list_accounts`` can
        price OTHER accounts from the same trade client without constructing a
        second adapter. Validated against a real Webull balance response."""
        assets = body.get("account_currency_assets") or []
        a0 = assets[0] if assets and isinstance(assets[0], dict) else {}
        return {
            "cash": _dec(body.get("total_cash_balance") or a0.get("cash_balance")),
            "buying_power": _dec(a0.get("buying_power") or a0.get("option_buying_power")),
            "total_equity": _dec(
                body.get("total_net_liquidation_value") or a0.get("net_liquidation_value")
            ),
            "currency": body.get("total_asset_currency") or a0.get("currency") or "USD",
        }

    def get_balance_snapshot(self) -> dict[str, Any]:
        """Cash / buying power / equity for the Brokers UI + connect. Shape
        matches the Alpaca/SnapTrade adapters so ``_refresh_balance_into`` can
        consume it."""
        trade = self._trade_client()
        res = trade.account_v2.get_account_balance(self.account_id)
        if getattr(res, "status_code", None) != 200:
            raise RuntimeError(f"webull get_account_balance failed: {getattr(res, 'status_code', '?')}")
        return self._parse_balance(res.json() or {})

    def get_pnl_snapshot(self) -> dict[str, Any] | None:
        """Equity / day-start / today's P&L for the daily kill switches. Webull
        reports today's P&L DIRECTLY (``total_day_profit_loss``), so day-start is
        derived as equity − todays_pl. Returns None (poller skips) on failure."""
        try:
            trade = self._trade_client()
            res = trade.account_v2.get_account_balance(self.account_id)
            if getattr(res, "status_code", None) != 200:
                return None
            b = res.json() or {}
            equity = _dec(b.get("total_net_liquidation_value"))
            todays_pl = _dec(b.get("total_day_profit_loss")) or Decimal(0)
            if equity is None:
                return None
            return {
                "todays_pl": todays_pl,
                "equity": equity,
                "beginning_day_balance": equity - todays_pl,
            }
        except Exception:  # noqa: BLE001
            log.warning("webull get_pnl_snapshot failed", exc_info=True)
            return None

    # ── writes — subscriber mirror execution on direct Webull ────────────
    # Order identity: Webull's cancel / replace / get_order_detail all key on
    # the CALLER-generated client_order_id (NOT the broker order_id), so we use
    # our Order row's UUID (stripped to Webull's 32-char max) as the
    # client_order_id AND return it as broker_order_id. Reusing the same
    # client_order_id across retries of one logical order is Webull's only
    # idempotency guard against double-placement.
    _ORDER_TYPE_MAP = {
        OrderType.MARKET: "MARKET",
        OrderType.LIMIT: "LIMIT",
        OrderType.STOP: "STOP_LOSS",
        OrderType.STOP_LIMIT: "STOP_LOSS_LIMIT",
    }

    def place_order(self, req: BrokerOrderRequest) -> BrokerOrderResult:
        if not self.account_id:
            raise RuntimeError("webull place_order: no account_id configured")
        trade = self._trade_client()
        coid = self._client_order_id(req)
        if req.instrument_type == InstrumentType.OPTION:
            resp = trade.order_v2.place_option(
                self.account_id, [self._build_option_order(req, coid)]
            )
        else:
            resp = trade.order_v3.place_order(
                self.account_id, [self._build_stock_order(req, coid)]
            )
        self._assert_place_accepted(resp, coid)
        # The place response returns only {client_order_id, order_id} — no fill
        # yet. Report SUBMITTED; the subscriber reconciler polls get_order for
        # the fill (exactly like the SnapTrade subscriber path).
        return BrokerOrderResult(
            broker_order_id=coid,
            status=OrderStatus.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=Decimal(0),
            filled_avg_price=None,
        )

    def get_order(self, broker_order_id: str) -> BrokerOrderResult:
        """Order status/fill for a mirror we placed. ``broker_order_id`` is the
        client_order_id we generated at placement (see place_order)."""
        trade = self._trade_client()
        detail = self._fetch_detail(trade, broker_order_id)
        if detail is None:
            raise RuntimeError(
                f"webull get_order_detail failed for {broker_order_id}"
            )
        _body, _is_opt, status, filled_qty, filled_px = detail
        return BrokerOrderResult(
            broker_order_id=broker_order_id,
            status=status,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=filled_qty,
            filled_avg_price=filled_px,
        )

    # How many of today's orders one snapshot call pulls back. Anything beyond
    # this falls back to a per-order read, so a large day degrades in cost rather
    # than in correctness.
    _SNAPSHOT_PAGE_SIZE = 50

    def get_orders_snapshot(self) -> dict[str, BrokerOrderResult]:
        """Today's orders for this account in ONE call, keyed by the
        client_order_id we placed them under.

        ``fills_sync._refresh_open_orders`` is broker-agnostic and reads one
        order at a time, which is fine on Alpaca but not here: Webull's trade
        endpoints share roughly 10 requests / 30 seconds per app_key, and the
        reconciler runs every 30s. A subscriber with ten working orders would
        spend the entire budget on a single sweep, and ``_refresh_open_orders``
        swallows per-order failures — so the throttled ones would simply not
        sync, invisibly, which on direct Webull is the ONLY fill-sync path there
        is. One list call covers them all.

        Best-effort by contract: a caller that gets an empty or partial map must
        fall back to ``get_order`` for whatever is missing. Orders from a previous
        day are legitimately absent.
        """
        trade = self._trade_client()
        try:
            res = trade.order.list_today_orders(
                self.account_id, page_size=self._SNAPSHOT_PAGE_SIZE
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("webull list_today_orders failed: %s", str(exc)[:200])
            return {}
        if getattr(res, "status_code", None) != 200:
            return {}
        body = res.json() or {}
        rows = body if isinstance(body, list) else (
            body.get("orders") or body.get("items") or body.get("data") or []
        )
        out: dict[str, BrokerOrderResult] = {}
        now = datetime.now(timezone.utc)
        for o in rows:
            if not isinstance(o, dict):
                continue
            # We key every order we place by our own client_order_id, and that is
            # what our broker_order_id column holds — see place_order.
            coid = str(_first(o, "client_order_id", "clientOrderId") or "").strip()
            if not coid:
                continue
            legs = o.get("items") or o.get("legs") or []
            leg = legs[0] if legs and isinstance(legs[0], dict) else o
            status_raw = str(
                _first(leg, "order_status", "status")
                or _first(o, "order_status", "status") or ""
            ).upper()
            if not status_raw:
                continue
            out[coid] = BrokerOrderResult(
                broker_order_id=coid,
                status=_STATUS_MAP.get(status_raw, OrderStatus.SUBMITTED),
                submitted_at=now,
                filled_quantity=(
                    _dec(_first(leg, "filled_qty", "filledQty", "cumulative_quantity"))
                    or Decimal(0)
                ),
                filled_avg_price=_dec(
                    _first(leg, "filled_price", "avg_fill_price", "filledPrice")
                ),
            )
        if body.get("hasNext") if isinstance(body, dict) else False:
            log.info(
                "webull orders snapshot: more than %d orders today for account %s; "
                "the remainder fall back to per-order reads",
                self._SNAPSHOT_PAGE_SIZE, self.account_id,
            )
        return out

    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a working mirror.

        Returns per ``BrokerAdapter.cancel_order``'s contract, which callers rely
        on for more than logging: ``True`` means we cancelled a LIVE order, so
        cancel-then-replace may safely place the replacement; ``False`` means the
        broker reports it already terminal — it may have FILLED, and placing on
        top would DOUBLE the position. When the state can't be established we
        raise, because "unknown" must never be reported as True.

        That distinction is why the no-detail path below confirms instead of
        trusting the cancel's own 200. A 200 from Webull says the request was
        accepted, not that there was a live order behind it — and
        ``copy_engine._force_fill_cancel_then_place`` places a full-size
        replacement on any True.
        """
        trade = self._trade_client()
        detail = self._fetch_detail(trade, broker_order_id)
        if detail is not None:
            _body, is_option, status, _q, _p = detail
            if status in _TERMINAL_STATUSES:
                return False
            resp = (
                trade.order_v2.cancel_option(self.account_id, broker_order_id)
                if is_option
                else trade.order_v3.cancel_order(self.account_id, broker_order_id)
            )
            if getattr(resp, "status_code", None) == 200:
                return True
            # Non-200: it may have filled/cancelled between the read and here.
            again = self._fetch_detail(trade, broker_order_id)
            if again is not None and again[2] in _TERMINAL_STATUSES:
                return False
            raise RuntimeError(f"webull cancel failed: {self._error_text(resp)}")

        # Couldn't read the order — instrument type unknown, and so is whether it
        # was ever live. Try both cancel endpoints (a no-op on the wrong one),
        # then CONFIRM by re-reading rather than returning True on the 200.
        for _do_cancel in (
            lambda: trade.order_v3.cancel_order(self.account_id, broker_order_id),
            lambda: trade.order_v2.cancel_option(self.account_id, broker_order_id),
        ):
            try:
                resp = _do_cancel()
            except Exception:  # noqa: BLE001
                continue
            if getattr(resp, "status_code", None) != 200:
                continue
            after = self._fetch_detail(trade, broker_order_id)
            if after is None:
                # Cancel accepted but we still can't see the order (a throttle,
                # most likely — the same reason the first read failed). Refuse to
                # claim success: a replacement placed over a filled order doubles
                # the subscriber's position, and the caller's failure path just
                # leaves the mirror alone.
                raise RuntimeError(
                    f"webull cancel: request accepted for {broker_order_id} but "
                    "its state could not be confirmed; not reporting success"
                )
            if after[2] == OrderStatus.FILLED:
                return False      # it had already filled — nothing was cancelled
            return True
        raise RuntimeError(
            f"webull cancel failed: order {broker_order_id} not found / not cancellable"
        )

    # ── order-build + response helpers ───────────────────────────────────
    @staticmethod
    def _client_order_id(req: BrokerOrderRequest) -> str:
        # Our Order UUID (dashes stripped → 32 hex) is stable per logical order,
        # so retries reuse it — Webull's idempotency key. Fall back to a fresh
        # uuid only when the caller supplied none.
        raw = req.client_order_id or uuid.uuid4().hex
        return raw.replace("-", "")[:32]

    @staticmethod
    def _fmt_qty(q: Decimal | Any) -> str:
        d = Decimal(str(q))
        if d == d.to_integral_value():
            return str(int(d))
        return format(d.normalize(), "f")

    @staticmethod
    def _fmt_price(p: Decimal | Any) -> str:
        d = Decimal(str(p))
        # Webull price precision: 2 decimals for >= $1, 4 decimals for < $1.
        step = Decimal("0.01") if abs(d) >= 1 else Decimal("0.0001")
        return str(d.quantize(step, rounding=ROUND_HALF_UP))

    @staticmethod
    def _session(req: BrokerOrderRequest) -> str:
        # support_trading_session: CORE = regular hours only; ALL = include
        # pre/post-market. A MARKET order must be CORE (extended hours is
        # limit-only). Options are RTH-only and carry no session field.
        if req.order_type == OrderType.MARKET:
            return "CORE"
        return "ALL" if req.extended_hours else "CORE"

    @staticmethod
    def _position_intent(req: BrokerOrderRequest) -> str:
        # Open vs close is a distinct field on Webull options — NOT encoded in
        # side alone. A closing SELL must be SELL_TO_CLOSE (never SELL_TO_OPEN),
        # or the broker rejects it "no position to close".
        buy = req.side == OrderSide.BUY
        if req.is_closing:
            return "BUY_TO_CLOSE" if buy else "SELL_TO_CLOSE"
        return "BUY_TO_OPEN" if buy else "SELL_TO_OPEN"

    def _build_stock_order(self, req: BrokerOrderRequest, coid: str) -> dict[str, Any]:
        d: dict[str, Any] = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "symbol": req.symbol.upper(),
            "instrument_type": "STOCK",
            "market": "US",
            "side": "BUY" if req.side == OrderSide.BUY else "SELL",
            "order_type": self._ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "quantity": self._fmt_qty(req.quantity),
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "support_trading_session": self._session(req),
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price is not None:
            d["limit_price"] = self._fmt_price(req.limit_price)
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is not None:
            d["stop_price"] = self._fmt_price(req.stop_price)
        return d

    def _build_option_order(self, req: BrokerOrderRequest, coid: str) -> dict[str, Any]:
        if not (req.option_expiry and req.option_strike and req.option_right):
            raise RuntimeError(
                "webull option order missing contract terms "
                "(expiry/strike/right required)"
            )
        intent = self._position_intent(req)
        side = "BUY" if req.side == OrderSide.BUY else "SELL"
        leg: dict[str, Any] = {
            "side": side,
            "position_intent": intent,
            "quantity": self._fmt_qty(req.quantity),
            "ratio": "1",
            "instrument_type": "OPTION",
            "market": "US",
            "symbol": req.symbol.upper(),
            "strike_price": self._fmt_price(req.option_strike),
            "option_expire_date": req.option_expiry.isoformat(),
            "option_type": "CALL" if req.option_right == OptionRight.CALL else "PUT",
        }
        d: dict[str, Any] = {
            "client_order_id": coid,
            "combo_type": "NORMAL",
            "option_strategy": "SINGLE",
            "order_type": self._ORDER_TYPE_MAP.get(req.order_type, "MARKET"),
            "quantity": self._fmt_qty(req.quantity),
            "time_in_force": "DAY",
            "entrust_type": "QTY",
            "position_intent": intent,
            "side": side,
            "legs": [leg],
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and req.limit_price is not None:
            d["limit_price"] = self._fmt_price(req.limit_price)
        if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and req.stop_price is not None:
            d["stop_price"] = self._fmt_price(req.stop_price)
        return d

    def _fetch_detail(self, trade: Any, coid: str):
        """Query one order by client_order_id. Returns
        ``(body, is_option, status, filled_qty, filled_px)`` or None if the
        lookup itself failed (non-200 / no body)."""
        try:
            resp = trade.order_v3.get_order_detail(self.account_id, coid)
        except Exception:  # noqa: BLE001
            return None
        if getattr(resp, "status_code", None) != 200:
            return None
        body = resp.json() or {}
        order = body if isinstance(body, dict) else {}
        legs = (
            order.get("items") or order.get("legs")
            or order.get("orders") or order.get("order_legs") or []
        )
        leg = legs[0] if legs and isinstance(legs[0], dict) else order
        cat = str(
            _first(order, "category", "combo_ticker_type")
            or _first(leg, "category", "instrument_type") or ""
        ).upper()
        is_option = "OPTION" in cat
        status_raw = str(
            _first(leg, "order_status", "status")
            or _first(order, "order_status", "status") or ""
        ).upper()
        status = _STATUS_MAP.get(status_raw, OrderStatus.SUBMITTED)
        filled_qty = (
            _dec(_first(leg, "filled_qty", "filledQty", "cumulative_quantity"))
            or _dec(_first(order, "filled_qty", "filledQty"))
            or Decimal(0)
        )
        filled_px = (
            _dec(_first(leg, "filled_price", "avg_fill_price", "filledPrice", "avgFilledPrice"))
            or _dec(_first(order, "filled_price", "avg_fill_price"))
        )
        return order, is_option, status, filled_qty, filled_px

    def _raise_for_status(self, resp: Any, what: str) -> None:
        if getattr(resp, "status_code", None) != 200:
            raise RuntimeError(f"webull {what} failed: {self._error_text(resp)}")

    # Fields that, when present on a 200 body, mean the order was NOT accepted.
    _PLACE_ERROR_KEYS = ("error_code", "errorCode", "err_code", "failure_reason",
                         "failureReason", "reject_reason", "rejectReason")

    def _assert_place_accepted(self, resp: Any, coid: str) -> None:
        """Raise unless the broker actually accepted the order.

        An HTTP 200 alone is not proof. The SDK raises ServerException for every
        non-2xx, so the status check is really a backstop — what it cannot catch
        is a 200 whose BODY reports a per-order failure, which a batch place
        endpoint (``new_orders`` is a list) can legitimately return. Treating
        that as success writes a SUBMITTED row carrying a broker_order_id that
        does not exist: the reconciler can never resolve it, the mirror sits
        working forever, and close-detection thinks the subscriber holds a
        position they never opened.

        Deliberately conservative about UNKNOWN shapes. We only reject on a
        recognised error signal; an unfamiliar-but-successful body is accepted
        and logged, so a Webull response change degrades to a log line rather
        than refusing every order.
        """
        self._raise_for_status(resp, "place_order")
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return          # unparseable 200 — nothing to check against
        rows = body if isinstance(body, list) else [body]
        accepted = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in self._PLACE_ERROR_KEYS:
                if row.get(key) not in (None, "", 0, "0"):
                    raise RuntimeError(
                        f"webull place_order rejected: {row.get(key)} "
                        f"{row.get('msg') or row.get('message') or ''}".strip()
                    )
            # The documented success shape echoes our client_order_id and adds
            # Webull's own order_id.
            if _first(row, "order_id", "orderId") or str(
                _first(row, "client_order_id", "clientOrderId") or ""
            ) == coid:
                accepted = True
        if not accepted:
            log.warning(
                "webull place_order: 200 with an unrecognised body for %s (%r) — "
                "treating as accepted; if mirrors go missing, this is where to look.",
                coid, body if not isinstance(body, (bytes, bytearray)) else "<bytes>",
            )

    @staticmethod
    def _error_text(resp: Any) -> str:
        code = getattr(resp, "status_code", "?")
        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = str(body.get("msg") or body.get("message") or body.get("code") or body)
            else:
                detail = str(body)
        except Exception:  # noqa: BLE001
            detail = str(getattr(resp, "text", "") or "")
        return f"HTTP {code} {detail}".strip()


__all__ = ["WebullAdapter"]
