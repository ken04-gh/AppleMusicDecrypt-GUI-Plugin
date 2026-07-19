"""Async service layer bridging AppleMusicDecrypt core and the GUI."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from creart import add_creator, it, supported

from gui.session import clear_account, load_account, save_account, session_path
from gui.vm_account import VmAccountState, read_vm_account_state


@dataclass
class SongQualityInfo:
    song_id: str
    title: str
    artist: str
    storefront: str = ""
    qualities: list = field(default_factory=list)
    error: Optional[str] = None
    best_codec: Optional[str] = None


class BackendService:
    """Runs asyncio event loop in a background thread for GUI integration."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._init_error: Optional[str] = None
        self.local_instance = None
        self.ripper = None
        self._status_callbacks: list[Callable[[], None]] = []
        self._progress_callbacks: list[Callable[[str], None]] = []
        self._song_status_callbacks: list[Callable[[str, str, Optional[str]], None]] = []
        self._core_loaded = False
        self._current_username: Optional[str] = load_account(self.base_dir)
        self._quality_cache: dict[str, list] = {}
        self._quality_negative_cache: dict[str, str] = {}
        self._existence_cache: dict[str, bool] = {}
        self._catalog_hls_cache: dict[str, Optional[str]] = {}
        self._quality_health_song_id: Optional[str] = None
        self._last_playlist_ctx = None
        # Parallelism for track list resolution / existence checks (m3u8 stays serial)
        self._quality_concurrency = 6
        self._resolve_concurrency = 12
        self._vm_logged_in = False
        self._music_user_token: Optional[str] = None
        self._precheck_ready = False
        self._kernel_ready = False
        self._api_ready = False
        self._api_error: Optional[str] = None
        self._batch_cancelled = False
        self._needs_kernel_restart = False
        self._batch_progress_units_total = 0
        self._batch_progress_units_done = 0.0
        self._cache_import_candidates: dict[str, Any] = {}
        self._quality_probe_lock: Optional[asyncio.Lock] = None
        self._decrypt_recovery_lock: Optional[asyncio.Lock] = None
        self._cache_progress_song_id: Optional[str] = None
        self._cache_progress_index = 0

    def resolve_current_account(self) -> Optional[str]:
        return self._current_username or load_account(self.base_dir)

    def on_progress(self, callback: Callable[[str], None]):
        self._progress_callbacks.append(callback)

    def on_song_status(self, callback: Callable[[str, str, Optional[str]], None]):
        self._song_status_callbacks.append(callback)

    def _emit_song_status(self, song_id: str, status, err: Optional[str] = None):
        from src.task import Status

        if song_id == self._cache_progress_song_id:
            if status == Status.DECRYPTING:
                self._batch_progress_units_done = max(
                    self._batch_progress_units_done,
                    float(self._cache_progress_index * 2 + 1),
                )
            elif status in (Status.DONE, Status.FAILED):
                self._batch_progress_units_done = max(
                    self._batch_progress_units_done,
                    float((self._cache_progress_index + 1) * 2),
                )

        labels = {
            Status.WAITING: "等待中",
            Status.DOWNLOADING: "下载中",
            Status.DECRYPTING: "解密中",
            Status.DONE: "完成",
            Status.FAILED: "失败",
        }
        label = labels.get(status, str(status))
        for cb in list(self._song_status_callbacks):
            try:
                cb(song_id, label, err)
            except Exception:
                pass

    def _emit_progress(self, message: str):
        for cb in list(self._progress_callbacks):
            try:
                cb(message)
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=600)
        if self._init_error:
            raise RuntimeError(self._init_error)

    def _load_core(self):
        if self._core_loaded:
            return
        from asyncio import AbstractEventLoop
        from src.config import Config
        from src.api import WebAPI
        from src.grpc.manager import WrapperManager
        from src.qemu import QemuInstance
        from src.rip import Ripper
        from src.utils import check_dep, run_sync, safely_create_task

        if not supported(AbstractEventLoop):
            from creart.builtins.loop import EventLoopCreator

            add_creator(EventLoopCreator)

        if not supported(Config):
            from src.logger import LoggerCreator
            from src.config import ConfigCreator
            from src.api import APICreator
            from src.grpc.manager import WMCreator
            from src.measurer import MeasurerCreator
            add_creator(LoggerCreator)
            add_creator(ConfigCreator)
            add_creator(APICreator)
            add_creator(WMCreator)
            add_creator(MeasurerCreator)

        self._Config = Config
        self._WebAPI = WebAPI
        self._WrapperManager = WrapperManager
        self._run_sync = run_sync
        self._safely_create_task = safely_create_task
        self._check_dep = check_dep
        self.local_instance = QemuInstance()
        self.ripper = Ripper()
        self._normalize_ripper_compat()
        self.ripper.on_task_status(
            lambda song_id, status: self._emit_song_status(song_id, status),
        )
        self._core_loaded = True

    def _normalize_ripper_compat(self):
        """Make GUI startup tolerant of partially-overlaid older core files."""
        if not self.ripper or getattr(self.ripper, "_amd_backend_compat_ready", False):
            return

        from src.task import Status

        dm = getattr(self.ripper, "download_manager", None)
        if dm is not None:
            if not hasattr(dm, "finished_snapshots"):
                dm.finished_snapshots = {}
            if not hasattr(dm, "clear_finished_snapshots"):
                def _clear_finished_snapshots():
                    dm.finished_snapshots.clear()

                dm.clear_finished_snapshots = _clear_finished_snapshots
            if not hasattr(dm, "list_tasks"):
                def _list_tasks():
                    mapping = getattr(dm, "adam_id_task_mapping", {})
                    return list(mapping.values())

                dm.list_tasks = _list_tasks

        if not hasattr(self.ripper, "on_task_status"):
            self.ripper.on_task_status = lambda _callback: None
        if not hasattr(self.ripper, "clear_cancel"):
            self.ripper.clear_cancel = lambda: None
        if not hasattr(self.ripper, "request_cancel_all"):
            self.ripper.request_cancel_all = lambda: None
        if not hasattr(self.ripper, "on_decrypt_stream_lost"):
            async def _on_decrypt_stream_lost(*_args, **_kwargs):
                return None

            self.ripper.on_decrypt_stream_lost = _on_decrypt_stream_lost

        if not hasattr(self.ripper, "rip_cached_song"):
            async def _rip_cached_song(*_args, **_kwargs):
                raise RuntimeError("缓存导入需要新版 src/rip.py，请完整覆盖插件文件后重试")

            self.ripper.rip_cached_song = _rip_cached_song

        for method_name in ("rip_library_playlist", "rip_library_album", "rip_library_song"):
            if hasattr(self.ripper, method_name):
                continue

            async def _unsupported(*_args, _method_name=method_name, **_kwargs):
                raise RuntimeError(f"{_method_name} 需要新版 src/rip.py，请完整覆盖插件文件后重试")

            setattr(self.ripper, method_name, _unsupported)

        original_rip_song = getattr(self.ripper, "rip_song", None)
        if original_rip_song and not getattr(self.ripper, "_amd_rip_song_wrapped", False):
            supported_kwargs = set(inspect.signature(original_rip_song).parameters)

            async def _rip_song_compat(*args, **kwargs):
                filtered_kwargs = {k: v for k, v in kwargs.items() if k in supported_kwargs}
                try:
                    result = await original_rip_song(*args, **filtered_kwargs)
                except asyncio.CancelledError as exc:
                    return Status.FAILED, str(exc)
                except Exception as exc:
                    return Status.FAILED, str(exc)
                if isinstance(result, tuple) and len(result) == 2:
                    return result
                return Status.DONE, None

            self.ripper.rip_song = _rip_song_compat
            self.ripper._amd_rip_song_wrapped = True

        self.ripper._amd_backend_compat_ready = True

    def _run_loop(self):
        os.chdir(self.base_dir)
        deps = self.base_dir / "deps"
        os.environ["PATH"] = str(deps) + os.pathsep + os.environ.get("PATH", "")

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._quality_probe_lock = asyncio.Lock()
        try:
            loop.run_until_complete(self._bootstrap_precheck())
        except Exception as e:
            self._init_error = str(e)
        finally:
            self._ready.set()
        if self._init_error:
            return
        loop.run_forever()

    def start_kernel(self, timeout_sec: int = 900):
        if not self._loop:
            raise RuntimeError("Backend not started")
        fut = asyncio.run_coroutine_threadsafe(self._bootstrap_kernel(), self._loop)
        return fut.result(timeout=timeout_sec)

    def _detect_hw_accel(self):
        from src.hwaccel import detect_hardware_acceleration

        if not getattr(self, "_hw_accel_info", None):
            self._hw_accel_info = detect_hardware_acceleration(self.base_dir)
        return self._hw_accel_info

    def _try_enable_whp(self, cfg):
        """Auto-detect and apply HW accel; never respect a manual toggle."""
        from src.hwaccel import apply_hardware_acceleration

        info = apply_hardware_acceleration(cfg, self.base_dir)
        self._hw_accel_info = info
        self._emit_progress(info.summary() + " — " + info.message)

    def _apply_performance_profile(self, cfg):
        """Raise low resource caps that throttle decrypt/download (non-destructive)."""
        changed = False
        # VM memory: 512M starves wrapper decrypt; prefer ≥2G when possible
        raw = (cfg.localInstance.memorySize or "512M").strip().upper()
        try:
            if raw.endswith("G"):
                mb = int(float(raw[:-1]) * 1024)
            else:
                mb = int(raw.replace("M", "") or "512")
        except Exception:
            mb = 512
        if mb < 1536:
            cfg.localInstance.memorySize = "2048M"
            changed = True
        # Concurrent CDN song fetches (decrypt remains pipelined per song)
        if getattr(cfg.download, "parallelNum", 1) < 2:
            cfg.download.parallelNum = 3
            changed = True
        if getattr(cfg.download, "maxRunningTasks", 1) < 2:
            cfg.download.maxRunningTasks = 2
            changed = True
        if changed:
            try:
                cfg.save_to_file(str(self.base_dir / "config.toml"))
            except Exception:
                pass
            # Hot-update locks created at core load with old caps
            try:
                if self.ripper and getattr(self.ripper, "download_manager", None):
                    self.ripper.download_manager.task_lock = asyncio.Semaphore(
                        int(cfg.download.maxRunningTasks),
                    )
            except Exception:
                pass
            try:
                if self._core_loaded:
                    it(self._WebAPI).download_lock = asyncio.Semaphore(
                        int(cfg.download.parallelNum),
                    )
            except Exception:
                pass
            self._emit_progress(
                f"性能配置: 内存 {cfg.localInstance.memorySize} · "
                f"并发下载 {cfg.download.parallelNum} · 任务 {cfg.download.maxRunningTasks}",
            )

    def _ensure_absolute_download_paths(self, cfg):
        defaults = {
            "dirPathFormat": "downloads",
            "playlistDirPathFormat": "downloads",
            "songNameFormat": "{artist} - {title}",
        }
        changed = False
        for attr, default in defaults.items():
            val = (getattr(cfg.download, attr) or "").strip()
            if not val:
                setattr(cfg.download, attr, default)
                changed = True

        for attr in ("dirPathFormat", "playlistDirPathFormat"):
            fmt = getattr(cfg.download, attr)
            if "{" in fmt:
                prefix, tail = fmt.split("{", 1)
                prefix = prefix.rstrip("/\\")
                template = "{" + tail
            else:
                prefix, template = fmt.rstrip("/\\"), ""
            if not prefix:
                prefix = "downloads"
                template = template or ""
                fmt = f"{prefix}/{template}" if template else prefix
                setattr(cfg.download, attr, fmt)
                changed = True
            if Path(prefix).is_absolute():
                continue
            new_prefix = str((self.base_dir / prefix).resolve()).replace("\\", "/")
            new_fmt = f"{new_prefix}/{template}" if template else new_prefix
            setattr(cfg.download, attr, new_fmt)
            changed = True
        if changed:
            cfg.save_to_file(str(self.base_dir / "config.toml"))

    async def _wait_wrapper_online(self, timeout_sec: int = 240):
        """Wait until gRPC Status() works. Do NOT require Status.ready=true.

        Lifecycle:
        1) Manager online (this method) → user may log in
        2) After login → regions / ready (see _await_decrypt_ready)
        Blocking forever on ready=true before login is wrong for local WM.
        """
        from src.qemu import _port_open, build_wm_args

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + timeout_sec
        last_err = ""
        last_detail = ""
        ok_streak = 0
        port_down_ticks = 0
        recovered_once = False

        try:
            cfg = it(self._Config)
            cfg.localInstance.startArgs = build_wm_args(
                cfg.localInstance.startArgs, cfg.download.proxy,
            )
        except Exception:
            pass

        while loop.time() < deadline:
            if self.local_instance and not self.local_instance.qemu_running():
                raise RuntimeError(
                    "QEMU 已退出。请结束残留 qemu 进程后重开；若反复崩溃请改用软件模拟（已自动）。",
                )
            if not await _port_open("127.0.0.1", 32767, timeout=0.8):
                port_down_ticks += 1
                ok_streak = 0
                self._emit_progress("等待管理端口 32767…")
                if (
                    port_down_ticks >= 20
                    and not recovered_once
                    and self.local_instance
                    and int(loop.time() - started_at) > 40
                ):
                    recovered_once = True
                    port_down_ticks = 0
                    self._emit_progress("管理端口丢失，尝试拉起服务一次…")
                    try:
                        await self.local_instance.restart_wrapper_service(
                            on_wait=self._emit_progress,
                        )
                        wm = it(self._WrapperManager)
                        await wm.close_channel()
                        await wm.init("127.0.0.1:32767", False)
                    except Exception as exc:
                        last_err = str(exc)
                await asyncio.sleep(1)
                continue

            port_down_ticks = 0
            try:
                it(self._WrapperManager).status.cache_invalidate()
                st = await asyncio.wait_for(it(self._WrapperManager).status(), timeout=12)
                ok_streak += 1
                regions = list(st.regions) if st.regions else []
                last_detail = (
                    f"ready={st.ready} regions={len(regions)} clients={getattr(st, 'client_count', 0)}"
                )
                # Manager answers Status → online enough for Login RPC
                if ok_streak >= 2:
                    if st.ready or regions:
                        self._emit_progress(f"管理服务在线，解密能力可用（{last_detail}）")
                    else:
                        self._emit_progress(
                            f"管理服务在线，请登录 Apple ID 以启用解密（{last_detail}）",
                        )
                    return st
            except Exception as exc:
                ok_streak = 0
                last_err = str(getattr(exc, "msg", None) or exc)

            waited = int(loop.time() - started_at)
            self._emit_progress(
                f"连接管理服务…（{waited}s"
                f"{' · ' + last_detail if last_detail else ''}"
                f"{' · ' + last_err[:60] if last_err and not last_detail else ''}）",
            )
            await asyncio.sleep(1)

        raise TimeoutError(
            f"管理服务在 {timeout_sec}s 内无响应。\n"
            f"详情: {last_detail or last_err or '未知'}\n"
            "请结束 qemu-system-x86_64.exe 后重开；必要时在设置中配置代理。",
        )

    async def _await_decrypt_ready(self, timeout_sec: int = 180):
        """After login (or if account already present): wait for regions/ready."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        last = ""
        while loop.time() < deadline:
            try:
                it(self._WrapperManager).status.cache_invalidate()
                st = await asyncio.wait_for(it(self._WrapperManager).status(), timeout=12)
                regions = list(st.regions) if st.regions else []
                last = f"ready={st.ready} regions={regions or '[]'}"
                if regions or st.ready:
                    self._vm_logged_in = True
                    self._emit_progress(f"解密能力已就绪（{last}）")
                    return st
            except Exception as exc:
                last = str(getattr(exc, "msg", None) or exc)
            self._emit_progress(f"登录后等待解密实例…（{last}）")
            await asyncio.sleep(1)
        # Soft: credentials may still work even if ready flag lags
        self._emit_progress(
            f"登录已接受，区域尚未上报（{last}）。可稍后刷新；若下载失败请重新登录。",
        )
        return None

    async def _bootstrap_precheck(self):
        self._load_core()
        self._emit_progress("检查外部依赖 (ffmpeg / MP4Box / qemu)...")
        dep_installed, missing_dep = self._check_dep()
        if not dep_installed:
            raise RuntimeError(f"Missing dependency: {missing_dep}")

        cfg = it(self._Config)
        cfg.localInstance.enable = True
        cfg.instance.url = "127.0.0.1:32767"
        cfg.instance.secure = False
        self._emit_progress("加载并校验配置...")
        self._try_enable_whp(cfg)
        self._apply_performance_profile(cfg)
        self._ensure_absolute_download_paths(cfg)

        qcow2 = self.base_dir / "assets" / "wrapper-manager.qcow2"
        if not qcow2.exists():
            raise FileNotFoundError(f"缺少本地镜像: {qcow2}")

        self._emit_progress("连接 Apple Music API…")
        try:
            await self._init_web_api()
        except Exception as exc:
            from src.api import format_apple_network_error

            self._api_ready = False
            self._api_error = format_apple_network_error(exc)
            self._emit_progress("Apple API 未连通（可稍后配置代理重试），继续启动内核…")
        self._precheck_ready = True
        self._emit_progress("预检完成，启动本地内核…")

    async def _init_web_api(self):
        from src.api import format_apple_network_error

        def _do():
            api = it(self._WebAPI)
            api.init()
            return True

        try:
            await self._run_sync(_do)
            self._api_ready = True
            self._api_error = None
            self._emit_progress("Apple Music API 初始化成功")
        except Exception as exc:
            self._api_ready = False
            self._api_error = format_apple_network_error(exc)
            raise RuntimeError(self._api_error) from exc

    async def reinit_web_api(self) -> str:
        """Re-fetch developer token after proxy/network changes."""
        self._load_core()
        cfg = it(self._Config)
        try:
            api = it(self._WebAPI)
            api.set_proxy(cfg.download.proxy or "")
            await self._run_sync(lambda: api.ensure_token(force=True))
            self._api_ready = True
            self._api_error = None
            self._emit_progress("Apple Music API 已重新连接成功")
            return "Apple Music API 已连接成功"
        except Exception as exc:
            from src.api import format_apple_network_error

            self._api_ready = False
            self._api_error = format_apple_network_error(exc)
            raise RuntimeError(self._api_error) from exc

    async def ensure_api_ready(self):
        if self._api_ready:
            return
        try:
            await self.reinit_web_api()
        except Exception:
            raise RuntimeError(
                self._api_error
                or "Apple Music API 尚未就绪。请检查网络/代理后在设置页点击「重试连接 Apple API」。"
            )

    async def _bootstrap_kernel(self):
        """Start QEMU + manager (login-capable). Full decrypt needs Apple ID.

        Stages:
        - kernel online: gRPC Status works → Login allowed
        - decrypt ready: after login (or restored session) regions/ready present
        """
        if self._kernel_ready:
            return
        await self._connect_kernel_services()
        self._safely_create_task(self._poll_status())
        try:
            await self._sync_vm_account()
        except Exception as exc:
            self._emit_progress(f"账号状态读取失败（可手动登录）: {exc}")
        self._kernel_ready = True
        self._needs_kernel_restart = False
        if self.ripper:
            self.ripper.clear_cancel()

        if self._vm_logged_in:
            self._emit_progress("已有登录会话，等待解密实例…")
            await self._await_decrypt_ready(timeout_sec=120)
            self._emit_progress("内核与账号就绪，可查询/下载")
        else:
            self._emit_progress("管理服务已在线 — 请先登录 Apple ID，再查询/下载")

    async def _bootstrap(self):
        await self._bootstrap_precheck()
        await self._bootstrap_kernel()

    async def start_kernel_only(self):
        await self._bootstrap_kernel()

    async def _connect_kernel_services(self):
        from src.qemu import QemuInstance

        cfg = it(self._Config)
        await QemuInstance.ensure_ports_free(on_wait=self._emit_progress)
        self._emit_progress("启动本地内核 (QEMU)…")
        await self.local_instance.launch_instance(
            asyncio.get_running_loop(), self.base_dir, on_wait=self._emit_progress,
        )
        self._emit_progress("连接管理服务 127.0.0.1:32767…")
        wm = it(self._WrapperManager)
        await wm.close_channel()
        await wm.init(cfg.instance.url, cfg.instance.secure)
        self._emit_progress("等待管理服务在线（登录前不必 ready）…")
        await self._wait_wrapper_online()
        self._safely_create_task(self._start_decrypt_pipeline())

    async def _start_decrypt_pipeline(self):
        await it(self._WrapperManager).decrypt_init(
            on_success=self.ripper.on_decrypt_success,
            on_failure=self.ripper.on_decrypt_failed,
            on_stream_lost=self.ripper.on_decrypt_stream_lost,
        )

    async def _reconnect_decrypt_pipeline(self):
        self._emit_progress("解密连接中断，正在重连 gRPC 与解密流...")
        wm = it(self._WrapperManager)
        await wm.stop_decrypt_stream()
        await wm.reconnect_channel()
        await self._wait_wrapper_online(timeout_sec=60)
        await self._start_decrypt_pipeline()
        await asyncio.sleep(0.6)

    async def _ensure_decrypt_pipeline_ready(self, timeout_sec: int = 20):
        """Confirm the streaming RPC is live before feeding cached samples."""
        wm = it(self._WrapperManager)
        wait_ready = getattr(wm, "wait_decrypt_stream_ready", None)
        if wait_ready is None:
            wait_ready = wm._wait_decrypt_stream_ready
        try:
            await wait_ready(timeout=timeout_sec)
            return
        except Exception:
            pass

        recovery_lock = self._decrypt_recovery_lock
        if recovery_lock is None:
            recovery_lock = asyncio.Lock()
            self._decrypt_recovery_lock = recovery_lock
        async with recovery_lock:
            try:
                await wait_ready(timeout=2)
                return
            except Exception:
                self._emit_progress("解密流尚未就绪，正在恢复连接…")
            await self._reconnect_decrypt_pipeline()
            await wait_ready(timeout=timeout_sec)

    def _clear_quality_probe_cache(self):
        self._quality_negative_cache.clear()
        self._existence_cache.clear()
        self._catalog_hls_cache.clear()
        self._quality_health_song_id = None

    async def _restart_wrapper_after_quality_probe_failure(self):
        """Reset poisoned wrapper instances without restarting the QEMU VM."""
        wm = it(self._WrapperManager)
        cfg = it(self._Config)
        self._emit_progress("检测到查询实例异常，正在隔离恢复 wrapper-manager…")
        await wm.stop_decrypt_stream()
        if not self.local_instance or not getattr(self.local_instance, "client", None):
            raise RuntimeError("本地内核控制通道不可用")
        await self.local_instance.restart_wrapper_service(on_wait=self._emit_progress)
        await wm.close_channel()
        await wm.init(cfg.instance.url, cfg.instance.secure)
        await self._wait_wrapper_online(timeout_sec=90)
        await self._start_decrypt_pipeline()
        await self._await_decrypt_ready(timeout_sec=90)
        self._quality_health_song_id = None
        self._emit_progress("查询实例已恢复，继续处理后续歌曲")

    async def _quality_probe_service_is_healthy(self, wm, failed_song_id: str) -> bool:
        """Verify server state with a song that succeeded earlier in this session."""
        health_song_id = self._quality_health_song_id
        if not health_song_id or health_song_id == failed_song_id:
            return False
        await asyncio.sleep(0.8)
        try:
            await wm.m3u8(health_song_id, probe=True)
            return True
        except Exception:
            return False

    async def restart_kernel(self):
        from src.qemu import QemuInstance

        self._clear_quality_probe_cache()
        self._emit_progress("正在关闭内核...")
        await it(self._WrapperManager).stop_decrypt_stream()
        await self.poweroff_kernel()
        await QemuInstance.wait_ports_closed(timeout_sec=25)
        self.local_instance = QemuInstance()
        self._kernel_ready = False
        it(self._WrapperManager).status.cache_invalidate()
        await self._connect_kernel_services()
        await self._sync_vm_account()
        self._kernel_ready = True
        self._needs_kernel_restart = False
        self._batch_cancelled = False
        if self.ripper:
            self.ripper.clear_cancel()
        self._emit_progress("内核已重启并就绪")

    async def _ensure_kernel_healthy(self, try_decrypt_reconnect: bool = True) -> bool:
        try:
            it(self._WrapperManager).status.cache_invalidate()
            st = await asyncio.wait_for(it(self._WrapperManager).status(), timeout=12)
            if st.ready:
                return True
        except Exception:
            pass
        if try_decrypt_reconnect:
            try:
                await self._reconnect_decrypt_pipeline()
                it(self._WrapperManager).status.cache_invalidate()
                st = await asyncio.wait_for(it(self._WrapperManager).status(), timeout=12)
                if st.ready:
                    return True
            except Exception:
                pass
        self._emit_progress("解密内核无响应，正在自动重启...")
        await self.restart_kernel()
        return True

    def _was_quality_probed_ok(self, song_id: str) -> bool:
        return any(key.split(":")[-1] == song_id for key in self._quality_cache)

    @staticmethod
    def _is_transient_rip_error(err: Optional[str], *, quality_probed: bool = False) -> bool:
        if not err:
            return False
        lower = err.lower()
        if "not found on apple music" in lower or "does not exist" in lower:
            return False
        if "failed to get m3u8" in lower:
            return quality_probed
        if "no such file or directory" in lower:
            return True
        markers = (
            "retryerror", "wrappermanagerexception", "no available instance",
            "conn read", "dial timeout", "decryption failed", "i/o timeout",
            "internal error", "unavailable", "eof", "stream removed",
            "tcp stream", "stream lost", "decrypt stream",
        )
        return any(m in lower for m in markers)

    async def _rip_song_with_recovery(self, song, codec, flags, playlist_info, song_ctx):
        from src.task import Status

        quality_probed = self._was_quality_probed_ok(song.id)
        status, err = await self.ripper.rip_song(
            song, codec, flags, playlist=playlist_info, path_context=song_ctx,
        )
        if status == Status.DONE or not self._is_transient_rip_error(err, quality_probed=quality_probed):
            return status, err
        self._emit_progress(f"下载失败，正在恢复解密连接并重试: {song.id}")
        err_lower = (err or "").lower()
        if any(
            m in err_lower
            for m in (
                "stream removed", "tcp stream", "stream lost", "decrypt stream", "eof",
                "no available instance", "failed to get m3u8", "conn read",
            )
        ):
            try:
                await self._reconnect_decrypt_pipeline()
            except Exception:
                await self._ensure_kernel_healthy(try_decrypt_reconnect=False)
        else:
            await self._ensure_kernel_healthy()
        await asyncio.sleep(0.8)
        it(self._WrapperManager).status.cache_invalidate()
        return await self.ripper.rip_song(
            song, codec, flags, playlist=playlist_info, path_context=song_ctx,
        )

    async def _rip_cached_song_with_recovery(self, candidate, storefront: str, flags, path_context):
        from src.task import Status

        status, err = await self.ripper.rip_cached_song(
            candidate, storefront, flags=flags, path_context=path_context,
        )
        err_lower = (err or "").lower()
        if (
            status == Status.DONE
            or "no such file" in err_lower
            or "缓存资源不完整" in (err or "")
            or not self._is_transient_rip_error(err, quality_probed=True)
        ):
            return status, err
        self._emit_progress(f"缓存导入失败，正在恢复解密连接并重试: {candidate.adam_id}")
        if any(
            m in err_lower
            for m in (
                "stream removed", "tcp stream", "stream lost", "decrypt stream", "eof",
                "no available instance", "conn read",
            )
        ):
            try:
                await self._reconnect_decrypt_pipeline()
            except Exception:
                await self._ensure_kernel_healthy(try_decrypt_reconnect=False)
        else:
            await self._ensure_kernel_healthy()
        await asyncio.sleep(0.8)
        it(self._WrapperManager).status.cache_invalidate()
        return await self.ripper.rip_cached_song(
            candidate, storefront, flags=flags, path_context=path_context,
        )

    async def _resolve_cache_import_storefront(self, storefront: Optional[str] = None) -> str:
        sf = (storefront or "").strip().lower()
        if sf:
            return sf
        try:
            it(self._WrapperManager).status.cache_invalidate()
            st = await it(self._WrapperManager).status()
            regions = [str(region).strip().lower() for region in list(st.regions) if str(region).strip()]
            if regions:
                return regions[0]
        except Exception:
            pass
        raise ValueError("无法确定 Apple Music 地区。请先登录，或在本地缓存页填写地区代码（如 us / cn / jp）。")

    async def _poll_status(self):
        from src.measurer import Measurer
        while True:
            try:
                it(Measurer).record_speed_tick()
            except Exception:
                pass
            for cb in list(self._status_callbacks):
                try:
                    cb()
                except Exception:
                    pass
            await asyncio.sleep(1)

    def on_status_tick(self, callback: Callable[[], None]):
        self._status_callbacks.append(callback)

    def run_coro(self, coro: Coroutine) -> asyncio.Future:
        if not self._loop:
            raise RuntimeError("Backend not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def resolve_download_root(self) -> str:
        try:
            from src.utils import get_download_base_dir
            p = get_download_base_dir()
            if not p.is_absolute():
                p = (self.base_dir / p).resolve()
            return str(p)
        except Exception:
            return str((self.base_dir / "downloads").resolve())

    async def _sync_vm_account(self):
        """Refresh login flags from wrapper-manager. Never raises for 'not logged in'."""
        it(self._WrapperManager).status.cache_invalidate()
        st = await it(self._WrapperManager).status()
        regions = list(st.regions)
        vm_state = VmAccountState()
        if self.local_instance and getattr(self.local_instance, "client", None):
            try:
                vm_state = await read_vm_account_state(self.local_instance.client, regions)
            except Exception:
                vm_state = VmAccountState(regions=regions)
        self._vm_logged_in = bool(regions) or vm_state.vm_logged_in
        if vm_state.music_token:
            self._music_user_token = vm_state.music_token
            it(self._WebAPI).set_music_user_token(vm_state.music_token)
        else:
            if not self._vm_logged_in:
                self._music_user_token = None

        saved = self.resolve_current_account()
        if self._vm_logged_in and saved:
            self._current_username = saved
        elif self._vm_logged_in and not saved:
            # Kernel has credentials but local session has no email — still considered logged in.
            self._current_username = None
        elif not self._vm_logged_in:
            # Explicit not-logged-in is a normal post-startup state, not an error.
            pass

    async def ensure_account_ready(self) -> None:
        """Query/download need: manager online + Apple ID + preferably regions."""
        if not self._kernel_ready:
            raise RuntimeError("管理服务尚未在线，请等待启动完成。")
        await self.ensure_api_ready()
        try:
            await self._sync_vm_account()
        except Exception:
            pass
        if not self._vm_logged_in:
            raise ValueError(
                "请先完成 Apple ID 登录。\n"
                "管理服务已在线；登录成功后才会创建解密实例，方可查询/下载。",
            )
        # If logged in but no regions yet, wait a bit more
        try:
            it(self._WrapperManager).status.cache_invalidate()
            st = await it(self._WrapperManager).status()
            if list(st.regions) or st.ready:
                return
            await self._await_decrypt_ready(timeout_sec=60)
        except Exception:
            pass

    def _display_account(self, regions: list[str]) -> str:
        saved = self.resolve_current_account()
        if saved:
            return saved
        if regions or self._vm_logged_in:
            region_text = ", ".join(regions) if regions else "未知"
            return f"内核已登录 (区域: {region_text})"
        return ""

    async def get_status(self) -> dict[str, Any]:
        from src.measurer import Measurer

        dl_hist, dec_hist = [], []
        try:
            dl_hist, dec_hist = it(Measurer).speed_history_kb_s()
        except Exception:
            pass

        base = {
            "precheck_ready": self._precheck_ready,
            "kernel_started": self._kernel_ready,
            "needs_kernel_restart": self._needs_kernel_restart,
            "tasks": 0,
            "download_speed": "0 kB/s",
            "decrypt_speed": "0 kB/s",
            "download_history": dl_hist,
            "decrypt_history": dec_hist,
            "mode": "本地内核",
            "endpoint": "127.0.0.1:32767",
            "current_account": self._display_account([]),
            "vm_logged_in": self._vm_logged_in,
            "account_ready": self._vm_logged_in,
            "saved_account": self.resolve_current_account(),
            "download_root": self.resolve_download_root(),
            "download_tasks": [],
            "batch_units_done": float(getattr(self, "_batch_progress_units_done", 0) or 0),
            "batch_units_total": float(getattr(self, "_batch_progress_units_total", 0) or 0),
            "ready": False,
            "regions": [],
            "client_count": 0,
            "lifecycle": self._lifecycle_label(kernel_ready=False, account_ready=False, service_ready=False),
            "api_ready": self._api_ready,
            "api_error": self._api_error or "",
            "hw_accel": self._detect_hw_accel().summary(),
            "hw_accel_detail": self._detect_hw_accel().message,
            "hw_accel_display": self._detect_hw_accel().display_text(),
            "hw_accel_enabled": bool(getattr(self._detect_hw_accel(), "enabled", False)),
        }

        if not self._kernel_ready:
            return base

        try:
            it(self._WrapperManager).status.cache_invalidate()
            st = await it(self._WrapperManager).status()
            regions = list(st.regions)
            if regions:
                self._vm_logged_in = True
            account_ready = self._vm_logged_in or bool(regions)
            decrypt_ready = bool(st.ready or regions)
            base.update({
                "ready": st.ready,
                "decrypt_ready": decrypt_ready,
                "manager_online": True,
                "regions": regions,
                "client_count": st.client_count,
                "tasks": it(Measurer).tasks_count(),
                "download_speed": it(Measurer).download_speed(),
                "decrypt_speed": it(Measurer).decrypt_speed(),
                "current_account": self._display_account(regions),
                "vm_logged_in": account_ready,
                "account_ready": account_ready,
                "download_tasks": self.get_download_tasks_snapshot(),
                "batch_units_done": float(getattr(self, "_batch_progress_units_done", 0) or 0),
                "batch_units_total": float(getattr(self, "_batch_progress_units_total", 0) or 0),
                "lifecycle": self._lifecycle_label(
                    kernel_ready=True,
                    account_ready=account_ready,
                    service_ready=True,
                    decrypt_ready=decrypt_ready,
                ),
                "api_ready": self._api_ready,
                "api_error": self._api_error or "",
            })
        except Exception:
            base["ready"] = False
            base["decrypt_ready"] = False
            base["manager_online"] = False
            base["lifecycle"] = self._lifecycle_label(
                kernel_ready=True,
                account_ready=self._vm_logged_in,
                service_ready=False,
                decrypt_ready=False,
            )
            base["api_ready"] = self._api_ready
            base["api_error"] = self._api_error or ""
        return base

    async def scan_cache_directory(
        self,
        cache_root: str,
        on_progress: Optional[Callable[[int, int, Path], None]] = None,
    ) -> list[dict[str, Any]]:
        from src.cache_import import scan_apple_music_cache

        root = Path(cache_root or (self.base_dir / "Apple Music")).expanduser()

        def _scan():
            return scan_apple_music_cache(root, on_progress=on_progress)

        candidates = await asyncio.to_thread(_scan)
        self._cache_import_candidates = {item.candidate_id: item for item in candidates}
        return [item.to_dict() for item in candidates]

    async def import_cache_candidates(
        self,
        candidate_ids: list[str],
        storefront: Optional[str] = None,
        force: bool = False,
        language: Optional[str] = None,
    ) -> tuple[str, list[str], int, dict[str, str]]:
        from src.cache_import import IMPORT_READY
        from src.flags import Flags
        from src.task import Status
        from src.types import DownloadPathContext
        from src.utils import resolve_api_language

        if not candidate_ids:
            raise ValueError("请至少选择一个可导入缓存条目")
        await self.ensure_account_ready()
        await self.ensure_api_ready()
        if self._needs_kernel_restart:
            raise ValueError("内核状态异常，请关闭软件后重新启动再导入。")

        sf = await self._resolve_cache_import_storefront(storefront)
        lang = resolve_api_language(sf, language or "")
        flags = Flags(force_save=force, language=lang)

        self._batch_cancelled = False
        if self.ripper:
            self.ripper.clear_cancel()
            self.ripper.download_manager.clear_finished_snapshots()

        warnings: list[str] = []
        imported = 0
        per_candidate_status: dict[str, str] = {}
        work: list[tuple] = []
        for candidate_id in candidate_ids:
            candidate = self._cache_import_candidates.get(candidate_id)
            if not candidate:
                warnings.append(f"{candidate_id}: 未找到缓存候选，请重新扫描")
                per_candidate_status[candidate_id] = Status.FAILED.value
                continue
            if candidate.import_status != IMPORT_READY:
                warnings.append(
                    f"{candidate.track_artist} - {candidate.track_title}: {candidate.note or candidate.import_status}",
                )
                per_candidate_status[candidate_id] = Status.FAILED.value
                continue
            if candidate.track_artist and candidate.track_album:
                path_context = DownloadPathContext(
                    kind="album",
                    container_name=candidate.track_album,
                    parent_container=candidate.track_artist,
                )
            else:
                path_context = DownloadPathContext(
                    kind="song",
                    container_name=candidate.track_title or "Unknown",
                    parent_container="本地缓存",
                )
            work.append((candidate, path_context))

        n = len(work)
        self._emit_progress(f"开始导入本地缓存（共 {n} 首）…")
        self._batch_progress_units_total = max(1, n * 2)
        self._batch_progress_units_done = 0.05
        self._cache_progress_song_id = None
        self._cache_progress_index = 0
        self._emit_progress("正在确认缓存解密流…")
        await self._ensure_decrypt_pipeline_ready()

        for i, (candidate, path_context) in enumerate(work):
            if self._batch_cancelled:
                warnings.append("缓存导入已取消")
                for candidate2, _ in work[i:]:
                    if candidate2.candidate_id not in per_candidate_status:
                        per_candidate_status[candidate2.candidate_id] = Status.FAILED.value
                        self._emit_song_status(candidate2.adam_id, Status.FAILED, "已取消")
                self._batch_progress_units_done = float(self._batch_progress_units_total)
                break

            self._cache_progress_song_id = candidate.adam_id
            self._cache_progress_index = i
            self._emit_song_status(candidate.adam_id, Status.DOWNLOADING)
            self._emit_progress(f"[{i + 1}/{n}] 读取缓存并解密: {candidate.track_title}")
            self._batch_progress_units_done = float(i * 2) + 0.15
            try:
                status, err = await self._rip_cached_song_with_recovery(
                    candidate, sf, flags, path_context,
                )
                self._emit_song_status(candidate.adam_id, status, err)
                per_candidate_status[candidate.candidate_id] = status.value
                if status == Status.DONE:
                    imported += 1
                else:
                    warnings.append(
                        f"{candidate.track_artist} - {candidate.track_title}: {err or status.value}",
                    )
            except Exception as e:
                self._emit_song_status(candidate.adam_id, Status.FAILED, str(e))
                per_candidate_status[candidate.candidate_id] = Status.FAILED.value
                warnings.append(f"{candidate.track_artist} - {candidate.track_title}: {e}")

            self._batch_progress_units_done = float((i + 1) * 2)
            await asyncio.sleep(0.06)
            if (i + 1) % 8 == 0:
                try:
                    await self._ensure_kernel_healthy()
                except Exception:
                    pass

        self._batch_progress_units_done = float(self._batch_progress_units_total)
        self._cache_progress_song_id = None
        save_root = self.resolve_download_root()
        if imported == 0:
            detail = "\n".join(warnings[:8]) if warnings else "未知错误"
            raise ValueError(f"没有缓存条目成功导入:\n{detail}")
        return save_root, warnings, imported, per_candidate_status

    @staticmethod
    def _lifecycle_label(
        *,
        kernel_ready: bool,
        account_ready: bool,
        service_ready: bool,
        decrypt_ready: bool = False,
    ) -> str:
        if not kernel_ready:
            return "starting"
        if kernel_ready and not service_ready:
            return "kernel_degraded"
        if not account_ready:
            return "ready_need_login"
        if account_ready and not decrypt_ready:
            return "logged_in_warming"
        return "ready_logged_in"

    async def get_config_dict_light(self) -> dict[str, Any]:
        """Read config without requiring kernel."""
        self._load_core()
        cfg = it(self._Config)
        return {
            "dirPathFormat": cfg.download.dirPathFormat,
            "playlistDirPathFormat": cfg.download.playlistDirPathFormat,
            "songNameFormat": cfg.download.songNameFormat,
            "proxy": cfg.download.proxy,
            "appleCDNIP": cfg.download.appleCDNIP,
            "parallelNum": cfg.download.parallelNum,
            "maxRunningTasks": cfg.download.maxRunningTasks,
            "codecAlternative": cfg.download.codecAlternative,
            "saveLyrics": cfg.download.saveLyrics,
            "saveCover": cfg.download.saveCover,
            "failedSongNotPassIntegrityCheck": cfg.download.failedSongNotPassIntegrityCheck,
            "language": cfg.region.language,
            "memoryMB": cfg.localInstance.memorySize.replace("M", ""),
            "hw_accel": self._detect_hw_accel().summary(),
            "hw_accel_detail": self._detect_hw_accel().message,
            "hw_accel_display": self._detect_hw_accel().display_text(),
            "download_root": self.resolve_download_root(),
        }

    async def cancel_current_tasks(self) -> str:
        self._batch_cancelled = True
        self._needs_kernel_restart = True
        if self.ripper:
            self.ripper.request_cancel_all()
        return "已请求取消当前任务。请重启内核后再开始新的下载。"

    @staticmethod
    def _task_status_maps():
        from src.task import Status

        return {
            Status.WAITING: ("等待中", 5),
            Status.DOWNLOADING: ("下载中", 45),
            Status.DECRYPTING: ("解密中", 80),
            Status.DONE: ("完成", 100),
            Status.FAILED: ("失败", 0),
        }

    def get_download_tasks_snapshot(self) -> list[dict[str, Any]]:
        from src.task import Status

        if not self.ripper:
            return []
        status_maps = self._task_status_maps()
        tasks = []
        active_ids: set[str] = set()

        def _append_task(snap: dict):
            try:
                status = Status(snap["status"])
            except ValueError:
                status = Status.WAITING
            label, progress = status_maps.get(status, (snap["status"], 0))
            tasks.append({
                "id": snap["id"],
                "codec": snap.get("codec", ""),
                "title": snap.get("title", snap["id"]),
                "artist": snap.get("artist", ""),
                "status": snap["status"],
                "status_label": label,
                "progress": progress,
                "error": snap.get("error", ""),
            })

        for task in self.ripper.download_manager.list_tasks():
            active_ids.add(task.adamId)
            title = task.metadata.title if task.metadata else task.adamId
            artist = task.metadata.artist if task.metadata else ""
            _append_task({
                "id": task.adamId,
                "codec": task.codec,
                "title": title,
                "artist": artist,
                "status": task.status.value,
                "error": str(task.error) if task.error else "",
            })

        for song_id, snap in self.ripper.download_manager.finished_snapshots.items():
            if song_id not in active_ids:
                _append_task(snap)

        return tasks

    async def _ensure_music_token(self):
        if self._music_user_token:
            it(self._WebAPI).set_music_user_token(self._music_user_token)
            return self._music_user_token
        await self._sync_vm_account()
        if not self._music_user_token:
            raise ValueError("无法读取 Music-User-Token，请先在「Apple ID 登录」页登录")
        return self._music_user_token

    async def login(self, username: str, password: str, on_2fa: Callable[[], tuple[str, str]]):
        from src.exceptions import LoginCancelledException, TwoFAResendException
        from src.grpc.manager import WrapperManagerException

        async def _on_2fa(u: str, p: str) -> str:
            action, code = await asyncio.to_thread(on_2fa)
            if action == "cancel":
                raise LoginCancelledException()
            if action == "resend":
                raise TwoFAResendException()
            if not code:
                raise WrapperManagerException("未输入两步验证码")
            return code

        while True:
            try:
                if not self._kernel_ready:
                    raise WrapperManagerException("管理服务尚未在线，请等待启动完成后再登录")
                await it(self._WrapperManager).login(username, password, _on_2fa)
                save_account(self.base_dir, username)
                saved = load_account(self.base_dir)
                if saved != username:
                    raise RuntimeError(f"无法保存登录状态: {session_path(self.base_dir)}")
                self._current_username = saved
                self._vm_logged_in = True
                self._clear_quality_probe_cache()
                self._emit_progress(f"登录成功，初始化解密实例: {username}")
                # Full capability only after regions/ready — this is the "core fully up" step
                await self._await_decrypt_ready(timeout_sec=180)
                try:
                    await self._sync_vm_account()
                except Exception:
                    pass
                it(self._WrapperManager).status.cache_invalidate()
                st = await it(self._WrapperManager).status()
                if list(st.regions) or st.ready:
                    self._emit_progress(f"登录完成，解密已就绪: {username}")
                    return username
                self._emit_progress(f"登录完成: {username}（区域稍后刷新）")
                return username
            except TwoFAResendException:
                self._emit_progress("正在重新请求验证码...")
                await asyncio.sleep(2)
                continue
            except LoginCancelledException:
                raise WrapperManagerException("登录已取消")
            except WrapperManagerException:
                raise

    async def logout_current(self, username: Optional[str] = None):
        name = (username or "").strip() or self.resolve_current_account()
        if not name:
            raise ValueError("当前没有已登录的 Apple ID（请先在登录页输入 Apple ID 后再登出）")
        username = name
        await it(self._WrapperManager).logout(username)
        self._current_username = None
        self._vm_logged_in = False
        self._music_user_token = None
        it(self._WebAPI).set_music_user_token("")
        clear_account(self.base_dir)
        it(self._WrapperManager).status.cache_invalidate()
        await self._sync_vm_account()

    async def resolve_url(self, raw_url: str):
        from src.url import AppleMusicURL
        url = AppleMusicURL.parse_url(raw_url)
        if url:
            return url
        real = await it(self._WebAPI).get_real_url(raw_url)
        return AppleMusicURL.parse_url(real)

    async def _build_playlist_song_index(self, playlist, default_storefront: str):
        # Sync local resolve — avoid per-track HTTP for index mapping.
        from src.utils import playlist_write_song_index
        return playlist_write_song_index(playlist, default_storefront)

    async def _resolve_track_list(
        self,
        raw_tracks: list,
        default_storefront: str,
        *,
        default_artist: str = "",
        allow_http: bool = False,
    ) -> list[tuple[str, str, str, str, str]]:
        """Resolve many tracks with limited concurrency (local-first)."""
        if not raw_tracks:
            return []
        sem = asyncio.Semaphore(self._resolve_concurrency)
        out: list[Optional[tuple[str, str, str, str, str]]] = [None] * len(raw_tracks)

        async def _one(i: int, t):
            async with sem:
                try:
                    row = await it(self._WebAPI).resolve_catalog_track_entry(
                        t, default_storefront, allow_http_resolve=allow_http,
                    )
                except Exception:
                    return
                if not row:
                    return
                song_id, sf, title, artist = row
                src = ""
                attrs = getattr(t, "attributes", None)
                if attrs:
                    src = getattr(attrs, "url", None) or ""
                out[i] = (song_id, sf, title, artist or default_artist, src)

        await asyncio.gather(*[_one(i, t) for i, t in enumerate(raw_tracks)])
        return [x for x in out if x is not None]

    async def fetch_qualities(
        self,
        raw_url: str,
        on_progress: Optional[Callable[[int, int, SongQualityInfo], None]] = None,
    ) -> list[SongQualityInfo]:
        from src.metadata import SongMetadata
        from src.quality import pick_best_codec
        from src.url import URLType

        await self.ensure_account_ready()

        url = await self.resolve_url(raw_url)
        if not url:
            raise ValueError(f"无法解析链接: {raw_url}")

        from src.utils import resolve_api_language

        lang = resolve_api_language(url.storefront)
        tracks: list[tuple[str, str, str, str, str]] = []
        playlist_ctx = None
        match url.type:
            case URLType.Song:
                meta = await it(self._WebAPI).get_song_info(url.id, url.storefront, lang)
                m = SongMetadata.parse_from_song_data(meta)
                tracks = [(url.id, url.storefront, m.title, m.artist, "")]
            case URLType.Album:
                album = await it(self._WebAPI).get_album_info(url.id, url.storefront, lang)
                album_artist = album.data[0].attributes.artistName or ""
                raw = album.data[0].relationships.tracks.data or []
                tracks = await self._resolve_track_list(
                    raw, url.storefront, default_artist=album_artist, allow_http=False,
                )
            case URLType.Playlist:
                pl = await it(self._WebAPI).get_playlist_info_and_tracks(url.id, url.storefront, lang)
                pl = await self._build_playlist_song_index(pl, url.storefront)
                playlist_ctx = pl
                raw = pl.data[0].relationships.tracks.data or []
                tracks = await self._resolve_track_list(raw, url.storefront, allow_http=False)
                if len(tracks) < len(raw):
                    got = {t[0] for t in tracks}
                    missing = []
                    for t in raw:
                        row = await it(self._WebAPI).resolve_catalog_track_entry(
                            t, url.storefront, allow_http_resolve=False,
                        )
                        if not row:
                            missing.append(t)
                    if missing:
                        extra = await self._resolve_track_list(
                            missing, url.storefront, allow_http=True,
                        )
                        for e in extra:
                            if e[0] not in got:
                                tracks.append(e)
                                got.add(e[0])
            case URLType.LibraryPlaylist:
                music_token = await self._ensure_music_token()
                pl = await it(self._WebAPI).get_library_playlist_info_and_tracks(
                    url.id, music_token, lang,
                )
                pl = await self._build_playlist_song_index(pl, url.storefront)
                playlist_ctx = pl
                raw = pl.data[0].relationships.tracks.data or []
                tracks = await self._resolve_track_list(raw, url.storefront, allow_http=True)
            case URLType.LibraryAlbum:
                music_token = await self._ensure_music_token()
                album = await it(self._WebAPI).get_library_album(url.id, lang)
                album_tracks = await it(self._WebAPI).get_library_album_tracks(url.id, lang)
                album_name = album.data[0].attributes.artistName or ""
                tracks = await self._resolve_track_list(
                    album_tracks, url.storefront, default_artist=album_name, allow_http=True,
                )
            case URLType.LibrarySong:
                music_token = await self._ensure_music_token()
                catalog_id, title = await it(self._WebAPI).resolve_library_song(url.id, lang)
                meta = await it(self._WebAPI).get_song_info(catalog_id, url.storefront, lang)
                m = SongMetadata.parse_from_song_data(meta)
                tracks = [(catalog_id, url.storefront, title or m.title, m.artist, "")]
            case URLType.Artist:
                raise ValueError("艺人链接不支持音质查询，请直接下载")
            case _:
                raise ValueError(f"不支持的链接类型: {url.type}")

        total = len(tracks)
        if total == 0:
            return []
        self._last_playlist_ctx = playlist_ctx

        results_slot: list[Optional[SongQualityInfo]] = [None] * total
        done_count = 0
        progress_lock = asyncio.Lock()
        worker_n = max(1, min(self._quality_concurrency, total))
        sem = asyncio.Semaphore(worker_n)
        # Emit start so UI can reset ETA before first song finishes
        if on_progress:
            try:
                on_progress(0, total, SongQualityInfo(
                    song_id="", title="", artist="", storefront="",
                ))
            except Exception:
                pass

        async def _song_quality(
            index: int, song_id: str, storefront: str, title: str, artist: str, source_url: str,
        ):
            nonlocal done_count
            async with sem:
                info = SongQualityInfo(
                    song_id=song_id, title=title, artist=artist, storefront=storefront,
                )
                try:
                    info.qualities, info.song_id, info.storefront = await self._fetch_qualities_for_song(
                        song_id, storefront, lang, source_url=source_url, fast_probe=True,
                    )
                    info.best_codec = pick_best_codec(info.qualities) if info.qualities else None
                except Exception as e:
                    info.error = str(e)
                results_slot[index] = info
                async with progress_lock:
                    done_count += 1
                    if on_progress:
                        on_progress(done_count, total, info)
                if info.error:
                    err_lower = info.error.lower()
                    if any(
                        marker in err_lower
                        for marker in (
                            "no available instance", "conn read", "eof",
                            "dial timeout", "unavailable",
                        )
                    ):
                        await asyncio.sleep(0.4)

        await asyncio.gather(*[
            _song_quality(i, sid, sf, title, artist, src)
            for i, (sid, sf, title, artist, src) in enumerate(tracks)
        ])
        return [r for r in results_slot if r is not None]

    async def _resolve_song_id_from_url(self, source_url: str) -> Optional[tuple[str, str]]:
        from src.url import AppleMusicURL, URLType
        from src.utils import parse_song_from_apple_url

        if not source_url:
            return None
        via_local = parse_song_from_apple_url(source_url)
        if via_local:
            return via_local
        try:
            real_url = await it(self._WebAPI).get_real_url(source_url)
            via_real = parse_song_from_apple_url(real_url)
            if via_real:
                return via_real
            am_url = AppleMusicURL.parse_url(real_url)
            if am_url and am_url.type == URLType.Song:
                return am_url.id, am_url.storefront
        except Exception:
            return None
        return None

    async def _catalog_song_exists(self, song_id: str, storefront: str, *, fast: bool = False) -> bool:
        """Existence precheck. fast=True: only primary storefront (for quality probe)."""
        cache_key = f"{'fast' if fast else 'full'}:{storefront}:{song_id}"
        if cache_key in self._existence_cache:
            return self._existence_cache[cache_key]
        if fast:
            try:
                exists = await it(self._WebAPI).song_exist(song_id, storefront)
            except Exception:
                # Optimistic: let m3u8 decide (avoids multi-region latency)
                exists = True
            self._existence_cache[cache_key] = exists
            return exists
        from src.utils import check_song_existence
        exists = await check_song_existence(song_id, storefront)
        self._existence_cache[cache_key] = exists
        return exists

    async def _catalog_quality_manifest(
        self, song_id: str, storefront: str, lang: str,
    ) -> Optional[str]:
        """Return Catalog enhanced HLS, used as a non-destructive playability gate."""
        cache_key = f"{storefront}:{song_id}"
        if cache_key in self._catalog_hls_cache:
            return self._catalog_hls_cache[cache_key]
        metadata = await it(self._WebAPI).get_song_info(song_id, storefront, lang)
        attributes = getattr(metadata, "attributes", None) if metadata else None
        assets = getattr(attributes, "extendedAssetUrls", None) if attributes else None
        manifest = (getattr(assets, "enhancedHls", None) or "").strip() if assets else ""
        result = manifest or None
        self._catalog_hls_cache[cache_key] = result
        return result

    async def _fetch_qualities_for_song(
        self,
        song_id: str,
        storefront: str,
        lang: str,
        source_url: str = "",
        *,
        fast_probe: bool = False,
    ) -> tuple[list, str, str]:
        from src.grpc.manager import WrapperManagerException, is_permanent_m3u8_error
        from src.quality import get_available_audio_quality
        from src.utils import parse_song_from_apple_url

        # Local reparse only — no HTTP redirect during bulk probe
        if source_url:
            via = parse_song_from_apple_url(source_url)
            if via:
                reparsed_id, reparsed_sf = via
                if reparsed_id != song_id or reparsed_sf.upper() != storefront.upper():
                    song_id, storefront = reparsed_id, reparsed_sf
            elif not fast_probe:
                reparsed = await self._resolve_song_id_from_url(source_url)
                if reparsed:
                    reparsed_id, reparsed_sf = reparsed
                    if reparsed_id != song_id or reparsed_sf.upper() != storefront.upper():
                        song_id, storefront = reparsed_id, reparsed_sf

        cache_key = f"{storefront}:{song_id}"
        if cache_key in self._quality_cache:
            return self._quality_cache[cache_key], song_id, storefront
        if cache_key in self._quality_negative_cache:
            raise WrapperManagerException(self._quality_negative_cache[cache_key])

        if not await self._catalog_song_exists(song_id, storefront, fast=fast_probe):
            msg = f"Song not found on Apple Music (storefront={storefront}, id={song_id})"
            self._quality_negative_cache[cache_key] = msg
            raise WrapperManagerException(msg)

        catalog_manifest = await self._catalog_quality_manifest(song_id, storefront, lang)
        if not catalog_manifest:
            msg = f"Enhanced HLS unavailable (storefront={storefront}, id={song_id})"
            self._quality_negative_cache[cache_key] = msg
            raise WrapperManagerException(msg)

        probe_lock = self._quality_probe_lock
        if probe_lock is None:
            probe_lock = asyncio.Lock()
            self._quality_probe_lock = probe_lock
        async with probe_lock:
            if cache_key in self._quality_cache:
                return self._quality_cache[cache_key], song_id, storefront
            wm = it(self._WrapperManager)
            try:
                m3u8_url = await wm.m3u8(song_id, probe=True)
            except Exception as e:
                err_text = str(getattr(e, "msg", None) or e)
                if is_permanent_m3u8_error(err_text):
                    self._quality_negative_cache[cache_key] = err_text
                if not await self._quality_probe_service_is_healthy(wm, song_id):
                    try:
                        await self._restart_wrapper_after_quality_probe_failure()
                    except Exception as recovery_error:
                        raise WrapperManagerException(
                            f"{err_text}; wrapper-manager recovery failed: {recovery_error}",
                        ) from e
                raise
            self._quality_health_song_id = song_id
        qualities = await get_available_audio_quality(m3u8_url)
        self._quality_cache[cache_key] = qualities
        return qualities, song_id, storefront

    async def _build_path_context(self, url, lang: str, playlist_info=None):
        from src.types import DownloadPathContext
        from src.url import URLType

        match url.type:
            case URLType.Album:
                album = await it(self._WebAPI).get_album_info(url.id, url.storefront, lang)
                return DownloadPathContext(
                    kind="album",
                    container_name=album.data[0].attributes.name or "Album",
                )
            case URLType.LibraryAlbum:
                music_token = await self._ensure_music_token()
                album = await it(self._WebAPI).get_library_album(url.id, lang)
                return DownloadPathContext(
                    kind="library_album",
                    container_name=album.data[0].attributes.name or "Album",
                )
            case URLType.Playlist:
                if playlist_info and playlist_info.data:
                    return DownloadPathContext(
                        kind="playlist",
                        container_name=playlist_info.data[0].attributes.name or "Playlist",
                    )
            case URLType.LibraryPlaylist:
                if playlist_info and playlist_info.data:
                    return DownloadPathContext(
                        kind="library_playlist",
                        container_name=playlist_info.data[0].attributes.name or "Playlist",
                    )
            case URLType.Song | URLType.LibrarySong:
                return None
            case URLType.Artist:
                artist = await it(self._WebAPI).get_artist_info(url.id, url.storefront, lang)
                return DownloadPathContext(
                    kind="song",
                    container_name="",
                    parent_container=artist.data[0].attributes.name or "Artist",
                )
        return None

    async def _get_playlist_context(self, url, lang: str):
        from src.url import URLType

        if getattr(self, "_last_playlist_ctx", None) is not None:
            cached = self._last_playlist_ctx
            self._last_playlist_ctx = None
            return cached

        match url.type:
            case URLType.Playlist:
                pl = await it(self._WebAPI).get_playlist_info_and_tracks(url.id, url.storefront, lang)
                return await self._build_playlist_song_index(pl, url.storefront)
            case URLType.LibraryPlaylist:
                music_token = await self._ensure_music_token()
                pl = await it(self._WebAPI).get_library_playlist_info_and_tracks(
                    url.id, music_token, lang,
                )
                return await self._build_playlist_song_index(pl, url.storefront)
        return None

    async def download_with_best_codec_per_song(
        self,
        raw_url: str,
        quality_results: list[SongQualityInfo],
        force: bool = False,
        language: Optional[str] = None,
        fixed_codec: Optional[str] = None,
    ) -> tuple[str, list[str], int, dict[str, str]]:
        from src.flags import Flags
        from src.quality import pick_best_codec
        from src.types import DownloadPathContext
        from src.url import Song, URLType

        url = await self.resolve_url(raw_url)
        if not url:
            raise ValueError(f"无法解析链接: {raw_url}")
        if not quality_results:
            raise ValueError("没有音质查询结果，请先点击「查询」")

        from src.utils import resolve_api_language

        lang = resolve_api_language(url.storefront, language or "")
        playlist_info = await self._get_playlist_context(url, lang)
        path_context = await self._build_path_context(url, lang, playlist_info)

        from src.task import Status

        await self.ensure_account_ready()

        if self._needs_kernel_restart:
            raise ValueError("内核状态异常，请关闭软件后重新启动再下载。")

        self._batch_cancelled = False
        if self.ripper:
            self.ripper.clear_cancel()
            self.ripper.download_manager.clear_finished_snapshots()

        warnings: list[str] = []
        downloaded = 0
        per_song_status: dict[str, str] = {}
        save_root = self.resolve_download_root()

        # Build work list
        work: list[tuple] = []
        for info in quality_results:
            if info.error:
                warnings.append(f"{info.artist} - {info.title}: {info.error}")
                per_song_status[info.song_id] = Status.FAILED.value
                continue
            codec = fixed_codec or info.best_codec or pick_best_codec(info.qualities)
            if not codec:
                warnings.append(f"{info.artist} - {info.title}: 无可用编码")
                per_song_status[info.song_id] = Status.FAILED.value
                continue
            if fixed_codec and info.qualities:
                available = {q.codec for q in info.qualities}
                if fixed_codec not in available:
                    warnings.append(
                        f"{info.artist} - {info.title}: 不提供编码 {fixed_codec}",
                    )
                    per_song_status[info.song_id] = Status.FAILED.value
                    continue
            song = Song(
                id=info.song_id,
                storefront=info.storefront or url.storefront,
                url="",
                type=URLType.Song,
            )
            song_ctx = path_context
            if url.type in (URLType.Song, URLType.LibrarySong):
                song_ctx = DownloadPathContext(
                    kind="library_song" if url.type == URLType.LibrarySong else "song",
                    container_name=info.title or "Unknown",
                )
            elif url.type == URLType.Artist:
                song_ctx = DownloadPathContext(
                    kind="song",
                    container_name="",
                    parent_container=(path_context.parent_container if path_context else "Artist"),
                )
            song_lang = resolve_api_language(info.storefront or url.storefront, language or "")
            song_flags = Flags(force_save=force, language=song_lang)
            work.append((info, song, codec, song_flags, song_ctx))

        n = len(work)
        # Interleaved per song: download → decrypt → next song.
        # (Two-phase "download all then decrypt all" held many staged tasks /
        #  raw buffers and was unstable on batch; classic path is more reliable.)
        # Progress units: 2 per song (download half + decrypt half) for ETA smoothness.
        self._emit_progress(f"开始批量下载（交叉进行，共 {n} 首）…")
        self._batch_progress_units_total = max(1, n * 2)
        self._batch_progress_units_done = 0.0

        for i, (info, song, codec, song_flags, song_ctx) in enumerate(work):
            if self._batch_cancelled:
                warnings.append("批量下载已取消")
                for info2, *_ in work[i:]:
                    if info2.song_id not in per_song_status:
                        per_song_status[info2.song_id] = Status.FAILED.value
                        self._emit_song_status(info2.song_id, Status.FAILED, "已取消")
                self._batch_progress_units_done = float(self._batch_progress_units_total)
                break

            self._emit_song_status(info.song_id, Status.DOWNLOADING)
            self._emit_progress(f"[{i + 1}/{n}] 下载+解密: {info.title}")
            # Mid-song: mark first unit in progress for ETA (download side)
            self._batch_progress_units_done = float(i * 2) + 0.15

            try:
                status, err = await self._rip_song_with_recovery(
                    song, codec, song_flags, playlist_info, song_ctx,
                )
                self._emit_song_status(info.song_id, status, err)
                per_song_status[info.song_id] = status.value
                if status == Status.DONE:
                    downloaded += 1
                else:
                    warnings.append(
                        f"{info.artist} - {info.title}: {err or status.value}",
                    )
            except Exception as e:
                self._emit_song_status(info.song_id, Status.FAILED, str(e))
                per_song_status[info.song_id] = Status.FAILED.value
                warnings.append(f"{info.artist} - {info.title}: {e}")

            # Song fully finished (success or fail) → both units consumed
            self._batch_progress_units_done = float((i + 1) * 2)

            # Light spacing only — avoid long inter-song gaps
            await asyncio.sleep(0.06)
            if (i + 1) % 8 == 0:
                try:
                    await self._ensure_kernel_healthy()
                except Exception:
                    pass

        self._batch_progress_units_done = float(self._batch_progress_units_total)

        if downloaded == 0:
            detail = "\n".join(warnings[:8]) if warnings else "未知错误"
            raise ValueError(f"没有曲目成功下载:\n{detail}")
        return save_root, warnings, downloaded, per_song_status

    async def download(
        self,
        raw_url: str,
        codec: str,
        force: bool = False,
        language: Optional[str] = None,
        include_participate: bool = False,
    ) -> str:
        from src.flags import Flags
        from src.url import URLType

        await self.ensure_account_ready()

        url = await self.resolve_url(raw_url)
        if not url:
            raise ValueError(f"无法解析链接: {raw_url}")
        from src.utils import resolve_api_language

        lang = resolve_api_language(url.storefront, language or "")
        flags = Flags(force_save=force, language=lang, include_participate_in_works=include_participate)
        root = self.resolve_download_root()
        match url.type:
            case URLType.Song:
                await self.ripper.rip_song(url, codec, flags)
            case URLType.Album:
                await self.ripper.rip_album(url, codec, flags)
            case URLType.Artist:
                await self.ripper.rip_artist(url, codec, flags)
            case URLType.Playlist:
                await self.ripper.rip_playlist(url, codec, flags)
            case URLType.LibraryPlaylist:
                await self.ripper.rip_library_playlist(url, codec, flags)
            case URLType.LibraryAlbum:
                await self.ripper.rip_library_album(url, codec, flags)
            case URLType.LibrarySong:
                await self.ripper.rip_library_song(url, codec, flags)
            case _:
                raise ValueError(f"不支持的链接类型: {url.type}")
        return root

    async def prepare_preview_audio(
        self,
        song_id: str,
        storefront: str,
        codec: Optional[str] = None,
        title: str = "",
        artist: str = "",
    ) -> str:
        """Download+decrypt one song for in-app audition at best available quality.

        True DRM stream play is impossible; we rip once, then play locally.
        Converts via ffmpeg when MCI cannot open ALAC/raw codecs.
        """
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        from src.flags import Flags
        from src.quality import pick_best_codec
        from src.types import DownloadPathContext
        from src.url import Song, URLType
        from src.utils import resolve_api_language

        await self.ensure_account_ready()
        sf = (storefront or "us").strip() or "us"
        lang = resolve_api_language(sf)
        use_codec = codec
        if not use_codec:
            for key, quals in self._quality_cache.items():
                if key.endswith(f":{song_id}") or key.startswith(f"{sf}:{song_id}"):
                    use_codec = pick_best_codec(quals)
                    break
        use_codec = use_codec or "alac"

        tmp_root = Path(tempfile.gettempdir()) / "amd_preview"
        tmp_root.mkdir(parents=True, exist_ok=True)
        cached = tmp_root / f"{song_id}_play.wav"
        if cached.is_file() and cached.stat().st_size > 1024:
            return str(cached)
        for alt in tmp_root.glob(f"{song_id}.*"):
            if alt.is_file() and alt.stat().st_size > 1024 and alt.suffix.lower() in (
                ".wav", ".m4a", ".mp3", ".aac",
            ):
                return str(alt)

        ctx = DownloadPathContext(kind="song", container_name=f"_preview_{song_id}")
        song = Song(id=str(song_id), storefront=sf, url="", type=URLType.Song)
        flags = Flags(force_save=True, language=lang)
        status, err = await self._rip_song_with_recovery(song, use_codec, flags, None, ctx)
        if status.value != "DONE":
            raise RuntimeError(err or "试听准备失败")

        from src.utils import get_download_base_dir

        base = Path(get_download_base_dir())
        if not base.is_absolute():
            base = self.base_dir / base

        files: list[Path] = []
        preview_dirs = [
            p for p in base.rglob("*")
            if p.is_dir() and f"_preview_{song_id}" in p.name
        ]
        for d in preview_dirs:
            for ext in ("*.m4a", "*.m4p", "*.mp4", "*.ec3", "*.ac3"):
                files.extend(d.rglob(ext))
        if not files:
            # newest audio under base modified in last few minutes
            recent = []
            for ext in ("*.m4a", "*.mp4"):
                recent.extend(base.rglob(ext))
            recent = sorted(recent, key=lambda p: p.stat().st_mtime, reverse=True)[:8]
            files = recent
        if not files:
            raise FileNotFoundError("未找到试听文件，请确认下载目录可写")

        newest = max(files, key=lambda p: p.stat().st_mtime)
        raw_copy = tmp_root / f"{song_id}{newest.suffix}"
        try:
            shutil.copy2(newest, raw_copy)
            src_play = raw_copy
        except Exception:
            src_play = newest

        # Prefer WAV for MCI reliability (ALAC/Atmos often fail as mpegvideo)
        wav_out = tmp_root / f"{song_id}_play.wav"
        ffmpeg = self.base_dir / "deps" / "ffmpeg.exe"
        if not ffmpeg.is_file():
            ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
        try:
            cmd = [
                str(ffmpeg), "-y", "-v", "error",
                "-i", str(src_play),
                "-map", "0:a:0",
                "-c:a", "pcm_s16le",
                "-ac", "2",
                str(wav_out),
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=600)
            if r.returncode == 0 and wav_out.is_file() and wav_out.stat().st_size > 1024:
                return str(wav_out)
        except Exception:
            pass
        return str(src_play)

    async def get_config_dict(self) -> dict[str, Any]:
        self._load_core()
        cfg = it(self._Config)
        return {
            "dirPathFormat": cfg.download.dirPathFormat,
            "playlistDirPathFormat": cfg.download.playlistDirPathFormat,
            "songNameFormat": cfg.download.songNameFormat,
            "proxy": cfg.download.proxy,
            "appleCDNIP": cfg.download.appleCDNIP,
            "parallelNum": cfg.download.parallelNum,
            "maxRunningTasks": cfg.download.maxRunningTasks,
            "codecAlternative": cfg.download.codecAlternative,
            "saveLyrics": cfg.download.saveLyrics,
            "saveCover": cfg.download.saveCover,
            "failedSongNotPassIntegrityCheck": cfg.download.failedSongNotPassIntegrityCheck,
            "language": cfg.region.language,
            "memoryMB": cfg.localInstance.memorySize.replace("M", ""),
            "hw_accel": self._detect_hw_accel().summary(),
            "hw_accel_detail": self._detect_hw_accel().message,
            "hw_accel_display": self._detect_hw_accel().display_text(),
            "download_root": self.resolve_download_root(),
        }

    async def apply_config(self, updates: dict[str, Any]):
        self._load_core()
        cfg = it(self._Config)
        cfg.download.dirPathFormat = (updates["dirPathFormat"] or "").strip() or "downloads"
        cfg.download.playlistDirPathFormat = (updates["playlistDirPathFormat"] or "").strip() or "downloads"
        cfg.download.songNameFormat = (updates["songNameFormat"] or "").strip() or "{artist} - {title}"
        cfg.download.proxy = (updates["proxy"] or "").strip()
        cfg.download.appleCDNIP = updates["appleCDNIP"]
        cfg.download.parallelNum = int(updates["parallelNum"])
        cfg.download.maxRunningTasks = int(updates["maxRunningTasks"])
        cfg.download.codecAlternative = bool(updates["codecAlternative"])
        cfg.download.saveLyrics = bool(updates["saveLyrics"])
        cfg.download.saveCover = bool(updates["saveCover"])
        cfg.download.failedSongNotPassIntegrityCheck = bool(updates["failedSongNotPassIntegrityCheck"])
        cfg.region.language = updates["language"]
        cfg.localInstance.memorySize = f"{updates['memoryMB']}M"
        from src.hwaccel import apply_hardware_acceleration
        from src.qemu import build_wm_args

        # Hardware acceleration is fully automatic — never take a user flag.
        self._hw_accel_info = None
        apply_hardware_acceleration(cfg, self.base_dir)
        cfg.localInstance.enable = True
        cfg.instance.url = "127.0.0.1:32767"
        cfg.instance.secure = False
        # Keep VM startArgs in sync with host proxy + -mirror
        cfg.localInstance.startArgs = build_wm_args(
            cfg.localInstance.startArgs, cfg.download.proxy,
        )
        self._ensure_absolute_download_paths(cfg)
        cfg.save_to_file(str(self.base_dir / "config.toml"))
        # Apply proxy immediately to live WebAPI client
        try:
            it(self._WebAPI).set_proxy(cfg.download.proxy)
        except Exception:
            pass

    async def poweroff_kernel(self):
        if self._core_loaded:
            try:
                await it(self._WrapperManager).stop_decrypt_stream()
            except Exception:
                pass
        if self.local_instance is not None:
            try:
                await self.local_instance.terminate(poweroff=True)
            except Exception:
                pass
        try:
            from src.qemu import QemuInstance
            await QemuInstance._force_kill_qemu_process()
        except Exception:
            pass
        self._kernel_ready = False

    async def poweroff_vm(self):
        await self.poweroff_kernel()

    def shutdown(self, poweroff_kernel: bool = True):
        """Stop backend loop and fully tear down local QEMU."""
        from src.qemu import QemuInstance

        if poweroff_kernel:
            try:
                if self._loop and self._loop.is_running() and self._core_loaded:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.poweroff_kernel(), self._loop,
                        ).result(timeout=20)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                QemuInstance.force_kill_qemu_sync()
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
