"""Every global a poller function loads must actually resolve.

A name that is referenced but never imported raises NameError only when that
line runs. In a background sweep that is invisible: the sweep catches the
exception per account and logs it, so the feature is simply dead while
everything looks healthy.

That is not hypothetical. ``_close`` referenced ``market_hours`` as a bare
global and never imported it, so EVERY Discord position close raised NameError
-- the stop-out, the trailing exit, and the fallback that exits a position when
the broker refuses to hold its stop. Nothing closed for hours, with no error
surfaced anywhere a user could see.

Locally-imported names compile to fast locals, not LOAD_GLOBAL, so the
module's own `from x import y` inside a function satisfies this.
"""
import builtins
import dis
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.discord_stop_orders as stop_orders
import app.services.pnl_poller as pnl_poller


def _loaded_globals(code):
    for ins in dis.get_instructions(code):
        if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            yield ins.argval
    for const in code.co_consts:
        if hasattr(const, "co_names"):
            yield from _loaded_globals(const)


def _unresolved(module):
    missing = set()
    for name, obj in vars(module).items():
        code = getattr(obj, "__code__", None)
        # Only functions DEFINED here: an imported one resolves its globals
        # against its own module, not this one.
        if code is None or getattr(obj, "__module__", None) != module.__name__:
            continue
        for g in _loaded_globals(code):
            if g not in vars(module) and not hasattr(builtins, g):
                missing.add(f"{name}() -> {g}")
    return missing


def test_the_pnl_poller_has_no_unresolved_globals():
    assert _unresolved(pnl_poller) == set()


def test_the_stop_reconciler_has_no_unresolved_globals():
    assert _unresolved(stop_orders) == set()
