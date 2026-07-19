"""Task-Manager-style adaptive sparkline charts for speed history."""

from __future__ import annotations

import tkinter as tk
from typing import Sequence


class SpeedChart(tk.Canvas):
    """Single-series line chart that auto-scales to its container size."""

    def __init__(self, master, title: str, unit: str = "kB/s", height: int = 100, **kwargs):
        super().__init__(master, height=height, highlightthickness=1, highlightbackground="#ccc", **kwargs)
        self._title = title
        self._unit = unit
        self._values: list[float] = []
        self._current_kb_s = 0.0
        self._current_label = f"0 {unit}"
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_values(self, values: Sequence[float], current_text: str = ""):
        self._values = list(values)[-120:]
        if current_text:
            self._current_label = current_text
            try:
                value, unit = self._parse_speed_text(current_text)
                self._current_kb_s = value * 1024 if unit.lower().startswith("mb") else value
            except ValueError:
                self._current_kb_s = self._values[-1] if self._values else 0.0
        elif self._values:
            self._current_kb_s = self._values[-1]
            self._current_label = self._format_speed(self._current_kb_s)
        else:
            self._current_kb_s = 0.0
            self._current_label = self._format_speed(0.0)
        self._redraw()

    @staticmethod
    def _parse_speed_text(text: str) -> tuple[float, str]:
        parts = (text or "").strip().split()
        if not parts:
            raise ValueError("empty speed")
        return float(parts[0]), parts[1] if len(parts) > 1 else "kB/s"

    @staticmethod
    def _format_speed(kb_s: float) -> str:
        if kb_s >= 1024:
            return f"{kb_s / 1024:.2f} MB/s"
        return f"{kb_s:.2f} kB/s"

    def _redraw(self):
        self.delete("all")
        w = max(self.winfo_width(), 120)
        h = max(self.winfo_height(), 60)
        pad_l, pad_r, pad_t, pad_b = 44, 8, 22, 18
        plot_w = max(w - pad_l - pad_r, 20)
        plot_h = max(h - pad_t - pad_b, 20)

        self.create_text(8, 10, anchor=tk.NW, text=self._title, font=("", 9, "bold"), fill="#222")
        self.create_text(w - 8, 10, anchor=tk.NE, text=self._current_label, font=("", 9), fill="#0066cc")

        x0, y0 = pad_l, pad_t
        x1, y1 = pad_l + plot_w, pad_t + plot_h
        self.create_rectangle(x0, y0, x1, y1, outline="#d0d0d0", fill="#fafafa")

        vals = self._values if self._values else [0.0]
        peak_kb_s = max(max(vals), self._current_kb_s, 1.0) * 1.15
        display_unit = "MB/s" if peak_kb_s >= 1024 else self._unit
        scale = 1024.0 if display_unit == "MB/s" else 1.0
        peak = peak_kb_s / scale
        plot_vals = [v / scale for v in vals]
        n = len(vals)
        if n == 1:
            xs = [x0, x1]
            ys = [y1 - (plot_vals[0] / peak) * plot_h, y1 - (plot_vals[0] / peak) * plot_h]
        else:
            xs = [x0 + (i / (n - 1)) * plot_w for i in range(n)]
            ys = [y1 - (v / peak) * plot_h for v in plot_vals]

        if len(xs) >= 2:
            points = []
            for x, y in zip(xs, ys):
                points.extend([x, y])
            self.create_line(*points, fill="#0078d4", width=1.5, smooth=True)

        self.create_line(x0, y1, x1, y1, fill="#bbb")
        peak_label = f"{peak:.1f}" if display_unit == "MB/s" else f"{peak:.0f}"
        self.create_text(x0 - 4, y0 + 2, anchor=tk.NE, text=peak_label, font=("", 7), fill="#888")
        self.create_text(x0 - 4, y1 - 2, anchor=tk.SE, text="0", font=("", 7), fill="#888")
        self.create_text(x1, y1 + 2, anchor=tk.NE, text=display_unit, font=("", 7), fill="#888")
