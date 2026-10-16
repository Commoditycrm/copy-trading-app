"""Is this "cut" an instruction to get out NOW?

A trader's "cut" is the strongest exit there is — but the word turns up far more
often in talk ABOUT cutting than in a cut. Every message from Rafael, Clint and
Breakdownsniper that says "cut" (tests/fixtures/discord_cut_messages.json, 24 of
them) split 10 real cuts / 14 not, and reading any "cut" as a full close sold on
six of the fourteen:

    "Will cut if we close below my downside test level"     conditional
    "If we don't get a bounce shortly I'm going to have to cut calls"
    "Smh those SPY we cut earlier went to .75"               past
    "Hopefully someone caught AMD and didn't cut"            negated
    "we must cut losers eventually"                          a maxim
    "cut losers quickly hold winners longer"                 a maxim

So each clause that says "cut" is read on its own, and only one that commits —
the author's own, present-tense, nothing hedging it — makes the message a cut:

    cut      "Cutting fly", "Cut AMZN", "Cut my SPY puts here",
             "Going to cut remaining position here @ 2.6"   (here = now)
    partial  "cut half", "cutting a third", "cut 30%"
    blocked  conditional / hedged / negated / past / advice — never sells

Pure text in, a verdict out: no parser state, no I/O. Used by terse_alert (the
cut branch) and generic_text (so a cut is never read as an entry).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

CUT = "cut"
PARTIAL = "partial"
BLOCKED = "blocked"
NONE = "none"

_CUT_WORD_RE = re.compile(r"\bcut(?:s|ting)?\b", re.IGNORECASE)

# A "cut" clause is read within its own sentence. "but" splits too: "Holding it
# for now, but lets see" must not lend its hedge to a cut in the next clause,
# nor take one from it.
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+|;\s*|,\s*(?=but\b)|\s+but\s+", re.IGNORECASE)

_MENTION_RE = re.compile(r"@\s?\w+")

# ── what blocks a clause ─────────────────────────────────────────────────────
# Each blocker counts only CLOSE to the word "cut" — a window of words before and
# after it — because the same words turn up innocently elsewhere in a clause:
# "Cutting of course I trade WHEN not feeling well", "Cutting @here BREAK even".
# (name, words before, words after, pattern over that window)
_BLOCKERS: tuple[tuple[str, int, int, re.Pattern], ...] = (
    ("negated", 3, 0, re.compile(
        r"\b(?:not|never|no|won't|wont|don't|dont|didn't|didnt|wouldn't|isn't|aren't|ain't)\b")),
    ("conditional", 4, 3, re.compile(
        r"\b(?:if|unless|once|when|whenever|below|under|shortly|soon|later|eventually)\b")),
    ("hedged", 4, 0, re.compile(
        r"\b(?:may|might|could|would|maybe|perhaps|probably|possibly|likely|thinking|considering|"
        r"consider|planning|plan|want|have to|has to|will|gonna|going to|about to|ready to|best to)\b")),
    ("past", 5, 3, re.compile(
        r"\b(?:earlier|already|yesterday|before|ago|had|was|were|been|would've|should've|could've|"
        r"rather than|instead of|cleared)\b")),
)
# Whole-clause blockers: a question, a clause that opens on a condition, and
# advice to followers or a trading maxim rather than the author's own trade.
_QUESTION_RE = re.compile(r"\?|\b(?:should|shall)\s+(?:i|we)\s+cut", re.IGNORECASE)
_OPENS_CONDITIONAL_RE = re.compile(r"^\W*(?:if|unless|when|once|in\s+case)\b", re.IGNORECASE)
_ADVICE_RE = re.compile(
    r"\byou\b|\bcut\w*\s+(?:your\s+)?(?:losers?|losses)\b|\bmust\b|\balways\b", re.IGNORECASE)

# "here" / "now" / a price: the author is doing it as they type. Overrides a
# future-tense hedge ("Going to cut remaining position here @ 2.6"), never a
# condition, a negation, a question or the past.
_NOW_RE = re.compile(r"\b(?:here|now|right\s+now)\b|@\s*\$?\d*\.?\d+", re.IGNORECASE)
_NOW_OVERRIDES = {"hedged"}
_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

# Partial: "cut half", "cutting a third", "cut 1/3", "cut 30%", "cut some".
# A percent right after "loss"/"gain" — or a signed one ("-30%") — is the
# trade's RESULT ("Cutting @here 10% loss"), not a size.
_PARTIAL_RE = re.compile(
    r"\bcut(?:s|ting)?\s+(?:(?:my|the|our)\s+)?"
    r"(?P<frac>half|a\s+half|1\s*/\s*2|a\s+third|one\s+third|1\s*/\s*3|two\s+thirds|2\s*/\s*3|"
    r"a\s+quarter|1\s*/\s*4|some|a\s+few|(?P<pct>\d{1,2})\s*%(?!\s*(?:loss|gain|profit|down|up)))",
    re.IGNORECASE,
)
_FRACTIONS = {
    "half": Decimal("0.5"), "a half": Decimal("0.5"), "1/2": Decimal("0.5"),
    "a third": Decimal(1) / 3, "one third": Decimal(1) / 3, "1/3": Decimal(1) / 3,
    "two thirds": Decimal(2) / 3, "2/3": Decimal(2) / 3,
    "a quarter": Decimal("0.25"), "1/4": Decimal("0.25"),
    "some": Decimal("0.5"), "a few": Decimal("0.5"),
}

# A message this long is commentary unless the cut clause is plainly a trade:
# names the author's own position ("my"), a ticker, "here"/"now" or a price.
_LONG = 300
_STRONG_RE = re.compile(r"\bmy\b|\$[A-Za-z]{1,5}\b|\b[A-Z]{2,5}\b|\b(?:here|now)\b|@\s*\d", re.UNICODE)


@dataclass(frozen=True)
class CutIntent:
    kind: str                       # CUT | PARTIAL | BLOCKED | NONE
    reason: str                     # why, for the audit trail
    clause: str | None = None       # the clause the verdict rests on
    fraction: Decimal | None = None # PARTIAL only

    @property
    def sells(self) -> bool:
        return self.kind in (CUT, PARTIAL)


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(text or "") if c and c.strip()]


def _normalise(text: str) -> str:
    # Curly apostrophes ("don’t", "I’m") read as straight ones.
    return (text or "").replace("’", "'").replace("‘", "'")


def _blocker(clause: str) -> str | None:
    """Why this "cut" clause is not a cut, or None when it commits."""
    bare = _MENTION_RE.sub(" ", clause)
    if _QUESTION_RE.search(bare):
        return "question"
    if _OPENS_CONDITIONAL_RE.search(bare):
        return "conditional"
    if _ADVICE_RE.search(bare):
        return "advice"
    words = _WORD_RE.findall(bare.lower())
    cut_at = next((i for i, w in enumerate(words) if _CUT_WORD_RE.fullmatch(w)), None)
    if cut_at is None:
        return None
    now = bool(_NOW_RE.search(bare))
    for name, before, after, pattern in _BLOCKERS:
        window = " ".join(words[max(0, cut_at - before):cut_at] + words[cut_at + 1:cut_at + 1 + after])
        if pattern.search(window):
            if name in _NOW_OVERRIDES and now:
                continue
            return name
    return None


def classify(text: str) -> CutIntent:
    """Read a message for a cut. NONE when it never says "cut"."""
    text = _normalise(text)
    if not _CUT_WORD_RE.search(text):
        return CutIntent(NONE, "no cut")
    long_message = len(text) > _LONG
    first_block: CutIntent | None = None
    for clause in _clauses(text):
        if not _CUT_WORD_RE.search(clause):
            continue
        blocked = _blocker(clause)
        if blocked is not None:
            first_block = first_block or CutIntent(BLOCKED, blocked, clause)
            continue
        if long_message and not _STRONG_RE.search(_MENTION_RE.sub(" ", clause)):
            first_block = first_block or CutIntent(BLOCKED, "commentary", clause)
            continue
        part = _PARTIAL_RE.search(clause)
        if part:
            if part.group("pct"):
                fraction = Decimal(part.group("pct")) / 100
            else:
                fraction = _FRACTIONS.get(re.sub(r"\s+", " ", part.group("frac").lower()).replace(" / ", "/"))
            if fraction is not None and 0 < fraction < 1:
                return CutIntent(PARTIAL, f"cutting {part.group('frac')}", clause, fraction)
        return CutIntent(CUT, "cutting now", clause)
    return first_block or CutIntent(BLOCKED, "no committed cut")


__all__ = ["CUT", "PARTIAL", "BLOCKED", "NONE", "CutIntent", "classify"]
