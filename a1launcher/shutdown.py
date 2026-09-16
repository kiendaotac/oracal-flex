"""Cooperative shutdown on SIGINT / SIGTERM."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

logger = logging.getLogger("a1launcher.shutdown")


class Shutdown:
    """A signal-aware stop flag with an interruptible sleep.

    Sleeping on a `threading.Event` instead of `time.sleep` means a Ctrl-C or a
    `docker stop` is acted on immediately rather than up to a few minutes later.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self.signal_name: str | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def install(self) -> None:
        """Register handlers for SIGINT and SIGTERM."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handle)

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self.signal_name = signal.Signals(signum).name
        if self._event.is_set():
            logger.warning("%s received again; exiting immediately", self.signal_name)
            raise SystemExit(130)
        logger.info("%s received; finishing up and shutting down cleanly", self.signal_name)
        self._event.set()

    def sleep(self, seconds: float) -> bool:
        """Sleep up to `seconds`. Returns False if shutdown was requested."""
        if seconds <= 0:
            return not self.requested
        interrupted = self._event.wait(seconds)
        return not interrupted
