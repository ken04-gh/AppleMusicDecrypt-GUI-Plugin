"""Thread-safe log lines from loguru into the tkinter GUI."""

from __future__ import annotations

import queue
from typing import Callable, Optional

_log_queue: queue.Queue[str] = queue.Queue()
_sink_callback: Optional[Callable[[str], None]] = None


def emit_log_line(line: str) -> None:
    text = str(line).rstrip("\n")
    if not text:
        return
    if _sink_callback is not None:
        try:
            _sink_callback(text)
            return
        except Exception:
            pass
    _log_queue.put(text)


def set_log_sink(callback: Optional[Callable[[str], None]]) -> None:
    global _sink_callback
    _sink_callback = callback


def drain_pending_logs() -> list[str]:
    lines: list[str] = []
    while True:
        try:
            lines.append(_log_queue.get_nowait())
        except queue.Empty:
            break
    return lines