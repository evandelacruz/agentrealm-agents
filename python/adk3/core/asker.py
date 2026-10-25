"""The strategist off the decision thread (docs/v3/SPEC.md **Strategist**).

One call at a time runs on its own thread; play goes on with the last
answer until the next lands, so a slow model never costs a tick.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class Asker:
    def __init__(self, call: Callable[[Any], Any]):
        self.call = call
        self._thread: threading.Thread | None = None
        self._reply: Any = None
        self._error = ""
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ask(self, briefing: Any) -> bool:
        """Start a call with ``briefing``; False when one is already out."""
        if self.busy():
            return False

        def run() -> None:
            try:
                reply, error = self.call(briefing), ""
            except Exception as e:  # a failed call must never stop play
                reply, error = None, f"{type(e).__name__}: {e}"
            with self._lock:
                self._reply, self._error = reply, error

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return True

    def take(self) -> tuple[Any, str]:
        """The reply that landed since the last take (None when none), and the call's error."""
        with self._lock:
            reply, error, self._reply, self._error = self._reply, self._error, None, ""
        return reply, error
