"""Is this "add" the trader averaging in NOW — and into what?

Every message that says "add" on QA (tests/fixtures/discord_add_messages.json,
36 of them across Julia, Clint, Breakdownsniper and Mark) splits three ways:
real adds, entries that only mention adding ("Leave room to add"), and talk
("Add point will be 769.20", "Will be adding to SPY around 776", "No adds to
TSLA YET", "Something doesn't add up"). The adds that named no ticker and no
price — Julia's "Added to my position", Mark's "Added", "Added here", "Last
Add", "Added to lotto" — were ignored, so nobody averaged in.

A committed add is recognised by how its clause STARTS, never by "add"
appearing somewhere in it:

    "Added", "Added here", "Added to my position", "Added to Meta",
    "Adding a couple more on original TSLA entry", "I've added",
    "Last add", "This is my last add here"

and is then refused by anything that makes it talk: a condition, a hedge, a
negation, a question or advice close to the word. That keeps "risky add",
"add point", "add up", "will be adding" and "going to start adding" out
without a list of every way to say them.

Pure text in, a verdict out. Used by terse_alert (the no-price add) and
generic_text (which must never read an add as a fresh entry).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ADD = "add"
BLOCKED = "blocked"
NONE = "none"

_ADD_WORD_RE = re.compile(r"\badd(?:s|ed|ing)?\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\s?\w+")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+|;\s*|\s+but\s+", re.IGNORECASE)

# The clause opens with the add itself — optionally after "I" / "I've" / "just".
_COMMITTED_RE = re.compile(
    r"^\W*(?:(?:i|we)(?:'ve|\s+have)?\s+|just\s+|(?:i|we)\s+just\s+)?(?:added|adding)\b"
    r"|^\W*(?:this\s+is\s+)?(?:my\s+)?last\s+add\b",
    re.IGNORECASE,
)

# Each blocker counts only within a few words AFTER the add word (the clause
# opens with it, so nothing that matters comes before): "Adding .50 here new avg
# .73 if you want I truly don't think…" is an add — its "if", "you" and "don't"
# are about something else, further on.
_AFTER = 4
_BLOCKERS: tuple[tuple[str, re.Pattern], ...] = (
    ("negated", re.compile(r"\b(?:not|no|never|don't|dont|didn't|didnt|won't|wont)\b")),
    ("conditional", re.compile(
        r"\b(?:if|unless|once|when|around|below|under|above|soon|later|tomorrow|tmrw|eventually)\b")),
    ("hedged", re.compile(
        r"\b(?:will|going to|gonna|might|may|could|would|likely|planning|plan to|thinking|"
        r"looking to|want to|about to|maybe|probably|perhaps)\b")),
    ("idiom", re.compile(r"^up\b")),
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|\?")
# "not heavy" means light, not "not adding".
_NOT_HEAVY_RE = re.compile(r"\bnot[\s-]+heavy\b", re.IGNORECASE)


def _blocker(bare: str) -> str | None:
    words = _WORD_RE.findall(_NOT_HEAVY_RE.sub(" light ", bare).lower())
    at = next((i for i, w in enumerate(words) if _ADD_WORD_RE.fullmatch(w)), None)
    if at is None:
        return None
    after = words[at + 1:at + 1 + _AFTER]
    if "?" in after or (words and words[-1] == "?"):
        return "question"
    window = " ".join(w for w in after if w != "?")
    for name, pattern in _BLOCKERS:
        if pattern.search(window):
            return name
    return None

# A ticker the add names: "$IWM", "IWM", or one typed in another case right
# after "to"/"on" ("Added to Meta"). Words that are not tickers never count.
_NOT_TICKERS = {
    "ADD", "ADDED", "ADDING", "LAST", "MY", "TO", "ON", "THE", "HERE", "NOW", "MORE", "SOME",
    "A", "I", "THIS", "IS", "IT", "ENTRY", "ORIGINAL", "POSITION", "LOTTO", "LOTTOS", "AS",
    "WELL", "STILL", "RISKY", "LIGHT", "AND", "COUPLE", "FEW", "NEW", "AVG", "AVERAGE",
    "CALL", "CALLS", "PUT", "PUTS", "ODTE", "DTE", "AT", "OF", "IN", "FOR", "JUST", "WE",
}
_CAPS_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$?(?P<sym>[A-Z]{1,5})(?![A-Za-z0-9])")
_DOLLAR_TICKER_RE = re.compile(r"\$(?P<sym>[A-Za-z]{1,5})\b")
_AFTER_TO_RE = re.compile(r"\badd(?:ed|ing)?\s+(?:to|on)\s+(?P<sym>[A-Za-z]{1,5})\b", re.IGNORECASE)

# A message this long is commentary unless the add clause is plainly a trade.
_LONG = 300
_STRONG_RE = re.compile(r"\bmy\b|\$[A-Za-z]{1,5}\b|\b[A-Z]{2,5}\b|\b(?:here|now)\b|@\s*\d")


@dataclass(frozen=True)
class AddIntent:
    kind: str                     # ADD | BLOCKED | NONE
    reason: str
    clause: str | None = None
    symbol: str | None = None     # None = the position this channel is in

    @property
    def adds(self) -> bool:
        return self.kind == ADD


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(text or "") if c and c.strip()]


# "Added to …": a ticker, or the author's own position. Anything else is not a
# position ("Added to POSITIONS", "Added to the list", "Added to watchlist").
_TO_TARGET_RE = re.compile(r"\badd(?:ed|ing)?\s+to\s+(?P<target>[$\w]+(?:\s+\w+)?)", re.IGNORECASE)
_OWN_POSITION_RE = re.compile(
    r"^(?:my|the|this|our)\s+(?:position|positions|lotto|lottos|calls?|puts?|trade|contracts?)\b"
    r"|^(?:it|lotto|lottos|position)\b", re.IGNORECASE)
# A price in the add clause: those adds belong to the price-based rules
# (compact_alert / terse_alert's corroborated mid-sentence add).
_PRICE_RE = re.compile(r"(?<![\w.])@?\s*\$?\d*\.\d+\b|@\s*\$?\d+\b")


def _bad_target(bare: str) -> bool:
    m = _TO_TARGET_RE.search(bare)
    if not m:
        return False
    target = m.group("target")
    if _OWN_POSITION_RE.search(target):
        return False
    if _DOLLAR_TICKER_RE.search(target):
        return False
    first = target.split()[0]
    # A ticker as people type one: capitals ("META") or capitalised ("Meta").
    looks_like_ticker = (first.isalpha() and 1 <= len(first) <= 5
                         and (first.isupper() or first[:1].isupper())
                         and first.upper() not in _NOT_TICKERS)
    return not looks_like_ticker


def _symbol(clause: str) -> str | None:
    bare = _MENTION_RE.sub(" ", clause)
    m = _DOLLAR_TICKER_RE.search(bare)
    if m:
        return m.group("sym").upper()
    for m in _CAPS_TICKER_RE.finditer(bare):
        if m.group("sym") not in _NOT_TICKERS:
            return m.group("sym")
    m = _AFTER_TO_RE.search(bare)
    if m and m.group("sym").upper() not in _NOT_TICKERS:
        return m.group("sym").upper()
    return None


def classify(text: str) -> AddIntent:
    """Read a message for an add. NONE when no clause commits to one."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    if not _ADD_WORD_RE.search(text):
        return AddIntent(NONE, "no add")
    long_message = len(text) > _LONG
    first_block: AddIntent | None = None
    for clause in _clauses(text):
        bare = _MENTION_RE.sub(" ", clause).strip()
        if not _COMMITTED_RE.search(bare):
            continue
        blocked = _blocker(bare)
        if blocked is None and _bad_target(bare):
            blocked = "not a position"
        if blocked is None and _PRICE_RE.search(bare):
            blocked = "priced (the price rules read it)"
        if blocked is not None:
            first_block = first_block or AddIntent(BLOCKED, blocked, clause)
            continue
        if long_message and not _STRONG_RE.search(bare):
            first_block = first_block or AddIntent(BLOCKED, "commentary", clause)
            continue
        return AddIntent(ADD, "adding now", clause, _symbol(bare))
    return first_block or AddIntent(NONE, "no committed add")


__all__ = ["ADD", "BLOCKED", "NONE", "AddIntent", "classify"]
