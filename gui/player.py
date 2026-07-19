"""Lightweight local audio player for Windows (MCI) with play/pause/stop/seek."""

from __future__ import annotations

import ctypes
import threading
import time
from pathlib import Path
from typing import Callable, Optional


class MciPlayer:
    """Professional-enough transport using winmm MCI (no extra deps)."""

    def __init__(self):
        self._alias = "amdpreview"
        self._path: Optional[Path] = None
        self._lock = threading.RLock()
        self._paused = False
        self._playing = False
        self._winmm = ctypes.windll.winmm if hasattr(ctypes, "windll") else None

    def _mci(self, cmd: str) -> str:
        if not self._winmm:
            raise RuntimeError("当前系统不支持 MCI 播放")
        buf = ctypes.create_unicode_buffer(512)
        err = self._winmm.mciSendStringW(cmd, buf, 511, 0)
        if err:
            err_buf = ctypes.create_unicode_buffer(256)
            self._winmm.mciGetErrorStringW(err, err_buf, 255)
            raise RuntimeError(err_buf.value or f"MCI error {err}")
        return buf.value

    def close(self):
        with self._lock:
            try:
                self._mci(f"close {self._alias}")
            except Exception:
                pass
            self._path = None
            self._playing = False
            self._paused = False

    def open(self, path: str | Path):
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        with self._lock:
            self.close()
            p = str(path).replace("/", "\\")
            ext = path.suffix.lower()
            errors: list[str] = []
            # Prefer codec-appropriate MCI device; fall back through common types
            if ext == ".wav":
                candidates = [
                    f'open "{p}" type waveaudio alias {self._alias}',
                    f'open "{p}" alias {self._alias}',
                    f'open "{p}" type mpegvideo alias {self._alias}',
                ]
            else:
                candidates = [
                    f'open "{p}" type mpegvideo alias {self._alias}',
                    f'open "{p}" alias {self._alias}',
                    f'open "{p}" type waveaudio alias {self._alias}',
                ]
            opened = False
            for cmd in candidates:
                try:
                    self._mci(cmd)
                    opened = True
                    break
                except Exception as e:
                    errors.append(str(e))
                    try:
                        self._mci(f"close {self._alias}")
                    except Exception:
                        pass
            if not opened:
                raise RuntimeError(errors[-1] if errors else "无法打开音频")
            try:
                self._mci(f"set {self._alias} time format milliseconds")
            except Exception:
                pass
            self._path = path
            self._playing = False
            self._paused = False

    def play(self, from_ms: Optional[int] = None):
        with self._lock:
            if not self._path:
                raise RuntimeError("未加载音频")
            if from_ms is not None:
                self._mci(f"play {self._alias} from {max(0, int(from_ms))}")
            elif self._paused:
                self._mci(f"resume {self._alias}")
            else:
                self._mci(f"play {self._alias}")
            self._playing = True
            self._paused = False

    def pause(self):
        with self._lock:
            if not self._playing or self._paused:
                return
            try:
                self._mci(f"pause {self._alias}")
            except Exception:
                self._mci(f"stop {self._alias}")
            self._paused = True

    def stop(self):
        """Hard stop (■) — reset to start, not resume-able as pause."""
        with self._lock:
            if not self._path:
                return
            try:
                self._mci(f"stop {self._alias}")
            except Exception:
                pass
            try:
                self._mci(f"seek {self._alias} to start")
            except Exception:
                pass
            self._playing = False
            self._paused = False

    def seek_ms(self, ms: int):
        with self._lock:
            if not self._path:
                return
            ms = max(0, int(ms))
            length = self.length_ms()
            if length > 0:
                ms = min(ms, length)
            was_playing = self._playing and not self._paused
            try:
                self._mci(f"seek {self._alias} to {ms}")
            except Exception:
                # fallback play-from
                if was_playing:
                    self._mci(f"play {self._alias} from {ms}")
                    return
            if was_playing:
                self._mci(f"play {self._alias}")
                self._playing = True
                self._paused = False

    def position_ms(self) -> int:
        with self._lock:
            if not self._path:
                return 0
            try:
                v = self._mci(f"status {self._alias} position")
                return int(v or 0)
            except Exception:
                return 0

    def length_ms(self) -> int:
        with self._lock:
            if not self._path:
                return 0
            try:
                v = self._mci(f"status {self._alias} length")
                return int(v or 0)
            except Exception:
                return 0

    def is_playing(self) -> bool:
        with self._lock:
            if not self._path or not self._playing or self._paused:
                return False
            try:
                mode = (self._mci(f"status {self._alias} mode") or "").lower()
                if "stop" in mode:
                    self._playing = False
                    return False
                return "play" in mode
            except Exception:
                return self._playing and not self._paused

    def is_paused(self) -> bool:
        return self._paused

    @property
    def path(self) -> Optional[Path]:
        return self._path
