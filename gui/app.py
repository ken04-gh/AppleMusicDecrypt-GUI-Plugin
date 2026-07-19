"""AppleMusicDecrypt graphical user interface."""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional

from gui.backend import BackendService, SongQualityInfo
from gui.errors import format_error
from gui.eta import EtaTracker
from gui.log_handler import set_log_sink
from gui.player import MciPlayer
from gui.principle_splash import PrincipleSplash
from gui.speed_chart import SpeedChart

CODECS = ["alac", "ec3", "ac3", "aac", "aac-binaural", "aac-downmix", "aac-legacy"]
SONG_COLUMNS = ("sel", "title", "artist", "quality", "status")
CACHE_COLUMNS = ("sel", "title", "artist", "album", "codec", "integrity", "status", "note")

# UI color tokens (status / lifecycle)
COLOR = {
    "starting": "#007AFF",
    "ready": "#34C759",
    "need_login": "#FF9500",
    "logged_in": "#34C759",
    "working": "#5856D6",
    "done": "#34C759",
    "fail": "#FF3B30",
    "muted": "#6E6E73",
    "info": "#1D1D1F",
}

UI_BG = "#F5F5F7"
UI_SURFACE = "#FFFFFF"
UI_BORDER = "#D2D2D7"
UI_TEXT = "#1D1D1F"
UI_SUBTLE = "#86868B"
UI_ACCENT = "#007AFF"
UI_FONT = "Segoe UI"


def mask_account(account: str) -> str:
    """Privacy mask: keep first 4 and last 4 chars when long enough."""
    s = (account or "").strip()
    if not s:
        return ""
    n = len(s)
    if n <= 4:
        return "*" * n
    if n <= 8:
        return s[:2] + ("*" * (n - 4)) + s[-2:]
    return s[:4] + ("*" * (n - 8)) + s[-4:]


def center_window(win: tk.Misc, width: Optional[int] = None, height: Optional[int] = None):
    """Place window at the center of the current screen."""
    win.update_idletasks()
    w = width or win.winfo_width()
    h = height or win.winfo_height()
    if w <= 1 or h <= 1:
        geom = win.winfo_geometry()
        try:
            size = geom.split("+", 1)[0]
            w, h = (int(x) for x in size.split("x"))
        except Exception:
            w, h = 960, 720
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - w) // 2)
    y = max(0, (sh - h) // 2)
    win.geometry(f"{w}x{h}+{x}+{y}")


def set_windows_app_id():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AppleMusicDecrypt.GUI.TouchIDLogoV3",
        )
    except Exception:
        pass


class AppleMusicGUI:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.backend = BackendService(base_dir)
        self._ui_queue: queue.Queue = queue.Queue()
        self._quality_results: list[SongQualityInfo] = []
        self._song_selected: dict[str, bool] = {}
        self._song_status: dict[str, str] = {}
        self._principle_splash: Optional[PrincipleSplash] = None
        self._2fa_event = threading.Event()
        self._2fa_action = "submit"
        self._2fa_code = ""
        self._2fa_dialog: Optional[tk.Toplevel] = None
        self._log_line_count = 0
        self._max_log_lines = 2000
        self._download_batch_total = 0
        self._download_batch_ids: set[str] = set()
        self._download_batch_finished: set[str] = set()
        self._task_finished = False
        self._exiting = False
        self._lifecycle = "starting"
        self._boot_phase = "starting"  # starting | online | ready
        self._account_plain: Optional[str] = None  # real Apple ID; never show raw after login
        self._account_locked = False
        self._eta_quality = EtaTracker()
        self._eta_download = EtaTracker()
        self._eta_tick_job = None
        self._search_matches: list[str] = []
        self._search_idx = -1
        self._cache_candidates: list[dict] = []
        self._cache_selected: dict[str, bool] = {}
        self._cache_status: dict[str, str] = {}
        self._cache_adam_to_candidates: dict[str, set[str]] = {}
        self._cache_import_total = 0
        self._cache_import_finished: set[str] = set()
        self._player = MciPlayer()
        self._preview_song_id: Optional[str] = None
        self._preview_loading = False
        self._seek_dragging = False

        set_windows_app_id()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Apple Music Decrypt")
        self._apply_window_icon()
        self._configure_style()
        self.root.minsize(860, 620)
        center_window(self.root, 1040, 760)

        set_log_sink(lambda line: self._ui_queue.put(("log_line", line)))
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._process_ui_queue)
        self.root.after(400, self._eta_tick_loop)
        self._apply_native_windows_icon()
        self.root.deiconify()
        self.root.after(120, self._apply_native_windows_icon)
        self._principle_splash = PrincipleSplash(self.root)
        self.root.after(200, self._start_backend)

    def _apply_window_icon(self):
        ico_path = self.base_dir / "assets" / "Touch_ID_Logo.ico"
        self._window_icon_applied = False
        try:
            if ico_path.is_file():
                self.root.iconbitmap(str(ico_path))
                self._window_icon_applied = True
        except Exception:
            pass

    def _apply_native_windows_icon(self):
        """Set both taskbar icon sizes on Tk's native top-level HWND."""
        if sys.platform != "win32":
            return
        ico_path = self.base_dir / "assets" / "Touch_ID_Logo.ico"
        if not ico_path.is_file():
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            self.root.update_idletasks()
            hwnd = wintypes.HWND(self.root.winfo_id())
            parent = user32.GetParent(hwnd)
            if parent:
                hwnd = wintypes.HWND(parent)

            load_image = user32.LoadImageW
            load_image.argtypes = (
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            )
            load_image.restype = wintypes.HANDLE

            image_icon = 1
            load_from_file = 0x0010
            sm_cxicon, sm_cyicon = 11, 12
            sm_cxsmicon, sm_cysmicon = 49, 50
            big = load_image(
                None,
                str(ico_path),
                image_icon,
                user32.GetSystemMetrics(sm_cxicon),
                user32.GetSystemMetrics(sm_cyicon),
                load_from_file,
            )
            small = load_image(
                None,
                str(ico_path),
                image_icon,
                user32.GetSystemMetrics(sm_cxsmicon),
                user32.GetSystemMetrics(sm_cysmicon),
                load_from_file,
            )
            if not big or not small:
                return

            wm_seticon = 0x0080
            icon_small, icon_big = 0, 1
            send_message = user32.SendMessageW
            send_message.argtypes = (
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            send_message.restype = wintypes.LPARAM
            send_message(hwnd, wm_seticon, icon_big, big)
            send_message(hwnd, wm_seticon, icon_small, small)

            set_class_icon = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
            set_class_icon.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
            set_class_icon.restype = ctypes.c_void_p
            set_class_icon(hwnd, -14, big)   # GCLP_HICON
            set_class_icon(hwnd, -34, small)  # GCLP_HICONSM
            self._native_icon_handles = (big, small)
        except Exception:
            pass

    def _configure_style(self):
        self.root.configure(bg=UI_BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", (UI_FONT, 10))
        self.root.option_add("*TCombobox*Listbox.font", (UI_FONT, 10))
        style.configure(".", font=(UI_FONT, 10), background=UI_BG, foreground=UI_TEXT)
        style.configure("TFrame", background=UI_BG)
        style.configure("Surface.TFrame", background=UI_SURFACE)
        style.configure("TLabel", background=UI_BG, foreground=UI_TEXT)
        style.configure("Muted.TLabel", background=UI_BG, foreground=UI_SUBTLE)
        style.configure("TNotebook", background=UI_BG, borderwidth=0, tabmargins=(6, 4, 6, 0))
        style.configure(
            "TNotebook.Tab",
            background=UI_BG,
            foreground=UI_SUBTLE,
            padding=(18, 9),
            font=(UI_FONT, 10),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", UI_SURFACE), ("active", "#EFEFF4")],
            foreground=[("selected", UI_TEXT), ("active", UI_TEXT)],
        )
        style.configure(
            "TButton",
            background="#E9E9EB",
            foreground=UI_TEXT,
            borderwidth=0,
            focusthickness=0,
            padding=(14, 7),
        )
        style.map(
            "TButton",
            background=[("active", "#DCDCE0"), ("pressed", "#D1D1D6"), ("disabled", "#F2F2F7")],
            foreground=[("disabled", UI_SUBTLE)],
        )
        style.configure(
            "Accent.TButton",
            background=UI_ACCENT,
            foreground="#FFFFFF",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 7),
        )
        style.map("Accent.TButton", background=[("active", "#0066CC"), ("pressed", "#0055AA")])
        style.configure("TEntry", fieldbackground=UI_SURFACE, bordercolor=UI_BORDER, lightcolor=UI_BORDER, padding=6)
        style.configure("TCombobox", fieldbackground=UI_SURFACE, bordercolor=UI_BORDER, padding=5)
        style.configure("TCheckbutton", background=UI_BG, foreground=UI_TEXT, padding=(4, 2))
        style.configure("TRadiobutton", background=UI_BG, foreground=UI_TEXT, padding=(4, 2))
        style.configure(
            "TLabelframe",
            background=UI_BG,
            bordercolor=UI_BORDER,
            relief=tk.SOLID,
            padding=10,
        )
        style.configure("TLabelframe.Label", background=UI_BG, foreground=UI_TEXT, font=(UI_FONT, 10, "bold"))
        style.configure(
            "Treeview",
            background=UI_SURFACE,
            fieldbackground=UI_SURFACE,
            foreground=UI_TEXT,
            bordercolor=UI_BORDER,
            rowheight=28,
            font=(UI_FONT, 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#F2F2F7",
            foreground=UI_TEXT,
            relief=tk.FLAT,
            font=(UI_FONT, 9, "bold"),
        )
        style.map("Treeview", background=[("selected", "#D6E9FF")], foreground=[("selected", UI_TEXT)])
        style.configure("Horizontal.TProgressbar", troughcolor="#E5E5EA", background=UI_ACCENT, bordercolor=UI_BG)
        png_path = self.base_dir / "assets" / "Touch_ID_Logo.png"
        try:
            if not self._window_icon_applied and png_path.is_file():
                self._window_icon_image = tk.PhotoImage(file=str(png_path))
                self.root.iconphoto(True, self._window_icon_image)
        except Exception:
            pass

    # ── UI build ─────────────────────────────────────────────

    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Flow: login first (required for decrypt) → query/download/cache → status → settings
        self._build_login_tab()
        self._build_quality_tab()
        self._build_cache_tab()
        self._build_status_tab()
        self._build_settings_tab()

        self.status_bar = tk.Label(
            outer,
            text="正在启动…",
            anchor=tk.W,
            fg=COLOR["starting"],
            bg=UI_SURFACE,
            padx=12,
            pady=7,
            font=(UI_FONT, 9),
        )
        self.status_bar.pack(fill=tk.X, pady=(10, 0))

    def _set_status_bar(self, text: str, kind: str = "info"):
        self.status_bar.config(text=text, fg=COLOR.get(kind, COLOR["info"]))

    def _set_label_color(self, label: ttk.Label | tk.Label, kind: str):
        try:
            label.config(foreground=COLOR.get(kind, COLOR["info"]))
        except tk.TclError:
            try:
                label.config(fg=COLOR.get(kind, COLOR["info"]))
            except tk.TclError:
                pass

    def _mask(self, account: Optional[str]) -> str:
        return mask_account(account or "")

    def _display_account(self, account: Optional[str] = None) -> str:
        plain = (account if account is not None else self._account_plain) or ""
        if not plain:
            return ""
        return self._mask(plain) if self._account_locked or self._account_plain else plain

    def _set_username_entry(self, plain: str, *, locked: bool):
        """Show account in entry; locked=True → masked + non-editable."""
        self.entry_username.config(state=tk.NORMAL)
        self.entry_username.delete(0, tk.END)
        if locked and plain:
            self.entry_username.insert(0, self._mask(plain))
            self.entry_username.config(state="readonly")
            self._account_locked = True
        else:
            if plain:
                self.entry_username.insert(0, plain)
            self.entry_username.config(state=tk.NORMAL)
            self._account_locked = False

    def _lock_logged_in_account(self, plain: str):
        plain = (plain or "").strip()
        if not plain:
            return
        self._account_plain = plain
        self._set_username_entry(plain, locked=True)
        # Clear password after success
        try:
            self.entry_password.delete(0, tk.END)
            self.entry_password.config(state=tk.DISABLED)
        except tk.TclError:
            pass
        masked = self._mask(plain)
        self.lbl_login_status.config(text=f"已登录: {masked}")
        self._set_label_color(self.lbl_login_status, "logged_in")
        self.lbl_account.config(text=f"账号: {masked}")
        self._set_label_color(self.lbl_account, "logged_in")
        self.btn_logout.config(state=tk.NORMAL)
        self.btn_login.config(state=tk.DISABLED)

    def _unlock_account_fields(self):
        self._account_plain = None
        self._account_locked = False
        try:
            self.entry_username.config(state=tk.NORMAL)
            self.entry_username.delete(0, tk.END)
            self.entry_password.config(state=tk.NORMAL)
            self.entry_password.delete(0, tk.END)
        except tk.TclError:
            pass
        self.btn_login.config(state=tk.NORMAL)
        self.btn_logout.config(state=tk.DISABLED)

    def _account_for_api(self) -> str:
        """Real account for backend login/logout (never masked)."""
        if self._account_plain:
            return self._account_plain.strip()
        return self.entry_username.get().strip()

    def _build_status_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="状态")

        pane = ttk.PanedWindow(frame, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(pane, padding=8)
        pane.add(left, weight=2)

        ttk.Label(left, text="运行状态", font=("", 10, "bold")).pack(anchor=tk.W, pady=(0, 6))

        self.lbl_ready = ttk.Label(left, text="内核: 启动中", foreground=COLOR["starting"])
        self.lbl_ready.pack(anchor=tk.W, pady=3)
        self.lbl_account = ttk.Label(left, text="账号: —", foreground=COLOR["muted"])
        self.lbl_account.pack(anchor=tk.W, pady=3)
        self.lbl_regions = ttk.Label(left, text="区域: —", foreground=COLOR["muted"])
        self.lbl_regions.pack(anchor=tk.W, pady=3)
        self.lbl_api = ttk.Label(left, text="Apple API: 检测中", foreground=COLOR["muted"])
        self.lbl_api.pack(anchor=tk.W, pady=3)
        self.lbl_hw_accel = ttk.Label(left, text="硬件加速: 检测中", foreground=COLOR["muted"])
        self.lbl_hw_accel.pack(anchor=tk.W, pady=3)
        self.lbl_mode = ttk.Label(left, text="模式: 本地内核 · 127.0.0.1:32767")
        self.lbl_mode.pack(anchor=tk.W, pady=3)
        self.lbl_tasks = ttk.Label(left, text="任务: 0")
        self.lbl_tasks.pack(anchor=tk.W, pady=3)
        self.lbl_speed = ttk.Label(left, text="速度: 下载 — · 解密 —")
        self.lbl_speed.pack(anchor=tk.W, pady=3)
        self.lbl_download_root = ttk.Label(left, text="目录: —", wraplength=400)
        self.lbl_download_root.pack(anchor=tk.W, pady=3)
        self.lbl_startup = ttk.Label(left, text="进度: …", foreground=COLOR["starting"])
        self.lbl_startup.pack(anchor=tk.W, pady=3)

        ttk.Separator(left, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Label(
            left,
            text=(
                "流程\n"
                "1. 管理服务在线  →  2. 登录 Apple ID  →  3. 解密就绪  →  4. 查询/下载\n\n"
                "· 登录前不必等待 ready=true\n"
                "· API 与内核独立；API 失败时可配代理后重试\n"
                "· 单次下载任务结束后确认即退出"
            ),
            wraplength=400,
            foreground=COLOR["muted"],
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Button(left, text="刷新", command=self._refresh_status).pack(anchor=tk.W, pady=10)

        right = ttk.LabelFrame(pane, text="吞吐", padding=6)
        pane.add(right, weight=3)
        self.chart_download = SpeedChart(right, title="下载", unit="kB/s", height=110)
        self.chart_download.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        self.chart_decrypt = SpeedChart(right, title="解密", unit="kB/s", height=110)
        self.chart_decrypt.pack(fill=tk.BOTH, expand=True)

    def _build_login_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="登录")

        form = ttk.Frame(frame)
        form.pack(fill=tk.X)

        ttk.Label(form, text="Apple ID", font=("", 10, "bold")).pack(anchor=tk.W)
        ttk.Label(form, text="邮箱", foreground=COLOR["muted"]).pack(anchor=tk.W, pady=(6, 0))
        self.entry_username = ttk.Entry(form, width=50)
        self.entry_username.pack(anchor=tk.W, fill=tk.X, pady=(2, 6))

        ttk.Label(form, text="密码", foreground=COLOR["muted"]).pack(anchor=tk.W)
        self.entry_password = ttk.Entry(form, width=50, show="*")
        self.entry_password.pack(anchor=tk.W, fill=tk.X, pady=(2, 6))

        btn_row = ttk.Frame(form)
        btn_row.pack(fill=tk.X, pady=6)
        self.btn_logout = ttk.Button(
            btn_row, text="登出", command=self._do_logout, state=tk.DISABLED,
        )
        self.btn_logout.pack(side=tk.RIGHT)
        self.btn_login = ttk.Button(btn_row, text="登录", command=self._do_login, style="Accent.TButton")
        self.btn_login.pack(side=tk.RIGHT, padx=(0, 8))

        self.lbl_login_status = ttk.Label(
            form, text="启动中…", foreground=COLOR["starting"],
        )
        self.lbl_login_status.pack(anchor=tk.W, pady=(4, 4))

        ttk.Label(
            form,
            text="管理服务在线后可登录；成功后账号将脱敏显示并锁定，登出后可再改。",
            foreground=COLOR["muted"],
            wraplength=720,
        ).pack(anchor=tk.W, pady=(0, 8))

        log_frame = ttk.LabelFrame(frame, text="日志", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(log_toolbar, text="清空", command=self._clear_log).pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=12, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 9),
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_quality_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="下载")

        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="链接").pack(side=tk.LEFT)
        self.entry_quality_url = ttk.Entry(row)
        self.entry_quality_url.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.btn_quality = ttk.Button(row, text="查询", command=self._do_quality, style="Accent.TButton")
        self.btn_quality.pack(side=tk.LEFT)

        prog_row = ttk.Frame(frame)
        prog_row.pack(fill=tk.X, pady=(6, 0))
        self.lbl_quality_progress = ttk.Label(prog_row, text="")
        self.lbl_quality_progress.pack(side=tk.LEFT)
        self.quality_progress = ttk.Progressbar(prog_row, mode="determinate", length=240)
        self.quality_progress.pack(side=tk.LEFT, padx=(12, 8))
        self.lbl_quality_eta = ttk.Label(
            prog_row, text="", foreground=COLOR["working"], font=("", 9, "bold"),
        )
        self.lbl_quality_eta.pack(side=tk.LEFT, padx=(4, 0))

        sel_row = ttk.Frame(frame)
        sel_row.pack(fill=tk.X, pady=8)
        ttk.Label(sel_row, text="模式").pack(side=tk.LEFT)
        self.var_quality_best = tk.BooleanVar(value=False)
        ttk.Radiobutton(
            sel_row, text="统一编码", variable=self.var_quality_best, value=False,
            command=self._on_quality_download_mode_changed,
        ).pack(side=tk.LEFT, padx=(6, 8))
        ttk.Radiobutton(
            sel_row, text="每首最高", variable=self.var_quality_best, value=True,
            command=self._on_quality_download_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(sel_row, text="编码").pack(side=tk.LEFT)
        self.combo_quality_codec = ttk.Combobox(sel_row, values=CODECS, state="readonly", width=14)
        self.combo_quality_codec.set("alac")
        self.combo_quality_codec.pack(side=tk.LEFT, padx=6)
        self.btn_quality_download = ttk.Button(
            sel_row, text="下载选中", command=self._download_from_quality, style="Accent.TButton",
        )
        self.btn_quality_download.pack(side=tk.LEFT, padx=(8, 6))
        ttk.Button(sel_row, text="全选", command=self._select_all_songs).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(sel_row, text="反选", command=self._invert_song_selection).pack(side=tk.LEFT)

        dl_opts = ttk.Frame(frame)
        dl_opts.pack(fill=tk.X, pady=2)
        self.var_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(dl_opts, text="覆盖已有文件", variable=self.var_force).pack(side=tk.LEFT, padx=(0, 12))
        self.var_include = tk.BooleanVar(value=False)
        ttk.Checkbutton(dl_opts, text="艺人页含参与作品", variable=self.var_include).pack(side=tk.LEFT)
        ttk.Button(dl_opts, text="打开目录", command=self._open_download_folder).pack(
            side=tk.RIGHT,
        )

        ttk.Label(
            frame,
            text="先登录 → 查询 → 勾选「选」列 → 下载。完成后确认即退出。语言随链接地区。",
            foreground=COLOR["muted"],
            wraplength=900,
        ).pack(anchor=tk.W, pady=(6, 2))

        self.lbl_download_hint = ttk.Label(frame, text="保存: …", wraplength=900)
        self.lbl_download_hint.pack(anchor=tk.W, pady=(0, 4))

        prog_frame = ttk.Frame(frame)
        prog_frame.pack(fill=tk.X, pady=(0, 4))
        dl_txt_row = ttk.Frame(prog_frame)
        dl_txt_row.pack(fill=tk.X)
        self.lbl_download_progress = ttk.Label(dl_txt_row, text="")
        self.lbl_download_progress.pack(side=tk.LEFT)
        self.lbl_download_eta = ttk.Label(
            dl_txt_row, text="", foreground=COLOR["working"], font=("", 9, "bold"),
        )
        self.lbl_download_eta.pack(side=tk.RIGHT)
        self.download_progress = ttk.Progressbar(prog_frame, mode="determinate", length=480)
        self.download_progress.pack(anchor=tk.W, pady=(4, 0), fill=tk.X)

        # List search (case-insensitive title / artist / id / quality)
        search_row = ttk.Frame(frame)
        search_row.pack(fill=tk.X, pady=(4, 2))
        ttk.Label(search_row, text="定位").pack(side=tk.LEFT)
        self.var_list_search = tk.StringVar()
        self.entry_list_search = ttk.Entry(search_row, textvariable=self.var_list_search, width=28)
        self.entry_list_search.pack(side=tk.LEFT, padx=6)
        self.entry_list_search.bind("<Return>", lambda e: self._search_list_next())
        self.entry_list_search.bind("<Shift-Return>", lambda e: self._search_list_prev())
        self.entry_list_search.bind("<KeyRelease>", lambda e: self._search_list_live())
        self.entry_list_search.bind("<F3>", lambda e: self._search_list_next())
        ttk.Button(search_row, text="上一个", width=6, command=self._search_list_prev).pack(
            side=tk.LEFT, padx=2,
        )
        ttk.Button(search_row, text="下一个", width=6, command=self._search_list_next).pack(
            side=tk.LEFT, padx=2,
        )
        self.lbl_search_hit = ttk.Label(search_row, text="", foreground=COLOR["muted"])
        self.lbl_search_hit.pack(side=tk.LEFT, padx=8)
        # Global shortcuts while on download tab
        self.root.bind_all("<Control-f>", self._focus_list_search)
        self.root.bind_all("<Control-F>", self._focus_list_search)

        tree_frame = ttk.LabelFrame(frame, text="曲目", padding=4)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=4)
        self.quality_tree = ttk.Treeview(
            tree_frame,
            columns=SONG_COLUMNS,
            show="headings",
            height=14,
            selectmode="extended",
        )
        headers = {
            "sel": "选", "title": "曲名", "artist": "艺人",
            "quality": "音质", "status": "状态",
        }
        widths = {"sel": 36, "title": 220, "artist": 140, "quality": 360, "status": 100}
        for col in SONG_COLUMNS:
            self.quality_tree.heading(col, text=headers[col])
            self.quality_tree.column(col, width=widths[col], anchor=tk.W)

        self.quality_tree.tag_configure("st_done", foreground=COLOR["done"])
        self.quality_tree.tag_configure("st_fail", foreground=COLOR["fail"])
        self.quality_tree.tag_configure("st_work", foreground=COLOR["working"])
        self.quality_tree.tag_configure("st_wait", foreground=COLOR["starting"])
        self.quality_tree.tag_configure("st_ok", foreground=COLOR["info"])
        self.quality_tree.tag_configure("st_need", foreground=COLOR["need_login"])
        self.quality_tree.tag_configure("search_hit", background="#FFF3E0")
        self.quality_tree.tag_configure("search_current", background="#FFE082")

        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.quality_tree.yview)
        self.quality_tree.configure(yscrollcommand=scroll.set)
        self.quality_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.quality_tree.bind("<Button-1>", self._on_song_tree_click)
        self.quality_tree.bind("<<TreeviewSelect>>", lambda e: None)

        # Preview player bar
        player = ttk.LabelFrame(frame, text="试听（查询结果 · 最高可用音质）", padding=6)
        player.pack(fill=tk.X, pady=(6, 0))
        p_top = ttk.Frame(player)
        p_top.pack(fill=tk.X)
        self.lbl_preview_title = ttk.Label(p_top, text="未选择曲目", foreground=COLOR["muted"])
        self.lbl_preview_title.pack(side=tk.LEFT)
        self.lbl_preview_state = ttk.Label(p_top, text="", foreground=COLOR["muted"])
        self.lbl_preview_state.pack(side=tk.RIGHT)

        p_ctrl = ttk.Frame(player)
        p_ctrl.pack(fill=tk.X, pady=(6, 2))
        self.btn_preview_play = ttk.Button(p_ctrl, text="▶ 播放", width=8, command=self._preview_play)
        self.btn_preview_play.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_preview_pause = ttk.Button(p_ctrl, text="❚❚ 暂停", width=8, command=self._preview_pause)
        self.btn_preview_pause.pack(side=tk.LEFT, padx=4)
        self.btn_preview_stop = ttk.Button(p_ctrl, text="■ 停止", width=8, command=self._preview_stop)
        self.btn_preview_stop.pack(side=tk.LEFT, padx=4)
        self.lbl_preview_time = ttk.Label(p_ctrl, text="00:00 / 00:00", foreground=COLOR["muted"])
        self.lbl_preview_time.pack(side=tk.RIGHT)

        self.var_preview_pos = tk.DoubleVar(value=0.0)
        self.scale_preview = ttk.Scale(
            player, from_=0, to=1000, orient=tk.HORIZONTAL,
            variable=self.var_preview_pos, command=self._on_preview_seek_drag,
        )
        self.scale_preview.pack(fill=tk.X, pady=(2, 0))
        self.scale_preview.bind("<ButtonPress-1>", lambda e: setattr(self, "_seek_dragging", True))
        self.scale_preview.bind("<ButtonRelease-1>", self._on_preview_seek_release)

    def _build_cache_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="本地缓存")

        path_row = ttk.Frame(frame)
        path_row.pack(fill=tk.X)
        ttk.Label(path_row, text="缓存目录").pack(side=tk.LEFT)
        self.var_cache_root = tk.StringVar(value=str((self.base_dir / "Apple Music").resolve()))
        self.entry_cache_root = ttk.Entry(path_row, textvariable=self.var_cache_root)
        self.entry_cache_root.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        ttk.Button(path_row, text="选择目录", command=self._pick_cache_root).pack(side=tk.LEFT, padx=(0, 6))
        self.btn_cache_scan = ttk.Button(path_row, text="开始扫描", command=self._scan_cache, style="Accent.TButton")
        self.btn_cache_scan.pack(side=tk.LEFT)

        opts = ttk.Frame(frame)
        opts.pack(fill=tk.X, pady=(8, 4))
        ttk.Label(opts, text="地区").pack(side=tk.LEFT)
        self.var_cache_storefront = tk.StringVar(value="")
        self.entry_cache_storefront = ttk.Entry(opts, width=8, textvariable=self.var_cache_storefront)
        self.entry_cache_storefront.pack(side=tk.LEFT, padx=(6, 12))
        self.var_cache_force = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="覆盖已有文件", variable=self.var_cache_force).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(opts, text="过滤").pack(side=tk.LEFT)
        self.var_cache_filter = tk.StringVar(value="全部")
        self.combo_cache_filter = ttk.Combobox(
            opts,
            values=["全部", "可导入", "需确认", "不可导入", "资源不完整", "当前版本暂不支持"],
            state="readonly",
            width=16,
            textvariable=self.var_cache_filter,
        )
        self.combo_cache_filter.pack(side=tk.LEFT, padx=(6, 12))
        self.combo_cache_filter.bind("<<ComboboxSelected>>", lambda _e: self._refresh_cache_rows())
        self.btn_cache_import = ttk.Button(opts, text="导入选中", command=self._import_selected_cache, style="Accent.TButton")
        self.btn_cache_import.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(opts, text="全选可导入", command=self._select_importable_cache).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(opts, text="反选", command=self._invert_cache_selection).pack(side=tk.LEFT)

        ttk.Label(
            frame,
            text="扫描只读取 Apple Music Windows 的 movpkg 缓存；只有能稳定识别曲目 ID、标题、艺人、专辑且分片完整的条目才会进入解密/保存链路。",
            foreground=COLOR["muted"],
            wraplength=900,
        ).pack(anchor=tk.W, pady=(2, 4))

        prog = ttk.Frame(frame)
        prog.pack(fill=tk.X, pady=(0, 4))
        self.lbl_cache_progress = ttk.Label(prog, text="")
        self.lbl_cache_progress.pack(side=tk.LEFT)
        self.cache_progress = ttk.Progressbar(prog, mode="determinate", length=360)
        self.cache_progress.pack(side=tk.LEFT, padx=(12, 8))
        self.lbl_cache_summary = ttk.Label(prog, text="", foreground=COLOR["muted"])
        self.lbl_cache_summary.pack(side=tk.LEFT, padx=(4, 0))

        tree_frame = ttk.LabelFrame(frame, text="缓存条目", padding=4)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=4)
        self.cache_tree = ttk.Treeview(
            tree_frame,
            columns=CACHE_COLUMNS,
            show="headings",
            height=16,
            selectmode="extended",
        )
        headers = {
            "sel": "选", "title": "标题", "artist": "艺人", "album": "专辑",
            "codec": "缓存类型", "integrity": "完整性", "status": "可处理性", "note": "备注",
        }
        widths = {
            "sel": 36, "title": 190, "artist": 150, "album": 190,
            "codec": 90, "integrity": 90, "status": 110, "note": 260,
        }
        for col in CACHE_COLUMNS:
            self.cache_tree.heading(col, text=headers[col])
            self.cache_tree.column(col, width=widths[col], anchor=tk.W)
        for tag, color in (
            ("st_done", COLOR["done"]),
            ("st_fail", COLOR["fail"]),
            ("st_work", COLOR["working"]),
            ("st_wait", COLOR["starting"]),
            ("st_ok", COLOR["info"]),
            ("st_need", COLOR["need_login"]),
        ):
            self.cache_tree.tag_configure(tag, foreground=color)
        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.cache_tree.yview)
        self.cache_tree.configure(yscrollcommand=scroll.set)
        self.cache_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.cache_tree.bind("<Button-1>", self._on_cache_tree_click)

    def _build_settings_tab(self):
        frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(frame, text="设置")

        canvas = tk.Canvas(frame, highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.setting_vars: dict[str, tk.Variable] = {}

        def add_entry(parent, label, key, default=""):
            ttk.Label(parent, text=label).pack(anchor=tk.W, pady=(8, 0))
            var = tk.StringVar(value=default)
            self.setting_vars[key] = var
            ttk.Entry(parent, textvariable=var, width=70).pack(anchor=tk.W)

        def add_int(parent, label, key, default=1):
            ttk.Label(parent, text=label).pack(anchor=tk.W, pady=(8, 0))
            var = tk.StringVar(value=str(default))
            self.setting_vars[key] = var
            ttk.Entry(parent, textvariable=var, width=20).pack(anchor=tk.W)

        def add_bool(parent, label, key, default=False):
            var = tk.BooleanVar(value=default)
            self.setting_vars[key] = var
            ttk.Checkbutton(parent, text=label, variable=var).pack(anchor=tk.W, pady=4)

        ttk.Label(inner, text="存储", font=("", 10, "bold")).pack(anchor=tk.W)
        add_entry(inner, "下载根目录", "dirPathFormat")
        add_entry(inner, "歌单目录格式", "playlistDirPathFormat")
        add_entry(inner, "文件名格式", "songNameFormat")
        add_entry(inner, "代理 (例 http://127.0.0.1:7890，访问 Apple 失败时填写)", "proxy")
        ttk.Label(
            inner,
            text="代理同时影响 Apple API 与 VM 内组件下载。",
            foreground=COLOR["muted"],
            wraplength=560,
        ).pack(anchor=tk.W, pady=(2, 0))
        add_entry(inner, "Apple CDN IP（可选）", "appleCDNIP")
        add_int(inner, "并发下载", "parallelNum")
        add_int(inner, "最大任务数", "maxRunningTasks")

        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        ttk.Label(inner, text="音质与元数据", font=("", 10, "bold")).pack(anchor=tk.W)
        add_bool(inner, "编码缺失时自动回退", "codecAlternative")
        add_bool(inner, "保存歌词", "saveLyrics")
        add_bool(inner, "保存封面", "saveCover")
        add_bool(inner, "校验失败则丢弃", "failedSongNotPassIntegrityCheck")
        add_entry(inner, "元数据语言（空=随链接地区）", "language")

        ttk.Separator(inner, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)
        ttk.Label(inner, text="内核", font=("", 10, "bold")).pack(anchor=tk.W)
        add_int(inner, "内存 (MB)", "memoryMB", 512)
        ttk.Label(inner, text="硬件加速（自动）", font=("", 9, "bold")).pack(
            anchor=tk.W, pady=(10, 2),
        )
        self.lbl_hw_accel_settings = ttk.Label(
            inner,
            text="检测中…",
            foreground=COLOR["muted"],
            wraplength=560,
            justify=tk.LEFT,
        )
        self.lbl_hw_accel_settings.pack(anchor=tk.W, pady=(0, 4))

        btn_row = ttk.Frame(inner)
        btn_row.pack(anchor=tk.W, pady=16)
        ttk.Button(btn_row, text="加载", command=self._load_settings_form).pack(
            side=tk.LEFT, padx=(0, 8),
        )
        ttk.Button(btn_row, text="保存", command=self._save_settings).pack(
            side=tk.LEFT, padx=(0, 8),
        )
        ttk.Button(btn_row, text="选择目录…", command=self._pick_download_root).pack(
            side=tk.LEFT, padx=(0, 8),
        )
        ttk.Button(btn_row, text="重试 Apple API", command=self._retry_apple_api).pack(
            side=tk.LEFT,
        )

    # ── lifecycle / exit ─────────────────────────────────────

    def _start_backend(self):
        self.backend.on_progress(lambda msg: self._ui_queue.put(("startup_progress", msg)))

        def _worker():
            try:
                self.backend.start()
                self._ui_queue.put(("precheck_ready", None))
                self.backend.start_kernel()
                self.backend.on_status_tick(lambda: self._ui_queue.put(("refresh_status", None)))
                self.backend.on_song_status(
                    lambda sid, label, _err: self._ui_queue.put(("song_status", (sid, label))),
                )
                self._ui_queue.put(("backend_ready", None))
            except Exception as e:
                self._ui_queue.put(("backend_error", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _exit_quietly(self):
        if self._exiting:
            return
        self._exiting = True
        set_log_sink(None)
        try:
            self._player.close()
        except Exception:
            pass
        if self._principle_splash:
            try:
                self._principle_splash.destroy()
            except Exception:
                pass
        # Always tear down backend + force-kill QEMU residual
        try:
            self.backend.shutdown(poweroff_kernel=True)
        except Exception:
            try:
                from src.qemu import QemuInstance
                QemuInstance.force_kill_qemu_sync()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _finish_task_and_reset(self, title: str, message: str, *, is_error: bool = False):
        """Show result dialog; OK returns the download tab to an idle state."""
        self._task_finished = True
        if is_error:
            self._set_status_bar("任务失败，可调整后重试", "fail")
            messagebox.showerror(title, message)
        else:
            self._set_status_bar("任务完成，可继续下一次下载", "done")
            messagebox.showinfo(title, message)
        self._task_finished = False
        self._download_batch_total = 0
        self._download_batch_ids.clear()
        self._download_batch_finished.clear()
        self._eta_download.reset(total=1)
        self._last_batch_units = {"done": 0.0, "total": 1.0}
        self.download_progress["maximum"] = 100
        self.download_progress["value"] = 0
        self.lbl_download_progress.config(text="就绪，可继续查询或下载")
        self.lbl_download_eta.config(text="")
        self.btn_quality_download.config(state=tk.NORMAL)
        self.btn_quality.config(state=tk.NORMAL)

    def _select_login_tab(self):
        try:
            for i in range(self.notebook.index("end")):
                if self.notebook.tab(i, "text") in ("登录", "Apple ID 登录"):
                    self.notebook.select(i)
                    break
        except Exception:
            pass

    def _select_settings_tab(self):
        try:
            for i in range(self.notebook.index("end")):
                if self.notebook.tab(i, "text") == "设置":
                    self.notebook.select(i)
                    break
        except Exception:
            pass

    def _retry_apple_api(self):
        """Save is optional; re-init API with current form proxy if present."""
        proxy = ""
        if "proxy" in self.setting_vars:
            proxy = self.setting_vars["proxy"].get().strip()

        def _worker():
            try:
                if proxy or "proxy" in self.setting_vars:
                    # Apply proxy from form without requiring full save
                    updates = {
                        "dirPathFormat": self.setting_vars["dirPathFormat"].get(),
                        "playlistDirPathFormat": self.setting_vars["playlistDirPathFormat"].get(),
                        "songNameFormat": self.setting_vars["songNameFormat"].get(),
                        "proxy": proxy,
                        "appleCDNIP": self.setting_vars["appleCDNIP"].get(),
                        "parallelNum": self.setting_vars["parallelNum"].get(),
                        "maxRunningTasks": self.setting_vars["maxRunningTasks"].get(),
                        "codecAlternative": self.setting_vars["codecAlternative"].get(),
                        "saveLyrics": self.setting_vars["saveLyrics"].get(),
                        "saveCover": self.setting_vars["saveCover"].get(),
                        "failedSongNotPassIntegrityCheck": self.setting_vars[
                            "failedSongNotPassIntegrityCheck"
                        ].get(),
                        "language": self.setting_vars["language"].get(),
                        "memoryMB": self.setting_vars["memoryMB"].get(),
                    }
                    self.backend.run_coro(self.backend.apply_config(updates)).result(timeout=30)
                msg = self.backend.run_coro(self.backend.reinit_web_api()).result(timeout=120)
                self._ui_queue.put(("api_retry_ok", msg))
            except Exception as e:
                self._ui_queue.put(("api_retry_fail", str(e)))

        self._set_status_bar("正在重试连接 Apple Music API…", "working")
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_lifecycle(self, lifecycle: str, account_ready: bool, service_ready: bool):
        self._lifecycle = lifecycle
        if lifecycle == "starting":
            self._set_label_color(self.lbl_ready, "starting")
            self.lbl_ready.config(text="内核: 启动中")
            self._set_label_color(self.lbl_startup, "starting")
        elif lifecycle == "kernel_degraded":
            self._set_label_color(self.lbl_ready, "fail")
            self.lbl_ready.config(text="内核: 连接异常")
        elif lifecycle == "ready_need_login":
            self._set_label_color(self.lbl_ready, "ready")
            self.lbl_ready.config(text="内核: 在线 · 待登录")
            self._set_label_color(self.lbl_startup, "need_login")
            self.lbl_startup.config(text="进度: 请登录以启用解密")
            self._set_label_color(self.lbl_account, "need_login")
            self._set_label_color(self.lbl_regions, "need_login")
        elif lifecycle == "logged_in_warming":
            self._set_label_color(self.lbl_ready, "working")
            self.lbl_ready.config(text="内核: 已登录 · 解密实例准备中")
            self._set_label_color(self.lbl_startup, "working")
            self.lbl_startup.config(text="进度: 等待区域/实例")
            self._set_label_color(self.lbl_account, "logged_in")
        elif lifecycle == "ready_logged_in":
            self._set_label_color(self.lbl_ready, "ready")
            self.lbl_ready.config(text="内核: 解密就绪")
            self._set_label_color(self.lbl_startup, "ready")
            self.lbl_startup.config(text="进度: 可查询/下载")
            self._set_label_color(self.lbl_account, "logged_in")
            self._set_label_color(self.lbl_regions, "ready")
        else:
            if account_ready:
                self._set_label_color(self.lbl_account, "logged_in")
            else:
                self._set_label_color(self.lbl_account, "need_login")

    # ── UI queue ─────────────────────────────────────────────

    def _process_ui_queue(self):
        if self._exiting:
            return
        while True:
            try:
                kind, data = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "startup_progress":
                self._boot_phase = "starting"
                self.lbl_startup.config(text=f"进度: {data}")
                self._set_label_color(self.lbl_startup, "starting")
                self._set_status_bar(data, "starting")
                # Do not flash「未登录」while booting
                cur = self.lbl_login_status.cget("text") or ""
                if cur in ("", "未登录") or cur.startswith("启动"):
                    self.lbl_login_status.config(text="启动中…")
                    self._set_label_color(self.lbl_login_status, "starting")
                if self._principle_splash:
                    self._principle_splash.set_status(data)
            elif kind == "precheck_ready":
                self._boot_phase = "starting"
                self.lbl_startup.config(text="进度: 预检完成，启动内核…")
                self._set_label_color(self.lbl_startup, "starting")
                self._set_status_bar("预检完成，启动内核…", "starting")
                self.lbl_login_status.config(text="启动中…")
                self._set_label_color(self.lbl_login_status, "starting")
                self._load_settings_form()
            elif kind == "backend_ready":
                self._boot_phase = "online"
                api_ok = bool(getattr(self.backend, "_api_ready", False))
                api_err = getattr(self.backend, "_api_error", None) or ""
                saved = self.backend.resolve_current_account()

                if not api_ok:
                    self.lbl_api.config(text="Apple API: 未连通")
                    self._set_label_color(self.lbl_api, "fail")
                else:
                    self.lbl_api.config(text="Apple API: 正常")
                    self._set_label_color(self.lbl_api, "ready")

                if saved:
                    # Session restore: treat as logged-in UI (masked + locked)
                    self._lock_logged_in_account(saved)
                    self.lbl_startup.config(text="进度: 管理在线")
                    self._set_label_color(self.lbl_startup, "ready")
                    self._set_status_bar(f"在线 · 账号 {self._mask(saved)}", "ready")
                else:
                    self._unlock_account_fields()
                    self.lbl_account.config(text="账号: 未登录")
                    self._set_label_color(self.lbl_account, "need_login")
                    self.lbl_login_status.config(text="可登录")
                    self._set_label_color(self.lbl_login_status, "need_login")
                    self.lbl_startup.config(text="进度: 请登录")
                    self._set_label_color(self.lbl_startup, "need_login")
                    self._set_status_bar("管理服务在线 · 请登录后使用", "need_login")
                    self._select_login_tab()

                if not api_ok:
                    messagebox.showwarning(
                        "Apple API",
                        (api_err or "无法访问 music.apple.com。")
                        + "\n\n内核仍可登录。请到「设置」填写代理后点「重试 Apple API」。",
                    )
                    if not saved:
                        self._select_settings_tab()

                if self._principle_splash:
                    self._principle_splash.destroy()
                    self._principle_splash = None
                self._load_settings_form()
                self._refresh_status()
            elif kind == "backend_error":
                err_text = format_error(data)
                self._set_status_bar(f"启动失败: {err_text.splitlines()[0]}", "fail")
                self._set_label_color(self.lbl_ready, "fail")
                self.lbl_ready.config(text="内核: 启动失败")
                self._set_label_color(self.lbl_startup, "fail")
                self.lbl_startup.config(text=f"进度: 失败")
                messagebox.showerror("启动失败", err_text)
            elif kind == "refresh_status":
                self._refresh_status()
            elif kind == "song_status":
                sid, label = data
                self._song_status[sid] = label
                if sid in self._download_batch_ids and label in ("完成", "失败"):
                    self._download_batch_finished.add(sid)
                self._refresh_song_row(sid)
                for cid in self._cache_adam_to_candidates.get(sid, set()):
                    self._cache_status[cid] = label
                    if label in ("完成", "失败"):
                        self._cache_import_finished.add(cid)
                    self._refresh_cache_row(cid)
            elif kind == "login_ok":
                username = (data or self._account_for_api() or "").strip()
                if username:
                    self._lock_logged_in_account(username)
                    self._set_status_bar(
                        f"登录完成 · 解密就绪 · {self._mask(username)}", "logged_in",
                    )
                else:
                    self.lbl_login_status.config(text="已登录")
                    self._set_label_color(self.lbl_login_status, "logged_in")
                    self._set_status_bar("登录完成 · 解密就绪", "logged_in")
                self._refresh_status()
            elif kind == "login_cancelled":
                self.lbl_login_status.config(text="已取消")
                self._set_label_color(self.lbl_login_status, "muted")
                if not self._account_locked:
                    self.btn_login.config(state=tk.NORMAL)
                self._set_status_bar("登录已取消", "need_login")
            elif kind == "login_fail":
                err_text = format_error(data)
                self.lbl_login_status.config(text=f"失败: {err_text.splitlines()[0]}")
                self._set_label_color(self.lbl_login_status, "fail")
                self._set_status_bar(f"登录失败: {err_text.splitlines()[0]}", "fail")
                # Failed attempt: keep email editable, clear sticky plain if not locked
                if not self._account_locked:
                    self._account_plain = None
                    self.btn_login.config(state=tk.NORMAL)
                    try:
                        self.entry_password.config(state=tk.NORMAL)
                    except tk.TclError:
                        pass
            elif kind == "logout_ok":
                self._unlock_account_fields()
                self.lbl_login_status.config(text="已登出")
                self._set_label_color(self.lbl_login_status, "need_login")
                self.lbl_account.config(text="账号: 未登录")
                self._set_label_color(self.lbl_account, "need_login")
                self._set_status_bar("已登出 · 管理服务仍在线", "need_login")
                self._refresh_status()
            elif kind == "logout_fail":
                messagebox.showerror("登出失败", format_error(data))
            elif kind == "quality_start":
                total = data
                self._quality_results = []
                self._song_selected.clear()
                self._song_status.clear()
                self._search_matches = []
                self._search_idx = -1
                if hasattr(self, "lbl_search_hit"):
                    self.lbl_search_hit.config(text="")
                if hasattr(self, "var_list_search"):
                    # keep user keyword; just clear match state
                    pass
                for item in self.quality_tree.get_children():
                    self.quality_tree.delete(item)
                self.quality_progress["maximum"] = max(total, 1)
                self.quality_progress["value"] = 0
                # Query: ~0.6–1.2s/song typical; floor prevents premature 00:00
                self._eta_quality.reset(total=max(total, 1), min_sec_per_unit=0.65)
                self.lbl_quality_progress.config(text=f"查询 0/{total}")
                self.lbl_quality_eta.config(text="估算中…")
                self._set_label_color(self.lbl_quality_eta, "working")
                self._set_status_bar(f"查询 0/{total}", "working")
            elif kind == "quality_progress":
                done, total, song = data
                # done==0 is start marker (no song row)
                if song is not None and getattr(song, "song_id", None):
                    self._append_quality_row(song)
                    # Keep live results for search/preview before quality_done
                    existing = next(
                        (i for i, s in enumerate(self._quality_results) if s.song_id == song.song_id),
                        None,
                    )
                    if existing is None:
                        self._quality_results.append(song)
                    else:
                        self._quality_results[existing] = song
                self.quality_progress["value"] = done
                self.quality_progress["maximum"] = max(total, 1)
                _, eta_txt, eta_refresh = self._eta_quality.update(float(done), float(total))
                self.lbl_quality_progress.config(text=f"查询 {done}/{total}")
                # Always refresh ETA label (tick may also update; avoid missing first lock)
                if eta_txt:
                    self.lbl_quality_eta.config(text=eta_txt)
                self._set_status_bar(f"查询 {done}/{total} · {eta_txt}", "working")
            elif kind == "quality_done":
                self._quality_results = data
                self.btn_quality.config(state=tk.NORMAL)
                self.quality_progress["value"] = self.quality_progress["maximum"]
                # Force finish ETA only after real completion
                if data:
                    self._eta_quality.update(len(data), len(data))
                summary = self._quality_best_summary(data)
                elapsed = self._eta_quality.elapsed_text()
                self.lbl_quality_progress.config(
                    text=f"完成 {len(data)} 首 · 用时 {elapsed}{summary}",
                )
                self.lbl_quality_eta.config(text="剩余 00:00")
                self._set_status_bar(
                    f"查询完成 {len(data)} 首 · 用时 {elapsed}{summary}", "done",
                )
                # Re-run search if user already typed a keyword
                if hasattr(self, "var_list_search") and (self.var_list_search.get() or "").strip():
                    self._search_list_live()
            elif kind == "quality_fail":
                self.btn_quality.config(state=tk.NORMAL)
                self.lbl_quality_progress.config(text="")
                self.lbl_quality_eta.config(text="")
                self.quality_progress["value"] = 0
                err_text = format_error(data)
                # Not-logged-in is operational, not startup failure
                if "尚未登录" in err_text or "请登录" in err_text:
                    self._set_status_bar("请先登录 Apple ID", "need_login")
                    self._select_login_tab()
                    messagebox.showwarning("需要登录", err_text)
                else:
                    self._set_status_bar(f"查询失败: {err_text.splitlines()[0]}", "fail")
                    messagebox.showerror("音质查询失败", err_text)
            elif kind == "cache_scan_start":
                self._cache_candidates = []
                self._cache_selected.clear()
                self._cache_status.clear()
                self._cache_adam_to_candidates.clear()
                for item in self.cache_tree.get_children():
                    self.cache_tree.delete(item)
                self.cache_progress["maximum"] = 1
                self.cache_progress["value"] = 0
                self.lbl_cache_progress.config(text="扫描中…")
                self.lbl_cache_summary.config(text="")
                self.btn_cache_scan.config(state=tk.DISABLED)
                self.btn_cache_import.config(state=tk.DISABLED)
                self._set_status_bar("正在扫描本地缓存…", "working")
            elif kind == "cache_scan_progress":
                done, total, path = data
                self.cache_progress["maximum"] = max(total, 1)
                self.cache_progress["value"] = done
                name = Path(path).name if path else ""
                self.lbl_cache_progress.config(text=f"扫描 {done}/{total} {name}")
            elif kind == "cache_scan_done":
                self._cache_candidates = data
                self._cache_selected.clear()
                self._cache_status.clear()
                self._cache_adam_to_candidates.clear()
                for candidate in self._cache_candidates:
                    cid = candidate.get("candidate_id", "")
                    adam_id = candidate.get("adam_id", "")
                    self._cache_selected[cid] = candidate.get("import_status") == "可导入"
                    self._cache_status[cid] = candidate.get("import_status") or ""
                    if adam_id:
                        self._cache_adam_to_candidates.setdefault(adam_id, set()).add(cid)
                self._refresh_cache_rows()
                self.cache_progress["maximum"] = max(len(data), 1)
                self.cache_progress["value"] = len(data)
                self.lbl_cache_progress.config(text=f"扫描完成 {len(data)} 个包")
                self.lbl_cache_summary.config(text=self._cache_summary_text())
                self.btn_cache_scan.config(state=tk.NORMAL)
                self.btn_cache_import.config(state=tk.NORMAL)
                self._set_status_bar(f"本地缓存扫描完成 · {self._cache_summary_text()}", "done")
            elif kind == "cache_scan_fail":
                self.btn_cache_scan.config(state=tk.NORMAL)
                self.btn_cache_import.config(state=tk.NORMAL)
                self.cache_progress["value"] = 0
                self.lbl_cache_progress.config(text="扫描失败")
                err_text = format_error(data)
                self._set_status_bar(f"缓存扫描失败: {err_text.splitlines()[0]}", "fail")
                messagebox.showerror("缓存扫描失败", err_text)
            elif kind == "cache_import_started":
                if isinstance(data, dict):
                    total = int(data.get("total") or 0)
                    candidate_ids = data.get("candidate_ids") or []
                    song_ids = data.get("song_ids") or []
                else:
                    total = 0
                    candidate_ids = []
                    song_ids = []
                self._download_batch_total = total
                self._download_batch_ids = set(song_ids)
                self._download_batch_finished.clear()
                self._cache_import_total = total
                self._cache_import_finished.clear()
                for cid in candidate_ids:
                    self._cache_status[cid] = "等待中"
                    self._refresh_cache_row(cid)
                self.cache_progress["maximum"] = max(total, 1)
                self.cache_progress["value"] = 0
                self.lbl_cache_progress.config(text=f"导入中 0/{total}")
                self.btn_cache_scan.config(state=tk.DISABLED)
                self.btn_cache_import.config(state=tk.DISABLED)
                self._eta_download.reset(total=float(max(total, 1) * 2), min_sec_per_unit=5.0)
                self._last_batch_units = {"done": 0.0, "total": float(max(total, 1) * 2)}
                self._set_status_bar(f"正在导入本地缓存 {total} 首…", "working")
            elif kind == "cache_import_done":
                statuses = data.get("statuses", {}) if isinstance(data, dict) else {}
                for cid, raw in statuses.items():
                    self._cache_status[cid] = {
                        "DONE": "完成",
                        "FAILED": "失败",
                        "WAITING": "等待中",
                        "DOWNLOADING": "读取中",
                        "DECRYPTING": "解密中",
                    }.get(raw, raw or self._cache_status.get(cid, ""))
                    self._refresh_cache_row(cid)
                total = max(self._download_batch_total, 1)
                done_count = sum(1 for cid in statuses if self._cache_status.get(cid) == "完成")
                self._cache_import_finished = {
                    cid for cid, status in self._cache_status.items()
                    if status in ("完成", "失败")
                }
                self.cache_progress["maximum"] = total
                self.cache_progress["value"] = total
                self.lbl_cache_progress.config(text=f"导入完成 {done_count}/{total}")
                self.btn_cache_scan.config(state=tk.NORMAL)
                self.btn_cache_import.config(state=tk.NORMAL)
                self._cache_import_total = 0
                self._cache_import_finished.clear()
                self.lbl_cache_summary.config(text=self._cache_summary_text())
                self._set_status_bar("本地缓存导入完成", "done")
                messagebox.showinfo("缓存导入完成", data.get("message", "导入完成"))
            elif kind == "cache_import_fail":
                self.btn_cache_scan.config(state=tk.NORMAL)
                self.btn_cache_import.config(state=tk.NORMAL)
                self._cache_import_total = 0
                self._cache_import_finished.clear()
                err_text = format_error(data)
                self.lbl_cache_progress.config(text="导入失败")
                self._set_status_bar(f"缓存导入失败: {err_text.splitlines()[0]}", "fail")
                if "尚未登录" in err_text or "请登录" in err_text:
                    self._select_login_tab()
                    messagebox.showwarning("需要登录", err_text)
                else:
                    messagebox.showerror("缓存导入失败", err_text)
            elif kind == "download_started":
                if isinstance(data, dict):
                    msg = data.get("message", "正在下载...")
                    self._download_batch_total = int(data.get("total") or 0)
                    self._download_batch_ids = set(data.get("song_ids") or [])
                    self._download_batch_finished.clear()
                else:
                    msg = str(data)
                    self._download_batch_total = 0
                    self._download_batch_ids.clear()
                    self._download_batch_finished.clear()
                self._set_status_bar(msg, "working")
                self.lbl_download_progress.config(text=msg)
                self.download_progress["maximum"] = 100
                self.download_progress["value"] = 0
                total_songs = self._download_batch_total or 1
                # Two-phase: download units + decrypt units; ~8s/unit floor (CDN+VM)
                self._eta_download.reset(
                    total=float(total_songs * 2),
                    min_sec_per_unit=6.0,
                )
                self._last_batch_units = {"done": 0.0, "total": float(total_songs * 2)}
                self.lbl_download_eta.config(text="估算中…")
                self._set_label_color(self.lbl_download_eta, "working")
                self.btn_quality_download.config(state=tk.DISABLED)
                self.btn_quality.config(state=tk.DISABLED)
            elif kind == "download_done":
                if self._download_batch_total > 0:
                    done_ok = sum(
                        1 for sid in self._download_batch_finished
                        if self._song_status.get(sid) == "完成"
                    )
                    fail_n = len(self._download_batch_finished) - done_ok
                    self.download_progress["value"] = 100
                    elapsed = self._eta_download.elapsed_text()
                    self.lbl_download_progress.config(
                        text=(
                            f"结束 成功 {done_ok} / 失败 {fail_n} / 共 {self._download_batch_total} 首"
                            f" · 用时 {elapsed}"
                        ),
                    )
                    self.lbl_download_eta.config(text="剩余 00:00")
                self._finish_task_and_reset("下载完成", str(data), is_error=False)
            elif kind == "download_fail":
                err_text = format_error(data)
                if "尚未登录" in err_text or "请登录" in err_text:
                    self.btn_quality_download.config(state=tk.NORMAL)
                    self.btn_quality.config(state=tk.NORMAL)
                    self._set_status_bar("请先登录 Apple ID", "need_login")
                    self._select_login_tab()
                    messagebox.showwarning("需要登录", err_text)
                else:
                    self._finish_task_and_reset("下载失败", err_text, is_error=True)
            elif kind == "need_2fa":
                self._prompt_2fa()
            elif kind == "settings_loaded":
                for key, val in data.items():
                    if key in self.setting_vars:
                        self.setting_vars[key].set(val)
                if data.get("download_root"):
                    self.lbl_download_hint.config(text=f"下载保存位置: {data['download_root']}")
                    self.lbl_download_root.config(text=f"下载目录: {data['download_root']}")
                hw = (
                    data.get("hw_accel_display")
                    or data.get("hw_accel_detail")
                    or data.get("hw_accel")
                    or ""
                )
                if hw and hasattr(self, "lbl_hw_accel_settings"):
                    self.lbl_hw_accel_settings.config(text=hw)
                    lower = hw.lower()
                    if "已自动启用" in hw or "已启用" in hw:
                        self._set_label_color(self.lbl_hw_accel_settings, "ready")
                    elif "自动关闭" in hw or "软件模拟" in hw or "不可用" in hw:
                        self._set_label_color(self.lbl_hw_accel_settings, "need_login")
                    else:
                        self._set_label_color(self.lbl_hw_accel_settings, "muted")
            elif kind == "settings_fail":
                messagebox.showerror("保存失败", format_error(data))
            elif kind == "settings_saved":
                messagebox.showinfo("设置", "设置已保存。部分选项需重启程序后生效。")
            elif kind == "api_retry_ok":
                self.lbl_api.config(text="Apple API: 就绪")
                self._set_label_color(self.lbl_api, "ready")
                self._set_status_bar(str(data), "ready")
                messagebox.showinfo("Apple API", str(data))
                self._refresh_status()
            elif kind == "api_retry_fail":
                err = format_error(data)
                self.lbl_api.config(text="Apple API: 仍不可用")
                self._set_label_color(self.lbl_api, "fail")
                self._set_status_bar("Apple API 重试失败（请检查代理）", "fail")
                messagebox.showerror("Apple API 连接失败", err)
            elif kind == "status_data":
                if isinstance(data, dict):
                    tu = float(data.get("batch_units_total") or 0)
                    du = float(data.get("batch_units_done") or 0)
                    if tu > 0:
                        self._last_batch_units = {"done": du, "total": tu}
                self._apply_status_data(data)
            elif kind == "log_line":
                self._append_log_line(data)
            elif kind == "preview_ready":
                self._preview_loading = False
                path = data
                try:
                    self._player.open(path)
                    self._player.play()
                    self.lbl_preview_state.config(text="播放中")
                    self._set_status_bar("试听播放中", "working")
                except Exception as e:
                    messagebox.showerror("试听", format_error(e))
                    self.lbl_preview_state.config(text="播放失败")
            elif kind == "preview_fail":
                self._preview_loading = False
                self.lbl_preview_state.config(text="准备失败")
                messagebox.showerror("试听", format_error(data))
        if not self._exiting:
            self.root.after(100, self._process_ui_queue)

    def _apply_status_data(self, st: dict):
        lifecycle = st.get("lifecycle") or (
            "ready_logged_in" if st.get("account_ready") or st.get("vm_logged_in")
            else ("ready_need_login" if st.get("kernel_started") or st.get("ready") else "starting")
        )
        account_ready = bool(st.get("account_ready") or st.get("vm_logged_in"))
        service_ready = bool(st.get("ready"))
        self._apply_lifecycle(lifecycle, account_ready, service_ready)

        regions_list = st.get("regions") or []
        if regions_list:
            regions = ", ".join(regions_list)
            self._set_label_color(self.lbl_regions, "ready")
            if hasattr(self, "var_cache_storefront") and not self.var_cache_storefront.get().strip():
                self.var_cache_storefront.set(str(regions_list[0]).lower())
        else:
            regions = "无（登录后分配）" if st.get("kernel_started") else "—"
            self._set_label_color(
                self.lbl_regions,
                "need_login" if st.get("kernel_started") else "muted",
            )
        self.lbl_regions.config(text=f"区域: {regions}")
        self.lbl_tasks.config(text=f"任务: {st['tasks']}")
        self.lbl_speed.config(
            text=f"速度: 下载 {st['download_speed']} · 解密 {st['decrypt_speed']}",
        )
        self.lbl_mode.config(
            text=f"模式: {st.get('mode', '本地')} · {st.get('endpoint', '127.0.0.1:32767')}",
        )
        hw_text = st.get("hw_accel") or "硬件加速: …"
        self.lbl_hw_accel.config(text=hw_text)
        if st.get("hw_accel_enabled") or "已启用" in hw_text:
            self._set_label_color(self.lbl_hw_accel, "ready")
        elif "软件模拟" in hw_text or "关闭" in hw_text or "不可用" in hw_text:
            self._set_label_color(self.lbl_hw_accel, "need_login")
        else:
            self._set_label_color(self.lbl_hw_accel, "muted")
        if hasattr(self, "lbl_hw_accel_settings"):
            disp = st.get("hw_accel_display") or st.get("hw_accel_detail") or hw_text
            if disp:
                self.lbl_hw_accel_settings.config(text=disp)
                if st.get("hw_accel_enabled") or "已启用" in disp:
                    self._set_label_color(self.lbl_hw_accel_settings, "ready")
                elif "软件模拟" in disp:
                    self._set_label_color(self.lbl_hw_accel_settings, "need_login")
        raw_acct = (st.get("current_account") or st.get("saved_account") or "").strip()
        if self._account_plain:
            self.lbl_account.config(text=f"账号: {self._mask(self._account_plain)}")
            self._set_label_color(self.lbl_account, "logged_in")
        elif raw_acct and (st.get("account_ready") or st.get("vm_logged_in")):
            # Backend knows account but UI not locked yet — mask if looks like email
            self.lbl_account.config(text=f"账号: {self._mask(raw_acct)}")
            self._set_label_color(self.lbl_account, "logged_in")
        elif self._boot_phase == "starting":
            self.lbl_account.config(text="账号: —")
            self._set_label_color(self.lbl_account, "muted")
        else:
            self.lbl_account.config(text="账号: 未登录")
            self._set_label_color(self.lbl_account, "need_login")
        if st.get("api_ready"):
            self.lbl_api.config(text="Apple API: 正常")
            self._set_label_color(self.lbl_api, "ready")
        else:
            brief = (st.get("api_error") or "未连通").splitlines()[0][:48]
            self.lbl_api.config(text=f"Apple API: 异常 · {brief}")
            self._set_label_color(self.lbl_api, "fail")
        root = st.get("download_root", "—")
        self.lbl_download_root.config(text=f"目录: {root}")
        self.lbl_download_hint.config(text=f"保存: {root}")
        self._update_login_buttons(st)
        self._update_download_tasks(st.get("download_tasks") or [])
        dl_hist = st.get("download_history") or []
        dec_hist = st.get("decrypt_history") or []
        dl_cur = st.get("download_speed") or "0 kB/s"
        dec_cur = st.get("decrypt_speed") or "0 kB/s"
        self.chart_download.set_values(dl_hist, dl_cur)
        self.chart_decrypt.set_values(dec_hist, dec_cur)

        if self._lifecycle in ("ready_need_login", "ready_logged_in", "logged_in_warming") and not self._task_finished:
            bar = self.status_bar.cget("text") or ""
            if bar.startswith("正在启动") or bar.startswith("连接") or "启动" in bar[:4]:
                if self._lifecycle == "ready_logged_in":
                    self._set_status_bar("解密就绪 · 可查询/下载", "ready")
                elif self._lifecycle == "logged_in_warming":
                    self._set_status_bar("已登录 · 解密实例准备中", "working")
                else:
                    self._set_status_bar("管理在线 · 请登录", "need_login")

    def _append_log_line(self, line: str):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self._log_line_count += 1
        if self._log_line_count > self._max_log_lines:
            self.log_text.delete("1.0", "2.0")
            self._log_line_count -= 1
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state=tk.DISABLED)
        self._log_line_count = 0

    def _update_login_buttons(self, st: dict):
        vm_logged_in = bool(st.get("vm_logged_in") or st.get("account_ready"))
        saved = (st.get("saved_account") or "").strip()
        current_txt = self.lbl_login_status.cget("text") or ""
        busy = (
            current_txt.startswith("登录中")
            or current_txt.startswith("正在重新")
            or current_txt.startswith("启动")
        )

        if vm_logged_in:
            plain = self._account_plain or saved
            if plain and not self._account_locked:
                self._lock_logged_in_account(plain)
            elif plain and self._account_locked:
                # Refresh masked labels only
                masked = self._mask(plain)
                if not busy:
                    self.lbl_login_status.config(text=f"已登录: {masked}")
                    self._set_label_color(self.lbl_login_status, "logged_in")
                self.btn_logout.config(state=tk.NORMAL)
                self.btn_login.config(state=tk.DISABLED)
            else:
                self.btn_logout.config(state=tk.NORMAL)
                if not busy:
                    self.lbl_login_status.config(text="已登录（凭据在内核）")
                    self._set_label_color(self.lbl_login_status, "logged_in")
            return

        # Not logged in
        self.btn_logout.config(state=tk.DISABLED)
        if self._account_locked:
            return
        if busy:
            return
        if current_txt.startswith("失败") or current_txt.startswith("登录失败"):
            return
        if self._boot_phase == "starting":
            self.lbl_login_status.config(text="启动中…")
            self._set_label_color(self.lbl_login_status, "starting")
        else:
            if current_txt not in ("可登录", "已登出", "已取消") and not current_txt.startswith("失败"):
                self.lbl_login_status.config(text="未登录")
                self._set_label_color(self.lbl_login_status, "need_login")

    # ── local cache import ───────────────────────────────────

    def _cache_sel_mark(self, candidate_id: str) -> str:
        return "☑" if self._cache_selected.get(candidate_id, False) else "☐"

    def _cache_status_tag(self, label: str) -> str:
        if label in ("完成",):
            return "st_done"
        if label in ("失败", "不可导入", "资源不完整", "当前版本暂不支持"):
            return "st_fail"
        if label in ("读取中", "下载中", "解密中"):
            return "st_work"
        if label in ("等待中", "需确认"):
            return "st_need"
        return "st_ok"

    def _cache_codec_label(self, candidate: dict) -> str:
        codec = candidate.get("codec") or "未知"
        sample_rate = candidate.get("sample_rate")
        bit_depth = candidate.get("bit_depth")
        if sample_rate and bit_depth:
            return f"{codec} {int(sample_rate) // 1000}k/{bit_depth}bit"
        return codec

    def _cache_row_values(self, candidate: dict) -> tuple:
        cid = candidate.get("candidate_id", "")
        status = self._cache_status.get(cid) or candidate.get("import_status") or ""
        note = candidate.get("note") or (f"ID {candidate.get('adam_id')}" if candidate.get("adam_id") else "")
        return (
            self._cache_sel_mark(cid),
            candidate.get("track_title") or "",
            candidate.get("track_artist") or "",
            candidate.get("track_album") or "",
            self._cache_codec_label(candidate),
            candidate.get("integrity_status") or "",
            status,
            note,
        )

    def _cache_candidate_visible(self, candidate: dict) -> bool:
        selected_filter = self.var_cache_filter.get() if hasattr(self, "var_cache_filter") else "全部"
        if selected_filter == "全部":
            return True
        return candidate.get("import_status") == selected_filter

    def _refresh_cache_row(self, candidate_id: str):
        if not hasattr(self, "cache_tree") or not self.cache_tree.exists(candidate_id):
            return
        candidate = next(
            (item for item in self._cache_candidates if item.get("candidate_id") == candidate_id),
            None,
        )
        if not candidate:
            return
        status = self._cache_status.get(candidate_id) or candidate.get("import_status") or ""
        self.cache_tree.item(
            candidate_id,
            values=self._cache_row_values(candidate),
            tags=(self._cache_status_tag(status),),
        )

    def _refresh_cache_rows(self):
        if not hasattr(self, "cache_tree"):
            return
        for item in self.cache_tree.get_children():
            self.cache_tree.delete(item)
        for candidate in self._cache_candidates:
            if not self._cache_candidate_visible(candidate):
                continue
            cid = candidate.get("candidate_id", "")
            status = self._cache_status.get(cid) or candidate.get("import_status") or ""
            self.cache_tree.insert(
                "", tk.END, iid=cid, values=self._cache_row_values(candidate),
                tags=(self._cache_status_tag(status),),
            )
        if hasattr(self, "lbl_cache_summary"):
            self.lbl_cache_summary.config(text=self._cache_summary_text())

    def _cache_summary_text(self) -> str:
        total = len(self._cache_candidates)
        ready = sum(1 for item in self._cache_candidates if item.get("import_status") == "可导入")
        needs = sum(1 for item in self._cache_candidates if item.get("import_status") == "需确认")
        bad = total - ready - needs
        return f"共 {total} · 可导入 {ready} · 需确认 {needs} · 不可导入 {bad}"

    def _on_cache_tree_click(self, event):
        region = self.cache_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.cache_tree.identify_column(event.x)
        row = self.cache_tree.identify_row(event.y)
        if not row or col != "#1":
            return
        candidate = next((item for item in self._cache_candidates if item.get("candidate_id") == row), None)
        if not candidate or candidate.get("import_status") != "可导入":
            return
        self._cache_selected[row] = not self._cache_selected.get(row, False)
        self._refresh_cache_row(row)

    def _select_importable_cache(self):
        for candidate in self._cache_candidates:
            cid = candidate.get("candidate_id", "")
            self._cache_selected[cid] = candidate.get("import_status") == "可导入"
            self._refresh_cache_row(cid)

    def _invert_cache_selection(self):
        for candidate in self._cache_candidates:
            if candidate.get("import_status") != "可导入":
                continue
            cid = candidate.get("candidate_id", "")
            self._cache_selected[cid] = not self._cache_selected.get(cid, False)
            self._refresh_cache_row(cid)

    def _selected_cache_ids(self) -> list[str]:
        return [
            item.get("candidate_id", "")
            for item in self._cache_candidates
            if item.get("import_status") == "可导入"
            and self._cache_selected.get(item.get("candidate_id", ""), False)
        ]

    def _pick_cache_root(self):
        path = filedialog.askdirectory(
            title="选择 Apple Music 缓存目录",
            initialdir=self.var_cache_root.get() or str(self.base_dir),
        )
        if path:
            self.var_cache_root.set(path)

    def _scan_cache(self):
        root = self.var_cache_root.get().strip()
        if not root:
            messagebox.showwarning("本地缓存", "请选择 Apple Music 缓存目录")
            return

        def _on_progress(done: int, total: int, path: Path):
            self._ui_queue.put(("cache_scan_progress", (done, total, str(path))))

        def _worker():
            try:
                self._ui_queue.put(("cache_scan_start", None))
                results = self.backend.run_coro(
                    self.backend.scan_cache_directory(root, on_progress=_on_progress)
                ).result(timeout=1800)
                self._ui_queue.put(("cache_scan_done", results))
            except Exception as e:
                self._ui_queue.put(("cache_scan_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _import_selected_cache(self):
        candidate_ids = self._selected_cache_ids()
        if not candidate_ids:
            messagebox.showwarning("本地缓存", "请至少勾选一个「可导入」条目")
            return
        candidate_set = set(candidate_ids)
        selected = [
            item for item in self._cache_candidates
            if item.get("candidate_id") in candidate_set
        ]
        storefront = self.var_cache_storefront.get().strip()

        def _worker():
            try:
                self._ui_queue.put(("cache_import_started", {
                    "total": len(candidate_ids),
                    "candidate_ids": candidate_ids,
                    "song_ids": [item.get("adam_id") for item in selected if item.get("adam_id")],
                }))
                save_root, warnings, imported, per_status = self.backend.run_coro(
                    self.backend.import_cache_candidates(
                        candidate_ids,
                        storefront=storefront,
                        force=self.var_cache_force.get(),
                    )
                ).result(timeout=3600)
                msg = (
                    f"成功导入 {imported} 首，失败/跳过 {len(warnings)} 首。\n"
                    f"下载根目录:\n{save_root or '见设置中的下载目录'}"
                )
                if warnings:
                    msg += f"\n\n失败或跳过的条目 ({len(warnings)} 首):\n" + "\n".join(warnings[:8])
                    if len(warnings) > 8:
                        msg += f"\n... 另有 {len(warnings) - 8} 首"
                self._ui_queue.put(("cache_import_done", {
                    "message": msg,
                    "statuses": per_status,
                }))
            except Exception as e:
                self._ui_queue.put(("cache_import_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _status_label_from_task(self, task: dict) -> str:
        status = task.get("status", "")
        if status == "DONE":
            return "完成"
        if status == "FAILED":
            return "失败"
        return task.get("status_label") or status

    def _status_tag(self, label: str) -> str:
        if label in ("完成",):
            return "st_done"
        if label in ("失败", "查询失败"):
            return "st_fail"
        if label in ("下载中", "解密中"):
            return "st_work"
        if label in ("等待中",):
            return "st_wait"
        if label in ("已查询",):
            return "st_ok"
        return "st_ok"

    def _update_download_tasks(self, tasks: list[dict]):
        self._sync_batch_finished_from_tasks(tasks)
        self._refresh_batch_progress_bar(tasks)

        if not tasks:
            if (
                self._download_batch_total <= 0
                and not self.lbl_download_progress.cget("text").startswith("正在")
                and "下载结束" not in self.lbl_download_progress.cget("text")
            ):
                self.lbl_download_progress.config(text="")
                self.download_progress["value"] = 0
            return

        for task in tasks:
            sid = str(task.get("id", ""))
            if not sid:
                continue
            label = self._status_label_from_task(task)
            prev = self._song_status.get(sid)
            if prev in ("完成", "失败") and label in ("等待中", "下载中", "解密中"):
                continue
            self._song_status[sid] = label
            self._refresh_song_row(sid)
            for cid in self._cache_adam_to_candidates.get(sid, set()):
                mapped = "读取中" if label == "下载中" else label
                prev_cache = self._cache_status.get(cid)
                if prev_cache in ("完成", "失败") and mapped in ("等待中", "读取中", "解密中"):
                    continue
                self._cache_status[cid] = mapped
                self._refresh_cache_row(cid)

    def _sync_batch_finished_from_tasks(self, tasks: list[dict]):
        if self._download_batch_total <= 0:
            return
        for task in tasks:
            sid = str(task.get("id", ""))
            if sid in self._download_batch_ids and task.get("status") in ("DONE", "FAILED"):
                self._download_batch_finished.add(sid)

    def _eta_tick_loop(self):
        """1 Hz smooth countdown + preview transport refresh."""
        if self._exiting:
            return
        try:
            if self._eta_quality.active:
                _, txt, changed = self._eta_quality.tick()
                if changed:
                    self.lbl_quality_eta.config(text=txt)
            if self._eta_download.active:
                _, txt, changed = self._eta_download.tick()
                if changed:
                    self.lbl_download_eta.config(text=txt)
            self._preview_poll_ui()
        except Exception:
            pass
        if not self._exiting:
            self.root.after(250, self._eta_tick_loop)

    def _refresh_batch_progress_bar(self, tasks: list[dict]):
        # Prefer two-phase unit progress from backend when available
        st_units = None
        try:
            # units injected via last status_data poll
            st_units = getattr(self, "_last_batch_units", None)
        except Exception:
            st_units = None

        if st_units and st_units.get("total", 0) > 0:
            total_u = float(st_units["total"])
            done_u = float(st_units.get("done", 0))
            # Cap at total-ε while download session still running so ETA won't hit 00:00 early
            active_batch = self._download_batch_total > 0 and (
                len(self._download_batch_finished) < self._download_batch_total
            )
            display_done = done_u
            if active_batch and done_u >= total_u - 1e-9:
                display_done = max(0.0, total_u - 0.05)
            overall = min(100.0, (done_u / total_u) * 100.0) if total_u else 0.0
            self.download_progress["value"] = overall
            _, eta_txt, eta_ref = self._eta_download.update(display_done, total_u)
            # Interleaved: each song = 2 units; show song index rather than global phase
            songs_done = int(done_u // 2)
            songs_total = max(1, int(round(total_u / 2.0)))
            if done_u + 1e-9 >= total_u:
                phase = "完成"
            else:
                phase = f"交叉下载 {min(songs_done + 1, songs_total)}/{songs_total}"
            self.lbl_download_progress.config(
                text=f"{phase} · 进度 {done_u:.0f}/{total_u:.0f} · {overall:.0f}%",
            )
            if self._cache_import_total > 0:
                cache_done = min(
                    self._cache_import_total,
                    max(len(self._cache_import_finished), int(done_u // 2)),
                )
                active_index = min(
                    self._cache_import_total,
                    int(done_u // 2) + 1,
                )
                cache_phase = "正在解密" if (done_u % 2.0) >= 1.0 else "正在准备"
                if done_u >= total_u - 1e-9:
                    cache_phase = "导入完成"
                    active_index = self._cache_import_total
                self.cache_progress["maximum"] = max(self._cache_import_total, 1)
                self.cache_progress["value"] = min(self._cache_import_total, done_u / 2.0)
                self.lbl_cache_progress.config(
                    text=(
                        f"{cache_phase} {active_index}/{self._cache_import_total}"
                        f" · 已完成 {cache_done}/{self._cache_import_total}"
                        f" · {overall:.0f}%"
                    ),
                )
            if eta_ref:
                self.lbl_download_eta.config(text=eta_txt)
            if phase != "完成":
                self._set_status_bar(f"{phase} · {eta_txt}", "working")
            if not active_batch and done_u >= total_u - 1e-9:
                self.lbl_download_eta.config(text="剩余 00:00")
            return

        if self._download_batch_total <= 0:
            active = [t for t in tasks if t.get("status") not in ("DONE", "FAILED")]
            if not active:
                return
            avg = sum(t.get("progress", 0) for t in active) / len(active)
            self.download_progress["value"] = avg
            # Use coarse status only as weak signal — never trust 80% as near-done
            units_done = min(0.55, avg / 100.0 * 0.7)
            self._eta_download.set_total(1.0)
            _, eta_txt, eta_ref = self._eta_download.update(units_done, 1.0)
            self.lbl_download_progress.config(text=f"进行中 · {avg:.0f}%")
            if eta_ref:
                self.lbl_download_eta.config(text=eta_txt)
            self._set_status_bar(f"下载中 · {eta_txt}", "working")
            return

        batch_tasks = [
            t for t in tasks if str(t.get("id", "")) in self._download_batch_ids
        ]
        active = [t for t in batch_tasks if t.get("status") not in ("DONE", "FAILED")]
        completed = len(self._download_batch_finished)
        # Two-phase: each song counts as 2 units (download + decrypt)
        total_u = float(self._download_batch_total * 2)
        dl_done = sum(
            1 for t in batch_tasks
            if t.get("status") in ("DOWNLOADING", "DECRYPTING", "DONE")
        )
        dec_done = sum(1 for t in batch_tasks if t.get("status") in ("DONE",))
        # Approximate: finished downloads + finished decrypts
        # DOWNLOADING = partial download unit, DECRYPTING = download done + partial decrypt
        units = 0.0
        for t in batch_tasks:
            st = t.get("status")
            if st == "DONE":
                units += 2.0
            elif st == "FAILED":
                units += 2.0  # consumed slot
            elif st == "DECRYPTING":
                units += 1.0 + min(0.85, (t.get("progress") or 80) / 100.0 * 0.5)
            elif st == "DOWNLOADING":
                units += min(0.95, (t.get("progress") or 45) / 100.0)
            elif st == "WAITING":
                units += 0.0
        units = max(units, float(completed))
        overall = min(100.0, (units / total_u) * 100.0) if total_u else 0
        self.download_progress["value"] = overall
        _, eta_txt, eta_ref = self._eta_download.update(units, total_u)

        if active or completed < self._download_batch_total:
            index = min(completed + 1, self._download_batch_total)
            phase = ""
            title = ""
            if active:
                phase = active[0].get("status_label") or active[0].get("status", "")
                title = (active[0].get("title") or "")[:20]
            self.lbl_download_progress.config(
                text=(
                    f"{index}/{self._download_batch_total} · {phase}"
                    f"{' · ' + title if title else ''} · {overall:.0f}%"
                ),
            )
            if eta_ref:
                self.lbl_download_eta.config(text=eta_txt)
            self._set_status_bar(
                f"任务 {index}/{self._download_batch_total} · {eta_txt}", "working",
            )
        else:
            done_ok = sum(
                1 for sid in self._download_batch_finished
                if self._song_status.get(sid) == "完成"
            )
            fail_n = completed - done_ok
            self.download_progress["value"] = 100
            self.lbl_download_progress.config(
                text=(
                    f"完成 成功 {done_ok} / 失败 {fail_n} / 共 {self._download_batch_total} 首"
                    f" · 用时 {self._eta_download.elapsed_text()}"
                ),
            )
            self.lbl_download_eta.config(text="剩余 00:00")

    def _refresh_status(self):
        def _done(fut):
            try:
                st = fut.result()
                self._ui_queue.put(("status_data", st))
            except Exception as e:
                self._set_status_bar(
                    f"状态刷新失败: {format_error(e).splitlines()[0]}", "fail",
                )

        try:
            fut = self.backend.run_coro(self.backend.get_status())
            fut.add_done_callback(lambda f: self.root.after(0, lambda: _done(f)))
        except Exception as e:
            self._set_status_bar(
                f"状态刷新失败: {format_error(e).splitlines()[0]}", "fail",
            )

    # ── login / 2FA ──────────────────────────────────────────

    def _do_login(self):
        if self._account_locked:
            messagebox.showinfo("登录", "当前已登录。请先登出再更换账号。")
            return
        username = self._account_for_api()
        password = self.entry_password.get()
        if not username or not password:
            messagebox.showwarning("登录", "请填写邮箱与密码")
            return
        # Remember plain for post-login mask (entry still editable until success)
        self._account_plain = username
        self.btn_login.config(state=tk.DISABLED)
        self.lbl_login_status.config(text="登录中…")
        self._set_label_color(self.lbl_login_status, "working")
        self._set_status_bar("登录中…", "working")

        def _worker():
            try:
                logged_in = self.backend.run_coro(
                    self.backend.login(username, password, self._request_2fa)
                ).result(timeout=600)
                self._ui_queue.put(("login_ok", logged_in or username))
            except Exception as e:
                msg = str(getattr(e, "msg", None) or e)
                if "取消" in msg:
                    self._ui_queue.put(("login_cancelled", None))
                else:
                    self._ui_queue.put(("login_fail", msg))

        threading.Thread(target=_worker, daemon=True).start()

    def _request_2fa(self) -> tuple[str, str]:
        self._2fa_code = ""
        self._2fa_action = "submit"
        self._2fa_event.clear()
        self._ui_queue.put(("need_2fa", None))
        self._2fa_event.wait(timeout=600)
        return self._2fa_action, self._2fa_code

    def _close_2fa_dialog(self):
        if self._2fa_dialog and self._2fa_dialog.winfo_exists():
            self._2fa_dialog.destroy()
        self._2fa_dialog = None

    def _prompt_2fa(self):
        self._close_2fa_dialog()
        dialog = tk.Toplevel(self.root)
        self._2fa_dialog = dialog
        dialog.title("两步验证")
        dialog.transient(self.root)
        dialog.grab_set()
        center_window(dialog, 420, 200)
        ttk.Label(
            dialog,
            text="请输入 Apple 设备上收到的 6 位验证码。\n若未收到，可点「重新发送」让 Apple 再次推送。",
            justify=tk.LEFT,
        ).pack(padx=16, pady=(12, 8))
        entry = ttk.Entry(dialog, width=24, font=("", 12))
        entry.pack(padx=16)
        entry.focus_set()

        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill=tk.X, pady=12, padx=16)

        def _submit():
            code = entry.get().strip()
            if not code:
                messagebox.showwarning("两步验证", "请输入验证码", parent=dialog)
                return
            self._2fa_action = "submit"
            self._2fa_code = code
            self._2fa_event.set()
            self._close_2fa_dialog()

        def _resend():
            self._2fa_action = "resend"
            self._2fa_code = ""
            self._2fa_event.set()
            self._close_2fa_dialog()
            self.lbl_login_status.config(text="正在重新请求验证码...")
            self._set_label_color(self.lbl_login_status, "working")

        def _cancel():
            self._2fa_action = "cancel"
            self._2fa_code = ""
            self._2fa_event.set()
            self._close_2fa_dialog()

        ttk.Button(btn_row, text="取消", command=_cancel).pack(side=tk.RIGHT)
        ttk.Button(btn_row, text="重新发送", command=_resend).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(btn_row, text="确认", command=_submit).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", _cancel)

    def _do_logout(self):
        plain = self._account_for_api()
        if not plain:
            messagebox.showwarning("登出", "没有可登出的账号记录。")
            return
        shown = self._mask(plain) if self._account_locked else plain
        if not messagebox.askyesno("登出", f"登出 {shown}？"):
            return

        def _worker():
            try:
                self.backend.run_coro(self.backend.logout_current(plain)).result(timeout=60)
                self._ui_queue.put(("logout_ok", None))
            except Exception as e:
                self._ui_queue.put(("logout_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    # ── quality / download ───────────────────────────────────

    def _open_download_folder(self):
        def _worker():
            try:
                root = self.backend.run_coro(self.backend.get_status()).result(timeout=30)["download_root"]
                os.startfile(root)  # type: ignore[attr-defined]
            except Exception as e:
                # Do not use download_fail (one-shot exit path).
                self._ui_queue.put(("settings_fail", f"无法打开文件夹: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _do_quality(self):
        url = self.entry_quality_url.get().strip()
        if not url:
            messagebox.showwarning("音质查询", "请输入链接")
            return
        self.btn_quality.config(state=tk.DISABLED)
        self._set_status_bar("正在解析链接...", "working")

        started = {"v": False}

        def _on_progress(done: int, total: int, song: SongQualityInfo):
            # done==0: backend start (total known). done>=1: real song progress.
            if not started["v"]:
                started["v"] = True
                self._ui_queue.put(("quality_start", total))
                if done <= 0:
                    return
            self._ui_queue.put(("quality_progress", (done, total, song)))

        def _worker():
            try:
                results = self.backend.run_coro(
                    self.backend.fetch_qualities(url, on_progress=_on_progress)
                ).result(timeout=3600)
                self._ui_queue.put(("quality_done", results))
            except Exception as e:
                self._ui_queue.put(("quality_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_quality_download_mode_changed(self):
        if self.var_quality_best.get():
            self.combo_quality_codec.config(state=tk.DISABLED)
        else:
            self.combo_quality_codec.config(state="readonly")

    def _quality_best_summary(self, results: list[SongQualityInfo]) -> str:
        counts: dict[str, int] = {}
        for song in results:
            if song.best_codec:
                counts[song.best_codec] = counts.get(song.best_codec, 0) + 1
        if not counts:
            return ""
        parts = [f"{codec}×{count}" for codec, count in sorted(counts.items())]
        return f" | 推荐: {', '.join(parts)}"

    def _sel_mark(self, song_id: str) -> str:
        return "☑" if self._song_selected.get(song_id, False) else "☐"

    def _format_quality_summary(self, song: SongQualityInfo) -> str:
        if song.error:
            return f"错误: {format_error(song.error).splitlines()[0]}"
        if not song.qualities:
            return "无可用音质"
        parts = []
        for q in song.qualities:
            tag = f"★{q.codec}" if q.codec == song.best_codec else q.codec
            detail = f"{tag}"
            if q.bitrate:
                detail += f" {q.bitrate}kbps"
            if q.sample_rate:
                detail += f" {q.sample_rate}Hz"
            if q.bit_depth:
                detail += f" {q.bit_depth}bit"
            parts.append(detail)
        return " | ".join(parts)

    def _song_row_values(self, song: SongQualityInfo) -> tuple:
        sid = song.song_id
        status = self._song_status.get(sid, "已查询" if not song.error else "查询失败")
        return (
            self._sel_mark(sid),
            song.title,
            song.artist,
            self._format_quality_summary(song),
            status,
        )

    def _refresh_song_row(self, song_id: str):
        if not self.quality_tree.exists(song_id):
            return
        song = next((s for s in self._quality_results if s.song_id == song_id), None)
        if song:
            label = self._song_status.get(song_id, "已查询" if not song.error else "查询失败")
            self.quality_tree.item(
                song_id,
                values=self._song_row_values(song),
                tags=(self._status_tag(label),),
            )

    def _append_quality_row(self, song: SongQualityInfo):
        sid = song.song_id
        self._song_selected.setdefault(sid, False)
        self._song_status.setdefault(sid, "已查询" if not song.error else "查询失败")
        label = self._song_status[sid]
        tag = self._status_tag(label)
        if self.quality_tree.exists(sid):
            self.quality_tree.item(sid, values=self._song_row_values(song), tags=(tag,))
        else:
            self.quality_tree.insert(
                "", tk.END, iid=sid, values=self._song_row_values(song), tags=(tag,),
            )

    def _on_song_tree_click(self, event):
        region = self.quality_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.quality_tree.identify_column(event.x)
        row = self.quality_tree.identify_row(event.y)
        if not row:
            return
        if col == "#1":
            cur = self._song_selected.get(row, False)
            self._song_selected[row] = not cur
            self._refresh_song_row(row)
        # Update preview title on row click
        song = next((s for s in self._quality_results if s.song_id == row), None)
        if song:
            self.lbl_preview_title.config(
                text=f"{song.artist} — {song.title}",
                foreground=COLOR["info"],
            )

    def _focus_list_search(self, _event=None):
        try:
            self.notebook.select(1)  # download tab often index 1; fall back below
        except Exception:
            pass
        try:
            # Prefer tab that contains the search entry
            for i in range(self.notebook.index("end")):
                tab = self.notebook.nametowidget(self.notebook.tabs()[i])
                if str(self.entry_list_search).startswith(str(tab)):
                    self.notebook.select(i)
                    break
        except Exception:
            pass
        try:
            self.entry_list_search.focus_set()
            self.entry_list_search.selection_range(0, tk.END)
        except Exception:
            pass
        return "break"

    def _song_search_haystack(self, song: SongQualityInfo) -> str:
        parts = [song.title or "", song.artist or "", song.song_id or ""]
        if song.best_codec:
            parts.append(song.best_codec)
        if song.qualities:
            for q in song.qualities:
                codec = getattr(q, "codec", None) or ""
                parts.append(str(codec))
                for attr in ("bit_depth", "sample_rate", "desc", "label"):
                    v = getattr(q, attr, None)
                    if v is not None:
                        parts.append(str(v))
        status = self._song_status.get(song.song_id, "")
        if status:
            parts.append(status)
        return " ".join(parts).casefold()

    def _search_list_live(self):
        q = (self.var_list_search.get() or "").strip()
        # Clear previous highlights
        for sid in list(self._search_matches):
            if self.quality_tree.exists(sid):
                tags = list(self.quality_tree.item(sid, "tags") or ())
                tags = [t for t in tags if t not in ("search_hit", "search_current")]
                self.quality_tree.item(sid, tags=tags)
        self._search_matches = []
        self._search_idx = -1
        if not q:
            self.lbl_search_hit.config(text="")
            return
        if not self._quality_results:
            self.lbl_search_hit.config(text="请先查询")
            return
        # Multi-token AND match, case-insensitive
        tokens = [t for t in q.casefold().split() if t]
        for song in self._quality_results:
            hay = self._song_search_haystack(song)
            if all(tok in hay for tok in tokens):
                self._search_matches.append(song.song_id)
                if self.quality_tree.exists(song.song_id):
                    tags = list(self.quality_tree.item(song.song_id, "tags") or ())
                    if "search_hit" not in tags:
                        tags.append("search_hit")
                    self.quality_tree.item(song.song_id, tags=tags)
        n = len(self._search_matches)
        if n:
            self.lbl_search_hit.config(text=f"{n} 处匹配")
            self._search_idx = 0
            self._search_focus_current()
        else:
            self.lbl_search_hit.config(text="无匹配")

    def _search_focus_current(self):
        if not self._search_matches:
            return
        # Clear previous current highlight
        for sid in self._search_matches:
            if self.quality_tree.exists(sid):
                tags = list(self.quality_tree.item(sid, "tags") or ())
                tags = [t for t in tags if t != "search_current"]
                if "search_hit" not in tags:
                    tags.append("search_hit")
                self.quality_tree.item(sid, tags=tags)
        self._search_idx %= len(self._search_matches)
        sid = self._search_matches[self._search_idx]
        if not self.quality_tree.exists(sid):
            return
        tags = list(self.quality_tree.item(sid, "tags") or ())
        if "search_current" not in tags:
            tags.append("search_current")
        self.quality_tree.item(sid, tags=tags)
        self.quality_tree.selection_set(sid)
        self.quality_tree.focus(sid)
        self.quality_tree.see(sid)
        # Position in full list (1-based) for professional feel
        list_pos = next(
            (i + 1 for i, s in enumerate(self._quality_results) if s.song_id == sid),
            "?",
        )
        self.lbl_search_hit.config(
            text=f"匹配 {self._search_idx + 1}/{len(self._search_matches)} · 列表第 {list_pos}/{len(self._quality_results)} 首",
        )
        song = next((s for s in self._quality_results if s.song_id == sid), None)
        if song:
            self.lbl_preview_title.config(
                text=f"{song.artist} — {song.title}",
                foreground=COLOR["info"],
            )

    def _search_list_next(self):
        if not self._search_matches:
            self._search_list_live()
            if not self._search_matches:
                return
            return
        self._search_idx += 1
        self._search_focus_current()

    def _search_list_prev(self):
        if not self._search_matches:
            self._search_list_live()
            if not self._search_matches:
                return
            return
        self._search_idx -= 1
        self._search_focus_current()

    def _selected_preview_song(self) -> Optional[SongQualityInfo]:
        sel = self.quality_tree.selection()
        sid = sel[0] if sel else None
        if not sid and self._search_matches and 0 <= self._search_idx < len(self._search_matches):
            sid = self._search_matches[self._search_idx]
        if not sid:
            return None
        return next((s for s in self._quality_results if s.song_id == sid and not s.error), None)

    def _preview_play(self):
        song = self._selected_preview_song()
        if not song:
            messagebox.showwarning("试听", "请先选择一首查询成功的曲目")
            return
        # Resume if same file paused
        if (
            self._player.path
            and self._preview_song_id == song.song_id
            and self._player.is_paused()
        ):
            try:
                self._player.play()
                self.lbl_preview_state.config(text="播放中")
                return
            except Exception as e:
                messagebox.showerror("试听", format_error(e))
                return
        if self._preview_loading:
            return
        codec = song.best_codec
        if not codec and song.qualities:
            from src.quality import pick_best_codec
            codec = pick_best_codec(song.qualities)
        self._preview_loading = True
        self._preview_song_id = song.song_id
        self.lbl_preview_state.config(text="准备中（下载+解密）…")
        self.lbl_preview_title.config(
            text=f"{song.artist} — {song.title}",
            foreground=COLOR["info"],
        )
        self._set_status_bar("正在准备试听…", "working")

        def _worker():
            try:
                path = self.backend.run_coro(
                    self.backend.prepare_preview_audio(
                        song.song_id, song.storefront, codec, song.title, song.artist,
                    )
                ).result(timeout=3600)
                self._ui_queue.put(("preview_ready", path))
            except Exception as e:
                self._ui_queue.put(("preview_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _preview_pause(self):
        try:
            self._player.pause()
            self.lbl_preview_state.config(text="已暂停")
        except Exception as e:
            messagebox.showerror("试听", format_error(e))

    def _preview_stop(self):
        try:
            self._player.stop()
            self.var_preview_pos.set(0)
            self.lbl_preview_state.config(text="已停止")
            self.lbl_preview_time.config(text="00:00 / 00:00")
        except Exception as e:
            messagebox.showerror("试听", format_error(e))

    def _on_preview_seek_drag(self, _val=None):
        if not self._seek_dragging:
            return

    def _on_preview_seek_release(self, _event=None):
        self._seek_dragging = False
        try:
            length = self._player.length_ms()
            if length <= 0:
                return
            pos = float(self.var_preview_pos.get())
            ms = int(pos / 1000.0 * length)
            self._player.seek_ms(ms)
            if not self._player.is_paused():
                self.lbl_preview_state.config(text="播放中")
        except Exception as e:
            messagebox.showerror("试听", format_error(e))

    def _preview_poll_ui(self):
        if self._preview_loading or not self._player.path:
            return
        if self._seek_dragging:
            return
        try:
            length = self._player.length_ms()
            pos = self._player.position_ms()
            if length > 0:
                self.var_preview_pos.set(min(1000.0, pos / length * 1000.0))
            def _fmt(ms: int) -> str:
                s = max(0, ms // 1000)
                return f"{s // 60:02d}:{s % 60:02d}"
            self.lbl_preview_time.config(text=f"{_fmt(pos)} / {_fmt(length)}")
            if self._player.is_playing():
                self.lbl_preview_state.config(text="播放中")
            elif self._player.is_paused():
                self.lbl_preview_state.config(text="已暂停")
            elif length > 0 and pos >= length - 400:
                self.lbl_preview_state.config(text="结束")
        except Exception:
            pass

    def _select_all_songs(self):
        for song in self._quality_results:
            self._song_selected[song.song_id] = True
            self._refresh_song_row(song.song_id)

    def _invert_song_selection(self):
        for song in self._quality_results:
            sid = song.song_id
            self._song_selected[sid] = not self._song_selected.get(sid, False)
            self._refresh_song_row(sid)

    def _get_selected_results(self) -> list[SongQualityInfo]:
        return [
            s for s in self._quality_results
            if self._song_selected.get(s.song_id, False) and not s.error
        ]

    def _download_from_quality(self):
        url = self.entry_quality_url.get().strip()
        if not url:
            messagebox.showwarning("下载", "请先输入链接")
            return
        if self._quality_results:
            selected = self._get_selected_results()
            if not selected:
                messagebox.showwarning(
                    "下载",
                    "请至少勾选一首可下载的曲目（点击「选」列切换 ☑/☐）",
                )
                return
            fixed_codec = None if self.var_quality_best.get() else self.combo_quality_codec.get()
            self._start_selected_download(url, selected, fixed_codec=fixed_codec)
            return
        if self.var_quality_best.get():
            messagebox.showwarning(
                "下载",
                "请先点击「查询」获取每首歌的可用音质，并勾选要下载的曲目",
            )
            return
        self._start_downloads(
            [url], self.combo_quality_codec.get(), self.var_force.get(), self.var_include.get(),
        )

    def _start_selected_download(
        self, url: str, results: list[SongQualityInfo], fixed_codec: Optional[str] = None,
    ):
        mode = f"统一编码 {fixed_codec}" if fixed_codec else "每首最高音质"

        def _worker():
            try:
                self._ui_queue.put(("download_started", {
                    "message": f"正在下载选中 {len(results)} 首（{mode}）",
                    "total": len(results),
                    "song_ids": [s.song_id for s in results],
                }))
                for s in results:
                    self._song_status[s.song_id] = "等待中"
                    sid = s.song_id
                    self.root.after(0, lambda x=sid: self._refresh_song_row(x))
                save_root, warnings, downloaded, per_song_status = self.backend.run_coro(
                    self.backend.download_with_best_codec_per_song(
                        url, results, self.var_force.get(), fixed_codec=fixed_codec,
                    )
                ).result(timeout=3600)
                status_labels = {
                    "DONE": "完成",
                    "FAILED": "失败",
                    "WAITING": "等待中",
                    "DOWNLOADING": "下载中",
                    "DECRYPTING": "解密中",
                }
                for s in results:
                    raw = per_song_status.get(s.song_id, "")
                    self._song_status[s.song_id] = status_labels.get(
                        raw, "完成" if not s.error else "失败",
                    )
                    sid = s.song_id
                    self.root.after(0, lambda x=sid: self._refresh_song_row(x))
                msg = (
                    f"成功 {downloaded} 首，失败/跳过 {len(warnings)} 首。\n"
                    f"下载根目录:\n{save_root or '见设置中的下载目录'}\n"
                    "歌单/专辑会在该目录下再建子文件夹存放各首歌曲。\n\n"
                    "点击确定后可继续查询或下载。"
                )
                if warnings:
                    msg += f"\n\n失败或跳过的曲目 ({len(warnings)} 首):\n" + "\n".join(warnings[:8])
                    if len(warnings) > 8:
                        msg += f"\n... 另有 {len(warnings) - 8} 首"
                self._ui_queue.put(("download_done", msg))
            except Exception as e:
                self._ui_queue.put(("download_fail", f"{url}\n{e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _start_downloads(self, urls: list[str], codec: str, force: bool, include: bool):
        def _worker():
            save_root = ""
            for url in urls:
                try:
                    self._ui_queue.put(("download_started", {
                        "message": f"正在下载: {url}",
                        "total": 1,
                        "song_ids": [],
                    }))
                    save_root = self.backend.run_coro(
                        self.backend.download(url, codec, force, include_participate=include)
                    ).result(timeout=3600)
                except Exception as e:
                    self._ui_queue.put(("download_fail", f"{url}\n{e}"))
                    return
            self._ui_queue.put((
                "download_done",
                f"文件已保存到:\n{save_root or '见设置中的下载目录'}\n\n点击确定后可继续查询或下载。",
            ))

        threading.Thread(target=_worker, daemon=True).start()

    # ── settings ─────────────────────────────────────────────

    def _load_settings_form(self):
        def _worker():
            try:
                cfg = self.backend.run_coro(self.backend.get_config_dict()).result(timeout=30)
                display = {
                    **cfg,
                    "parallelNum": str(cfg["parallelNum"]),
                    "maxRunningTasks": str(cfg["maxRunningTasks"]),
                }
                self._ui_queue.put(("settings_loaded", display))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _save_settings(self):
        updates = {
            "dirPathFormat": self.setting_vars["dirPathFormat"].get(),
            "playlistDirPathFormat": self.setting_vars["playlistDirPathFormat"].get(),
            "songNameFormat": self.setting_vars["songNameFormat"].get(),
            "proxy": self.setting_vars["proxy"].get(),
            "appleCDNIP": self.setting_vars["appleCDNIP"].get(),
            "parallelNum": self.setting_vars["parallelNum"].get(),
            "maxRunningTasks": self.setting_vars["maxRunningTasks"].get(),
            "codecAlternative": self.setting_vars["codecAlternative"].get(),
            "saveLyrics": self.setting_vars["saveLyrics"].get(),
            "saveCover": self.setting_vars["saveCover"].get(),
            "failedSongNotPassIntegrityCheck": self.setting_vars["failedSongNotPassIntegrityCheck"].get(),
            "language": self.setting_vars["language"].get(),
            "memoryMB": self.setting_vars["memoryMB"].get(),
        }

        def _worker():
            try:
                self.backend.run_coro(self.backend.apply_config(updates)).result(timeout=30)
                self._ui_queue.put(("settings_saved", None))
            except Exception as e:
                self._ui_queue.put(("settings_fail", str(e)))

        threading.Thread(target=_worker, daemon=True).start()

    def _pick_download_root(self):
        path = filedialog.askdirectory(title="选择下载根目录")
        if path:
            self.setting_vars["dirPathFormat"].set(path.replace("\\", "/"))

    def _on_close(self):
        if self._exiting:
            return
        if messagebox.askokcancel(
            "退出",
            "退出将中断任务，并结束本地内核进程（QEMU）。",
        ):
            self._exit_quietly()

    def run(self):
        self.root.mainloop()


def main():
    configured_root = os.environ.get("AMD_PROJECT_ROOT")
    base_dir = Path(configured_root).resolve() if configured_root else Path(__file__).resolve().parent.parent
    if not (base_dir / "main.py").exists():
        base_dir = Path(__file__).resolve().parent.parent
    if not (base_dir / "main.py").exists():
        base_dir = Path.cwd()
    app = AppleMusicGUI(base_dir)
    app.run()


if __name__ == "__main__":
    main()
