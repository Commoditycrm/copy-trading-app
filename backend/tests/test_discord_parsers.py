"""Tests for Discord alert parsing.

The governing rule from the spec: a parsing error must NEVER become a guessed
trade. So most of these assert what the parser REFUSES to do — a message missing
a strike, a right, or a resolvable expiry has to come back INVALID with a reason,
never a signal assembled from whatever happened to be present.
"""
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.discord_parsers import ParsedMessage, ParseStatus, parse_message

POSTED = datetime(2026, 9, 8, 14, 33, tzinfo=timezone.utc)


def card(title, description="", fields=None, posted_at=POSTED):
    return ParsedMessage(
        content="",
        embeds=[{"title": title, "description": description, "fields": fields or []}],
        posted_at=posted_at,
    )


def text(body, posted_at=POSTED):
    return ParsedMessage(content=body, posted_at=posted_at)


# ── Alert cards (the real-world format) ──────────────────────────────────────

def test_entering_card_is_a_buy():
    r = parse_message(card("ENTERING · OKLO $44 CALL · 09/11", "6 @ $1.58 · $948"))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.action.value, s.symbol, s.option_type.value) == ("BUY", "OKLO", "CALL")
    assert s.strike == Decimal("44")
    assert s.expiration == date(2026, 9, 11)
    assert s.quantity == Decimal("6")
    assert s.limit_price == Decimal("1.58")


def test_alerts_are_read_from_embeds_when_content_is_empty():
    """Real alert bots put everything in an embed and leave content blank. A
    parser reading only content would reject the entire live feed."""
    m = card("ENTERING · TSLA $365 CALL · 09/09", "2 @ $3.90 · $780")
    assert m.content == ""
    assert parse_message(m).status is ParseStatus.PARSED


def test_closing_card_is_a_full_sell():
    r = parse_message(card(
        "CLOSING · ASTS $65 CALL · 09/11",
        "Sold 4 @ $2.74 · +$152 · +16%\nPosition closed · total +$152 · +16%",
    ))
    assert r.signal.action.value == "SELL"
    assert r.signal.is_partial_close is False


def test_trimming_is_marked_partial_with_the_remaining_size():
    """A trim closes PART of a position. Treating it as a full exit would
    flatten a position the trader still holds — the worst failure here."""
    r = parse_message(card(
        "TRIMMING · META $630 CALL · 09/18",
        "Sold 3 @ $10.85 · +$630 · +24%\n2 of 5 still open",
    ))
    s = r.signal
    assert s.action.value == "SELL"
    assert s.is_partial_close is True
    assert s.remaining_quantity == Decimal("2")
    assert s.quantity == Decimal("3")


def test_a_trim_without_a_remaining_line_is_still_partial():
    r = parse_message(card("TRIMMING · CIFR $18 CALL · 09/11", "Sold 3 @ $1.03 · +$48"))
    assert r.signal.is_partial_close is True
    assert r.signal.remaining_quantity is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("CLOSING · MU $1,015 CALL · 09/11", Decimal("1015")),
        ("ENTERING · SPXW $7,700 PUT · 09/04", Decimal("7700")),
        ("ENTERING · NVDA $227.50 PUT · 09/09", Decimal("227.50")),
    ],
)
def test_strikes_with_commas_and_decimals(title, expected):
    """A naive \\d+ reads "$1,015" as 1 — a different contract at a plausible
    price. All three of these appear in the live feed."""
    r = parse_message(card(title, "Sold 1 @ $23.05"))
    assert r.signal.strike == expected


def test_put_cards_are_read_as_puts():
    r = parse_message(card("ENTERING · SPXW $7,700 PUT · 09/04", "1 @ $2.55 · $255"))
    assert r.signal.option_type.value == "PUT"


def test_a_card_without_a_quantity_line_is_invalid_not_guessed():
    r = parse_message(card("ENTERING · OKLO $44 CALL · 09/11", "looks juicy"))
    assert r.status is ParseStatus.INVALID
    assert "quantity" in r.reason.lower()


# ── Free text ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "BUY AAPL 250 CALL 18 SEP @ 2.15",
        "BUY 5 AAPL 250C SEP18 @ 2.15 LIMIT",
        "BTO AAPL 250C 09/18 @ 2.15",
    ],
)
def test_common_free_text_formats(body):
    r = parse_message(text(body))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.symbol, s.strike, s.option_type.value) == ("AAPL", Decimal("250"), "CALL")
    assert s.expiration == date(2026, 9, 18)


def test_multiline_format():
    r = parse_message(text("AAPL\nCALL\n250 STRIKE\nEXP: SEP 18\nBUY @ 2.15"))
    assert r.status is ParseStatus.PARSED
    assert r.signal.strike == Decimal("250")
    assert r.signal.option_type.value == "CALL"


def test_quantity_is_read_when_stated():
    assert parse_message(text("BUY 5 AAPL 250C SEP18 @ 2.15")).signal.quantity == Decimal("5")


def test_discord_alerts_are_always_limit_orders():
    """Every alert is placed as a LIMIT, even one that says MARKET. A market
    order on a thin option contract fills at whatever is there — which on a
    wide spread can be far from the alerted price."""
    r = parse_message(text("BUY AAPL 250C SEP18 MARKET"))
    assert r.signal.order_type.value == "LIMIT"
    assert r.signal.limit_price is None
    assert r.signal.limit_price_unspecified is True


def test_sell_words_are_read_as_sells():
    assert parse_message(text("STC AAPL 250C SEP18 @ 3.10")).signal.action.value == "SELL"


def test_a_stock_order_needs_no_option_fields():
    r = parse_message(text("BUY 100 SHARES AAPL @ 250.10"))
    assert r.status is ParseStatus.PARSED
    assert r.signal.asset_type.value == "STOCK"
    assert r.signal.option_type is None


# ── Refusals: the important half ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "Everyone welcome boizhikid!",
        "gm traders, watching SPY today",
        "Matt44 just slid into the server.",
        "",
    ],
)
def test_non_trade_messages_are_ignored_not_forced(body):
    assert parse_message(text(body)).status is ParseStatus.IGNORED


def test_a_call_without_a_strike_is_invalid():
    r = parse_message(text("BUY AAPL CALL SEP 18 @ 2.15"))
    assert r.status is ParseStatus.INVALID
    assert "strike" in r.reason.lower()


def test_a_strike_without_call_or_put_is_invalid():
    r = parse_message(text("BUY AAPL 250 STRIKE SEP 18 @ 2.15"))
    assert r.status is ParseStatus.INVALID
    assert "call or put" in r.reason.lower()


def test_an_option_without_an_expiry_is_invalid():
    r = parse_message(text("BUY AAPL 250C @ 2.15"))
    assert r.status is ParseStatus.INVALID
    assert "expiry" in r.reason.lower()


def test_an_expiry_with_no_year_and_no_timestamp_is_invalid():
    """Without a posted date the year is genuinely unknowable, and a wrong year
    is a wrong contract. Refuse rather than assume."""
    r = parse_message(ParsedMessage(content="BUY AAPL 250C 09/18 @ 2.15", posted_at=None))
    assert r.status is ParseStatus.INVALID


def test_an_impossible_date_is_invalid():
    r = parse_message(text("BUY AAPL 250C 02/31 @ 2.15"))
    assert r.status is ParseStatus.INVALID


# ── Expiry year inference ────────────────────────────────────────────────────

def test_expiry_year_comes_from_the_posted_date_not_today():
    """Re-reading an old message must not roll its expiry forward a year."""
    old = datetime(2025, 9, 8, tzinfo=timezone.utc)
    r = parse_message(card("ENTERING · OKLO $44 CALL · 09/11", "6 @ $1.58", posted_at=old))
    assert r.signal.expiration == date(2025, 9, 11)


def test_an_expiry_already_past_rolls_to_next_year():
    """Posted late December, expiring 01/02 ⇒ next January, not last."""
    dec = datetime(2026, 12, 28, tzinfo=timezone.utc)
    r = parse_message(card("ENTERING · SPY $600 CALL · 01/02", "1 @ $1.00", posted_at=dec))
    assert r.signal.expiration == date(2027, 1, 2)


def test_a_just_expired_date_stays_in_the_same_year():
    """A same-day alert posted just after expiry shouldn't jump a year."""
    r = parse_message(card("CLOSING · SPY $600 CALL · 09/08", "Sold 1 @ $1.00", posted_at=POSTED))
    assert r.signal.expiration == date(2026, 9, 8)


# ── Registry behaviour ───────────────────────────────────────────────────────

def test_the_card_parser_claims_cards_before_the_text_parser():
    r = parse_message(card("ENTERING · OKLO $44 CALL · 09/11", "6 @ $1.58 · $948"))
    assert r.signal.parser == "alert_card"


def test_free_text_falls_through_to_the_text_parser():
    assert parse_message(text("BUY AAPL 250C SEP18 @ 2.15")).signal.parser == "generic_text"


def test_an_unknown_parser_key_is_reported_not_silently_ignored():
    r = parse_message(text("BUY AAPL 250C SEP18 @ 2.15"), parser_key="nope")
    assert r.status is ParseStatus.INVALID


# ── The rest of the card ─────────────────────────────────────────────────────
# Every figure below is REPORTED by the alert. We record what it said rather
# than recomputing, so the tab shows the trader the same numbers they saw.

def test_an_open_card_records_the_stated_notional():
    r = parse_message(card("ENTERING · TSLA $350 CALL · 09/02", "1 @ $2.85 · $285"))
    s = r.signal
    assert s.notional == Decimal("285")
    # An open has no P&L to report.
    assert s.pnl_amount is None


def test_the_price_is_not_mistaken_for_the_notional():
    """"1 @ $2.85 · $285" has two dollar figures; the one after @ is the price."""
    s = parse_message(card("ENTERING · TSLA $350 CALL · 09/02", "1 @ $2.85 · $285")).signal
    assert s.limit_price == Decimal("2.85")
    assert s.notional == Decimal("285")


def test_a_close_records_fill_pnl_and_percent():
    s = parse_message(card(
        "CLOSING · ASTS $65 CALL · 09/11",
        "Sold 4 @ $2.74 · +$152 · +16%\nPosition closed · total +$152 · +16%",
    )).signal
    assert s.pnl_amount == Decimal("152")
    assert s.pnl_percent == Decimal("16")
    assert s.position_closed is True


def test_a_losing_close_keeps_the_negative_sign():
    """Dropping the sign would turn a loss into a gain in the P&L column."""
    s = parse_message(card(
        "CLOSING · META $620 CALL · 09/04",
        "Sold 6 @ $0.21 · -$96 · -43%\nPosition closed · total -$96 · -43%",
    )).signal
    assert s.pnl_amount == Decimal("-96")
    assert s.pnl_percent == Decimal("-43")
    assert s.total_pnl_amount == Decimal("-96")


def test_position_totals_are_separate_from_the_fill():
    """A final exit reports BOTH this fill's P&L and the position's total across
    every trim — conflating them would misreport the trade."""
    s = parse_message(card(
        "CLOSING · MU $1,015 CALL · 09/11",
        "Sold 1 @ $23.05 · +$705 · +44%\nPosition closed · total +$1,815 · +38%",
    )).signal
    assert s.pnl_amount == Decimal("705")        # this fill
    assert s.total_pnl_amount == Decimal("1815")  # whole position
    assert s.total_pnl_percent == Decimal("38")


def test_a_trim_records_both_remaining_and_original_size():
    s = parse_message(card(
        "TRIMMING · META $630 CALL · 09/18",
        "Sold 3 @ $10.85 · +$630 · +24%\n2 of 5 still open",
    )).signal
    assert s.remaining_quantity == Decimal("2")
    assert s.original_quantity == Decimal("5")
    assert s.position_closed is False


def test_position_closed_overrides_a_partial_reading():
    """If the card explicitly says the position is closed, that wins — a stale
    "still open" line must not leave us thinking size remains."""
    s = parse_message(card(
        "CLOSING · CIFR $18 CALL · 09/11",
        "Sold 8 @ $1.06 · +$152 · +22%\n8 of 8 still open\nPosition closed · total +$200",
    )).signal
    assert s.position_closed is True
    assert s.is_partial_close is False
    assert s.remaining_quantity is None


def test_a_unicode_minus_is_read_as_negative():
    """Cards render "−$96" with U+2212, which a plain "-" match would miss —
    silently flipping a loss to a gain."""
    s = parse_message(card(
        "CLOSING · X $10 PUT · 09/11", "Sold 1 @ $0.50 · −$96 · −43%"
    )).signal
    assert s.pnl_amount == Decimal("-96")
    assert s.pnl_percent == Decimal("-43")


# ── Compact alerts: "$TSLA 375 CALL 0DTE @0.95" ──────────────────────────────
# No action word at all — the SHAPE of the line is the instruction. Posting a
# contract with a price is the buy; a scissors marker is the exit.

def test_a_compact_entry_is_a_buy_of_one_lot():
    r = parse_message(text("$TSLA 375 CALL 0DTE @0.95"))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.action.value, s.symbol, s.option_type.value) == ("BUY", "TSLA", "CALL")
    assert s.strike == Decimal("375")
    assert s.limit_price == Decimal("0.95")
    # The format never states size; one lot is the convention.
    assert s.quantity == Decimal("1")


def test_0dte_expires_on_the_day_the_alert_was_posted():
    """Resolved against the MESSAGE's timestamp, not today — re-reading an old
    0DTE alert must not move its expiry to the present."""
    r = parse_message(text("$SPY 764 CALL 0DTE @0.90"))
    assert r.signal.expiration == POSTED.date()


def test_0dte_without_a_timestamp_is_invalid():
    r = parse_message(ParsedMessage(content="$SPY 764 CALL 0DTE @0.90", posted_at=None))
    assert r.status is ParseStatus.INVALID


def test_a_compact_entry_with_an_explicit_date():
    r = parse_message(text("$AAPL 330 CALL 09/04 @1.00"))
    assert r.signal.expiration == date(2026, 9, 4)


def test_a_scissors_line_is_a_sell():
    r = parse_message(text("✂️ $SPY 769c +361%"))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.action.value, s.symbol, s.strike) == ("SELL", "SPY", Decimal("769"))
    assert s.option_type.value == "CALL"     # lowercase "c"
    assert s.pnl_percent == Decimal("361")


def test_an_exit_leaves_the_expiry_unset_rather_than_guessing():
    """"✂️ $SPY 769c" states no expiry — it assumes you know what you hold.
    Inventing a date would put on the wrong contract, so it's flagged for
    resolution from the open position instead."""
    s = parse_message(text("✂️ $SPY 769c +361%")).signal
    assert s.expiration is None
    assert s.expiry_unspecified is True


def test_a_block_of_exits_produces_one_signal_per_line():
    """Reading only the first would silently drop the second trade."""
    r = parse_message(text("✂️ $SPY 769c +361%\n✂️ $SPY 770c +372%"))
    assert r.status is ParseStatus.PARSED
    assert len(r.signals) == 2
    assert [str(x.strike) for x in r.signals] == ["769", "770"]


def test_a_mixed_block_reads_every_line():
    r = parse_message(text("$TSLA 375 CALL 0DTE @0.95\n$SPY 764 CALL 0DTE @0.90"))
    assert len(r.signals) == 2
    assert all(x.action.value == "BUY" for x in r.signals)


def test_a_losing_trim_keeps_its_negative_percent():
    s = parse_message(text("✂️ $SPY 769c -22%")).signal
    assert s.pnl_percent == Decimal("-22")


def test_puts_are_read_from_a_lowercase_p():
    assert parse_message(text("✂️ $SPY 769p +40%")).signal.option_type.value == "PUT"


def test_chatter_is_not_forced_into_a_compact_alert():
    for body in ("$SPY looking strong today", "gm", "✂️ trimming soon"):
        assert parse_message(text(body)).status is not ParseStatus.PARSED


# ── Year inference must fail SAFE ────────────────────────────────────────────

def test_a_recently_past_date_is_not_rolled_a_year_forward():
    """Posted 09/10, alert says 09/04. Rolling to next year would produce a real,
    tradeable contract twelve months out — wrong, but plausible-looking. Keeping
    the past date yields an EXPIRED contract that validation rejects loudly."""
    posted = datetime(2026, 9, 10, tzinfo=timezone.utc)
    r = parse_message(ParsedMessage(content="$AAPL 330 CALL 09/04 @1.00", posted_at=posted))
    assert r.signal.expiration == date(2026, 9, 4)


def test_a_year_boundary_still_rolls_forward():
    posted = datetime(2026, 12, 28, tzinfo=timezone.utc)
    r = parse_message(ParsedMessage(content="$SPY 600 CALL 01/02 @1.00", posted_at=posted))
    assert r.signal.expiration == date(2027, 1, 2)


# ── Symbol-only closes: "META -> 100%" ───────────────────────────────────────
# A close reported as a ticker and a result. The percentage is the trade's P&L,
# not a fraction to sell — "-100%" only makes sense as a loss.

@pytest.mark.parametrize("body", ["META -> 100%", "META → 100%", "$META -> 100%", "META => 100%"])
def test_an_arrow_close_is_a_sell(body):
    r = parse_message(text(body))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.action.value, s.symbol) == ("SELL", "META")
    assert s.pnl_percent == Decimal("100")


def test_a_negative_arrow_close_is_a_loss_not_a_fraction():
    """"NVDA -> -100%" is a total loss. Reading the number as "close -100% of
    the position" is meaningless, which is what settles the ambiguity."""
    s = parse_message(text("NVDA -> -100%")).signal
    assert s.pnl_percent == Decimal("-100")
    assert s.action.value == "SELL"


def test_an_arrow_close_leaves_the_whole_contract_unresolved():
    """Only the symbol is known. Strike, call/put and expiry must all come from
    the open position — guessing which contract was meant would sell one the
    trader never intended to close."""
    s = parse_message(text("TSLA -> 25%")).signal
    assert s.contract_unspecified is True
    assert s.expiry_unspecified is True
    assert (s.strike, s.option_type, s.expiration) == (None, None, None)


def test_an_arrow_close_states_no_quantity():
    """Size comes from the position held, never from the alert."""
    assert parse_message(text("META -> 100%")).signal.quantity is None


def test_arrow_closes_are_marked_as_full_closes():
    s = parse_message(text("META -> 100%")).signal
    assert s.position_closed is True
    assert s.source_action == "CLOSE"


def test_a_block_of_arrow_closes_reads_every_line():
    r = parse_message(text("META -> 100%\nNVDA -> -100%\nTSLA -> 25%"))
    assert len(r.signals) == 3
    assert [x.symbol for x in r.signals] == ["META", "NVDA", "TSLA"]


def test_scissors_closes_still_pin_the_contract():
    """The scissors format DOES name strike and right, so it must not be
    downgraded to a symbol-only close — a position lookup has more to match on."""
    s = parse_message(text("✂️ $SPY 762c +210%")).signal
    assert s.contract_unspecified is False
    assert s.strike == Decimal("762")
    assert s.option_type.value == "CALL"
    assert s.expiry_unspecified is True


def test_an_arrow_close_is_not_confused_with_a_price_update():
    """"$TSLA 375c +43%" names a contract and is an update, not a close."""
    r = parse_message(text("$TSLA 375c +43%"))
    assert r.status is ParseStatus.IGNORED


@pytest.mark.parametrize("body", ["META -> the moon", "up -> 100", "-> 50%"])
def test_arrow_chatter_is_not_a_close(body):
    assert parse_message(text(body)).status is not ParseStatus.PARSED


# ── "Adding" — buying more of a position already held ────────────────────────
# The distinction from a fresh entry is what makes the expiry optional: the
# contract is the one already open, so a missing expiry is resolved from the
# position rather than being a parse failure.

@pytest.mark.parametrize(
    "body",
    ["Adding $MSFT 100c @1.90", "Add $MSFT 100c @1.90", "adding $MSFT 100c @1.90"],
)
def test_adding_is_a_buy(body):
    r = parse_message(text(body))
    assert r.status is ParseStatus.PARSED
    s = r.signal
    assert (s.action.value, s.symbol, s.strike) == ("BUY", "MSFT", Decimal("100"))
    assert s.source_action == "ADD"


def test_adding_resolves_the_expiry_from_the_open_position():
    """A fresh entry without an expiry is INVALID; an add isn't, because the
    contract is one the trader already holds."""
    s = parse_message(text("Adding $MSFT 100c @1.90")).signal
    assert s.expiration is None
    assert s.expiry_unspecified is True


def test_adding_with_a_stated_expiry_uses_it():
    s = parse_message(text("Adding $MSFT 100 CALL 10/10 @1.90")).signal
    assert s.expiration == date(2026, 10, 10)
    assert s.expiry_unspecified is False


def test_adding_a_bare_symbol_leaves_the_whole_contract_unresolved():
    s = parse_message(text("Adding $MSFT")).signal
    assert (s.action.value, s.symbol) == ("BUY", "MSFT")
    assert s.contract_unspecified is True


def test_adding_carries_the_limit_price_when_stated():
    s = parse_message(text("Adding $MSFT 100c @1.90")).signal
    assert s.limit_price == Decimal("1.90")
    assert s.order_type.value == "LIMIT"


def test_an_add_without_a_price_is_still_a_limit_order():
    """No price stated, so the limit has to come from the live quote at
    execution — but the order type is never market."""
    s = parse_message(text("adding $SPY 762c")).signal
    assert s.order_type.value == "LIMIT"
    assert s.limit_price is None
    assert s.limit_price_unspecified is True


@pytest.mark.parametrize(
    "body",
    [
        "Leave room to add incase the want a bit more of a bounce",
        "Adding more here",
        "might add later",
    ],
)
def test_prose_about_adding_is_not_an_order(body):
    """The word "add" appears constantly in commentary. An order needs a $ticker
    or an explicit contract — otherwise this parser would trade on chatter."""
    assert parse_message(text(body)).status is not ParseStatus.PARSED


def test_prose_about_adding_is_ignored_not_invalid():
    """INVALID is for things that look like an instruction. Commentary must not
    fill the review queue."""
    assert parse_message(text("Adding more here")).status is ParseStatus.IGNORED


# ── Always limit, never market ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "body",
    [
        "$TSLA 375 CALL 0DTE @0.95",     # entry with a price
        "AAPL $350 CALL 09/18",          # entry without one
        "Adding $MSFT 100c @1.90",       # add
        "adding $SPY 762c",              # add without a price
        "BUY AAPL 250C SEP18 MARKET",    # explicitly says MARKET
    ],
)
def test_entries_are_limit_orders(body):
    """A limit on the way in — never pay through a wide spread on a thin
    option, even when the alert says MARKET."""
    s = parse_message(text(body)).signal
    assert s.order_type.value == "LIMIT", body


@pytest.mark.parametrize(
    "body",
    [
        "✂️ $MSFT 100c +366%",           # scissors close
        "META -> 100%",                  # symbol-only close
        "STC AAPL 250C SEP18 @ 3.10",    # free-text close, price stated
    ],
)
def test_exits_are_market_orders(body):
    """An exit has to fill. A limit sell can sit unfilled while the position
    moves against you, and "close this" means get out — not get out at a
    price."""
    s = parse_message(text(body)).signal
    assert s.order_type.value == "MARKET", body


def test_a_stated_price_becomes_the_limit():
    s = parse_message(text("$TSLA 375 CALL 0DTE @0.95")).signal
    assert s.limit_price == Decimal("0.95")
    assert s.limit_price_unspecified is False


def test_an_entry_without_a_price_is_flagged_rather_than_invented():
    """Making a limit up would be a guess at the level to trade — it has to come
    from the live quote at execution instead."""
    s = parse_message(text("AAPL $350 CALL 09/18")).signal
    assert s.limit_price is None
    assert s.limit_price_unspecified is True


# ── real channel formats, per channel ────────────────────────────────────────
# Taken from five live channels. The same shape means opposite things across
# them, which is why percent_means_exit exists.

_POSTED = datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc)


def _p(text: str, *, percent_exit: bool = False):
    return parse_message(
        ParsedMessage(content=text, posted_at=_POSTED, percent_means_exit=percent_exit)
    )


@pytest.mark.parametrize(
    "text,symbol,strike,right",
    [
        ("$SPY 760 CALL 0DTE @1.00", "SPY", "760", "CALL"),        # Clint
        ("$CRWV 81 CALL 10/02 @4.75", "CRWV", "81", "CALL"),       # Swings
        ("SPY 763c @ .55", "SPY", "763", "CALL"),                  # Zenith
        ("TSLA 367.5C ODTE @1.28", "TSLA", "367.5", "CALL"),       # Mark
        ("SPY 760P @here @Sniper 1.2", "SPY", "760", "PUT"),       # Breakdownsniper
    ],
)
def test_every_channels_buy_format_parses(text, symbol, strike, right):
    sig = _p(text).signal
    assert sig is not None, text
    assert sig.action.value == "BUY"
    assert sig.symbol == symbol
    assert str(sig.strike) == strike
    assert sig.option_type.value == right


def test_a_price_written_without_its_leading_zero():
    """"@ .55" is how these channels write 55 cents."""
    assert _p("SPY 763c @ .55").signal.limit_price == Decimal("0.55")


def test_odte_with_the_letter_o_means_same_day():
    """A common mistype. Reading it as an unknown expiry rejects a valid alert."""
    assert _p("TSLA 367.5C ODTE @1.28").signal.expiration == _POSTED.date()


def test_discord_pings_do_not_hide_the_price():
    """"@here" looks like an "@price" and used to swallow the real one, leaving
    the alert unparseable."""
    assert _p("SPY 760P @here @Sniper 1.2").signal.limit_price == Decimal("1.2")


def test_a_buy_with_no_expiry_is_same_day():
    """Several channels post SPY scalps and never write the date."""
    assert _p("SPY 763c @ .55").signal.expiration == _POSTED.date()


# ── the same text, opposite meanings ─────────────────────────────────────────

@pytest.mark.parametrize("text", ["IWM 287P +52%", "AMD 27%"])
def test_a_percentage_is_a_trim_where_the_channel_says_so(text):
    sig = _p(text, percent_exit=True).signal
    assert sig is not None, text
    assert sig.action.value == "SELL"


@pytest.mark.parametrize("text", ["IWM 287P +52%", "$MSFT 100c +366%"])
def test_the_same_percentage_is_only_an_update_elsewhere(text):
    """Off by default. A channel that posts the same contract at +24%, +45%,
    +60% would otherwise fire three exits for one position."""
    r = _p(text, percent_exit=False)
    assert r.signal is None
    assert "price update" in (r.reason or "")


def test_a_named_contract_survives_the_percent_route():
    sig = _p("IWM 287P +52%", percent_exit=True).signal
    assert (sig.symbol, str(sig.strike), sig.option_type.value) == ("IWM", "287", "PUT")
    assert sig.contract_unspecified is False


def test_a_bare_ticker_percent_resolves_from_the_position():
    """"AMD 27%" names no contract, so execution must read it off the holding."""
    sig = _p("AMD 27%", percent_exit=True).signal
    assert sig.symbol == "AMD"
    assert sig.contract_unspecified is True


def test_the_percentage_is_the_result_not_a_size():
    """"+52%" is how the trade did, never "sell 52% of it"."""
    sig = _p("IWM 287P +52%", percent_exit=True).signal
    assert sig.quantity is None
    assert sig.pnl_percent == Decimal("52")


def test_prose_is_not_traded_even_on_a_percent_channel():
    assert _p("AMD 27% of the float is short", percent_exit=True).signal is None


def test_scissors_still_work_on_a_percent_channel():
    sig = _p("✂️ $SPY 760c +58%", percent_exit=True).signal
    assert sig.action.value == "SELL" and sig.symbol == "SPY"


# ── "light" / "not heavy": the author asking for a smaller entry ─────────────

def _buy(body):
    r = parse_message(text(body))
    assert r.status is ParseStatus.PARSED, (body, r.reason)
    return r.signals[0]


def test_light_flags_a_buy_for_half_size():
    assert _buy("BUY AAPL 250C 09/18 @ 2.15 light").half_size is True


def test_not_heavy_flags_a_buy_for_half_size():
    assert _buy("not heavy — BUY AAPL 250C 09/18 @ 2.15").half_size is True


def test_a_hyphenated_not_heavy_still_counts():
    """People type it both ways."""
    assert _buy("BUY AAPL 250C 09/18 @ 2.15 (not-heavy)").half_size is True


def test_an_ordinary_buy_is_not_flagged():
    assert _buy("BUY AAPL 250C 09/18 @ 2.15").half_size is False


def test_lighten_does_not_flag_a_buy():
    """"Lighten up" is the OPPOSITE instruction — trim a position. A substring
    match on "light" would read it as "open a smaller position"."""
    assert _buy("BUY AAPL 250C 09/18 @ 2.15, will lighten later").half_size is False


def test_light_inside_another_word_does_not_flag():
    assert _buy("BUY AAPL 250C 09/18 @ 2.15 delightful setup").half_size is False


def test_the_flag_is_carried_into_the_signal_dict():
    """discord_execution reads the dict, not the dataclass."""
    assert _buy("BUY AAPL 250C 09/18 @ 2.15 light").as_dict()["half_size"] is True


def test_a_sell_is_not_flagged():
    """An exit sizes from the position held, never from the alert — and here
    "light" most likely means lightening up, which is a trim, not a half-size
    entry. Halving an exit would strand part of a position."""
    r = parse_message(text("SELL AAPL 250C 09/18 @ 3.00 going light"))
    assert r.status is ParseStatus.PARSED
    assert r.signals[0].half_size is False
