"""Kopyaa Connector — a small desktop app that connects a trader's Discord
account to Kopyaa.

Tkinter rather than a web UI or a heavier toolkit: it ships with Python, adds
nothing to the download, and this window has three widgets. The app exists to
remove a terminal and a file from the trader's path, not to be pretty.

Threading: the capture opens a browser and blocks for as long as the trader
takes to sign in. That runs on a worker thread; Tk is not thread-safe, so status
updates are marshalled back with ``after()``.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import ttk

from .capture import CaptureError, capture_session, claim_code, upload_session

DEFAULT_BACKEND = os.environ.get("KOPYAA_BACKEND_URL", "https://app.kopyaa.com")

_BG = "#0f1115"
_FG = "#e6e8eb"
_MUTED = "#9aa3ad"
_ACCENT = "#5865f2"   # Discord blurple
_ERROR = "#f87171"
_OK = "#4ade80"


class ConnectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.busy = False
        root.title("Kopyaa Connector")
        root.configure(bg=_BG)
        root.geometry("460x340")
        root.resizable(False, False)

        wrap = tk.Frame(root, bg=_BG, padx=28, pady=24)
        wrap.pack(fill="both", expand=True)

        tk.Label(
            wrap, text="Connect Discord", bg=_BG, fg=_FG,
            font=("Helvetica", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            wrap,
            text="Enter the code shown in Kopyaa, then sign in to\nDiscord in the browser window that opens.",
            bg=_BG, fg=_MUTED, font=("Helvetica", 11), justify="left",
        ).pack(anchor="w", pady=(6, 18))

        tk.Label(wrap, text="Pairing code", bg=_BG, fg=_MUTED,
                 font=("Helvetica", 10)).pack(anchor="w")
        self.code_var = tk.StringVar()
        self.entry = tk.Entry(
            wrap, textvariable=self.code_var, font=("Menlo", 16),
            bg="#1a1d23", fg=_FG, insertbackground=_FG,
            relief="flat", justify="center",
        )
        self.entry.pack(fill="x", ipady=8, pady=(6, 4))
        self.entry.focus_set()
        self.entry.bind("<Return>", lambda _e: self.connect())

        tk.Label(wrap, text="e.g. KPY-4F2A-9C1D", bg=_BG, fg=_MUTED,
                 font=("Helvetica", 9)).pack(anchor="w")

        self.button = tk.Button(
            wrap, text="Connect", command=self.connect,
            bg=_ACCENT, fg="white", activebackground=_ACCENT,
            activeforeground="white", relief="flat",
            font=("Helvetica", 12, "bold"), cursor="hand2",
        )
        self.button.pack(fill="x", ipady=8, pady=(18, 10))

        self.progress = ttk.Progressbar(wrap, mode="indeterminate")

        self.status = tk.Label(
            wrap, text="", bg=_BG, fg=_MUTED, font=("Helvetica", 11),
            wraplength=400, justify="left",
        )
        self.status.pack(anchor="w", fill="x")

        # Only surfaced when it's actually needed — a backend field on the main
        # screen would just be a thing to get wrong.
        self.backend = DEFAULT_BACKEND

    # ── UI helpers (main thread only) ───────────────────────────────────────

    def set_status(self, text: str, color: str = _MUTED) -> None:
        self.status.configure(text=text, fg=color)

    def _from_worker(self, text: str, color: str = _MUTED) -> None:
        """Status update from the capture thread. Tk is not thread-safe, so hop
        back to the main loop."""
        self.root.after(0, lambda: self.set_status(text, color))

    def _finish(self, text: str, color: str) -> None:
        def done() -> None:
            self.busy = False
            self.progress.stop()
            self.progress.pack_forget()
            self.button.configure(state="normal", text="Connect")
            self.set_status(text, color)
        self.root.after(0, done)

    # ── Flow ────────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if self.busy:
            return
        code = self.code_var.get().strip()
        if not code:
            self.set_status("Enter the pairing code from Kopyaa.", _ERROR)
            return

        self.busy = True
        self.button.configure(state="disabled", text="Connecting…")
        self.progress.pack(fill="x", pady=(0, 10))
        self.progress.start(12)
        self.set_status("Checking the code…")

        threading.Thread(target=self._run, args=(code,), daemon=True).start()

    def _run(self, code: str) -> None:
        try:
            claim = claim_code(self.backend, code)
            self._from_worker(f"Connecting “{claim['label']}”. Opening browser…")

            state = capture_session(self._from_worker)

            self._from_worker("Uploading to Kopyaa…")
            upload_session(self.backend, code, claim["upload_token"], state)

            self._finish(
                "Connected. You can close this window — Kopyaa is now "
                "monitoring the channel.",
                _OK,
            )
        except CaptureError as exc:
            self._finish(str(exc), _ERROR)
        except Exception as exc:  # noqa: BLE001
            self._finish(f"Something went wrong: {exc}", _ERROR)


def main() -> None:
    root = tk.Tk()
    ConnectorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
