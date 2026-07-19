"""Startup status panel."""

from __future__ import annotations

import random
import tkinter as tk


PRINCIPLE_TIPS: list[str] = [
    "管理服务在线后即可登录 Apple ID；登录成功后会继续准备解密实例。",
    "首次冷启动可能需要 1 到 2 分钟，软件模拟会更慢一些。",
    "如果 Apple API 异常，可先在设置页配置代理后重试。",
    "查询、下载和本地缓存导入都需要先完成 Apple ID 登录。",
    "下载完成后不会自动退出，可直接继续下一次任务。",
    "状态页可查看区域、吞吐、硬件加速和当前任务状态。",
]


STATE_COLORS = {
    "working": "#007AFF",
    "ready": "#34C759",
    "need_login": "#FF9500",
    "fail": "#FF3B30",
}


def _center_window(win: tk.Misc, width: int, height: int):
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 2)
    win.geometry(f"{width}x{height}+{x}+{y}")


class PrincipleSplash:
    def __init__(self, parent: tk.Misc):
        self.win = tk.Toplevel(parent)
        self.win.title("启动状态")
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#F5F5F7")
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)
        self.win.bind("<Alt-F4>", lambda _e: "break")
        _center_window(self.win, 560, 300)

        self._drag_origin: tuple[int, int] | None = None
        self._last_index = -1

        shell = tk.Frame(self.win, bg="#F5F5F7", highlightthickness=1, highlightbackground="#D2D2D7")
        shell.pack(fill=tk.BOTH, expand=True)
        shell.bind("<ButtonPress-1>", self._begin_drag)
        shell.bind("<B1-Motion>", self._drag)

        header = tk.Frame(shell, bg="#FFFFFF", height=72)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        header.bind("<ButtonPress-1>", self._begin_drag)
        header.bind("<B1-Motion>", self._drag)

        self._dot = tk.Label(header, text="●", fg=STATE_COLORS["working"], bg="#FFFFFF", font=("Segoe UI", 18))
        self._dot.pack(side=tk.LEFT, padx=(24, 10), pady=20)
        self._simple = tk.Label(
            header,
            text="正在启动",
            fg="#1D1D1F",
            bg="#FFFFFF",
            font=("Segoe UI", 16, "bold"),
        )
        self._simple.pack(side=tk.LEFT, pady=20)

        body = tk.Frame(shell, bg="#F5F5F7")
        body.pack(fill=tk.BOTH, expand=True, padx=26, pady=(20, 18))

        self._detail = tk.Label(
            body,
            text="正在检查运行环境并启动本地管理服务…",
            fg="#3A3A3C",
            bg="#F5F5F7",
            justify=tk.LEFT,
            anchor=tk.NW,
            wraplength=500,
            font=("Segoe UI", 10),
        )
        self._detail.pack(fill=tk.BOTH, expand=True)

        self._tip = tk.Label(
            body,
            text="",
            fg="#6E6E73",
            bg="#F5F5F7",
            justify=tk.LEFT,
            anchor=tk.SW,
            wraplength=500,
            font=("Segoe UI", 9),
        )
        self._tip.pack(fill=tk.X, pady=(18, 0))

        self._show_random_tip()
        self._schedule()

    def _begin_drag(self, event):
        self._drag_origin = (event.x_root - self.win.winfo_x(), event.y_root - self.win.winfo_y())

    def _drag(self, event):
        if not self._drag_origin:
            return
        dx, dy = self._drag_origin
        self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _show_random_tip(self):
        if len(PRINCIPLE_TIPS) <= 1:
            idx = 0
        else:
            choices = [i for i in range(len(PRINCIPLE_TIPS)) if i != self._last_index]
            idx = random.choice(choices)
        self._last_index = idx
        self._tip.config(text=PRINCIPLE_TIPS[idx])

    def _schedule(self):
        if self.win.winfo_exists():
            self._show_random_tip()
            self.win.after(6500, self._schedule)

    def set_status(self, message: str):
        if not self.win.winfo_exists():
            return
        detail = (message or "正在启动…").strip()
        simple, kind = self._classify(detail)
        color = STATE_COLORS[kind]
        self._dot.config(fg=color)
        self._simple.config(text=simple)
        self._detail.config(text=detail)

    @staticmethod
    def _classify(message: str) -> tuple[str, str]:
        if any(token in message for token in ("失败", "异常", "错误", "Missing", "Timeout")):
            return "需要处理", "fail"
        if any(token in message for token in ("就绪", "在线", "可登录", "ready")):
            return "服务在线", "ready"
        if "登录" in message and not any(token in message for token in ("正在", "等待")):
            return "等待登录", "need_login"
        if any(token in message for token in ("下载", "解密", "导入")):
            return "正在处理", "working"
        return "正在启动", "working"

    def destroy(self):
        if self.win.winfo_exists():
            self.win.destroy()
