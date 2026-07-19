import asyncio
import base64
import json
import os
import subprocess
import zipfile
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import httpx
from creart import it
from tenacity import retry, retry_if_exception_type, wait_random_exponential, stop_after_attempt, before_sleep_log

from src.config import Config
from src.logger import GlobalLogger
from src.utils import hidden_subprocess_kwargs

def _qemu_binary(base_dir: Path) -> str:
    name = "qemu-system-x86_64.exe" if os.name == "nt" else "qemu-system-x86_64"
    return str((base_dir / "deps" / name).resolve())


async def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def build_wm_args(start_args: str | None = None, proxy: str | None = None) -> str:
    """Normalize wrapper-manager CLI flags for the local QEMU image.

    Important for CN networks:
    - ``-mirror`` uses a mirror when the manager downloads wrapper binaries
    - ``-proxy`` lets the VM reach the internet via the host's HTTP proxy
    Without these, gRPC may listen while ``Status.ready`` stays false forever.
    """
    raw = (start_args if start_args is not None else it(Config).localInstance.startArgs) or ""
    tokens = raw.split()
    # Drop empty tokens
    tokens = [t for t in tokens if t]

    def _has_flag(name: str) -> bool:
        return name in tokens

    def _set_option(flag: str, value: str):
        nonlocal tokens
        if flag in tokens:
            i = tokens.index(flag)
            # flag may be followed by value
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                tokens[i + 1] = value
            else:
                tokens.insert(i + 1, value)
        else:
            tokens.extend([flag, value])

    if not _has_flag("-host"):
        _set_option("-host", "0.0.0.0")
    if not _has_flag("-port"):
        _set_option("-port", "32767")
    # Always enable mirror for offline Windows package users (mostly CN)
    if not _has_flag("-mirror"):
        tokens.append("-mirror")
    if not _has_flag("-debug"):
        tokens.append("-debug")

    proxy_val = (proxy if proxy is not None else it(Config).download.proxy) or ""
    proxy_val = proxy_val.strip()
    if proxy_val:
        _set_option("-proxy", proxy_val)
    elif "-proxy" in tokens:
        # Remove stale -proxy and its value when host proxy cleared
        i = tokens.index("-proxy")
        drop = [i]
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
            drop.append(i + 1)
        tokens = [t for j, t in enumerate(tokens) if j not in drop]

    return " ".join(tokens)


def _qemu_smp_count() -> int:
    """vCPU count for local wrapper VM (balance speed vs host load)."""
    try:
        n = os.cpu_count() or 4
    except Exception:
        n = 4
    # Keep headroom for GUI + host; WHPX benefits from multi-vCPU decrypt load
    return max(2, min(8, n // 2 if n >= 8 else min(4, n)))


def build_qemu_arguments(base_dir: Path) -> list[str]:
    cfg = it(Config)
    qcow2 = (base_dir / "assets" / "wrapper-manager.qcow2").resolve()
    qemu = _qemu_binary(base_dir)
    smp = _qemu_smp_count()
    args = [
        qemu, "-machine", "q35",
        "-cpu", cfg.localInstance.cpuModel,
        "-smp", str(smp),
        "-m", cfg.localInstance.memorySize,
        "-hda", str(qcow2),
        "-device", "virtio-net-pci,netdev=net0",
        "-chardev", "socket,id=qga0,host=127.0.0.1,port=32766,server=on,wait=off",
        "-device", "virtio-serial-pci",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        "-netdev", "user,id=net0,hostfwd=tcp:127.0.0.1:32767-:32767",
    ]
    if cfg.localInstance.enableHardwareAcceleration and cfg.localInstance.hardwareAccelerator:
        args[1:1] = ["-accel", cfg.localInstance.hardwareAccelerator]
    if not cfg.localInstance.showWindow:
        args.extend(["-display", "none"])
    share = base_dir / "deps" / "share"
    if share.is_dir():
        for name in ("vgabios-stdvga.bin", "bios-256k.bin"):
            bios = share / name
            if bios.exists():
                args.extend(["-L", str(share.resolve())])
                break
    return args


class QGAException(Exception):
    msg: str

    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


class QemuCrashedException(Exception):
    msg: str

    def __init__(self, stdout: str, stderr: str):
        self.msg = stdout + "\n" + stderr


def _parse_file_handle(fp) -> int | None:
    if isinstance(fp, int):
        return fp
    if isinstance(fp, dict) and isinstance(fp.get("handle"), int):
        return fp["handle"]
    return None


class QGAClient:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @retry(retry=retry_if_exception_type(asyncio.TimeoutError),
           wait=wait_random_exponential(multiplier=1, max=it(Config).download.maxWaitTime),
           stop=stop_after_attempt(8), before_sleep=before_sleep_log(it(GlobalLogger).logger, "WARNING"))
    async def init(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", 32766)

    async def wait_for_guest(
        self,
        timeout_sec: float = 15,
        on_wait: Callable[[str], None] | None = None,
        alive_check: Callable[[], bool] | None = None,
    ):
        """Poll QGA until the guest agent responds (warm attach or cold boot)."""
        deadline = asyncio.get_running_loop().time() + timeout_sec
        cold_boot = timeout_sec > 30
        tick = 0
        while asyncio.get_running_loop().time() < deadline:
            if alive_check is not None and not alive_check():
                raise QemuCrashedException(
                    "",
                    "内核进程已意外退出。程序会自动使用可用加速方式；"
                    "若反复崩溃请结束 qemu-system-x86_64.exe 后重开。",
                )
            try:
                await self.init()
                await asyncio.wait_for(self.ping(), timeout=3.0)
                return
            except Exception:
                self.reader = None  # type: ignore[assignment]
                self.writer = None  # type: ignore[assignment]
                if on_wait and tick % 4 == 0:
                    if cold_boot:
                        on_wait("等待内核启动（约 1–2 分钟）...")
                    else:
                        on_wait("等待内核响应...")
                tick += 1
                await asyncio.sleep(0.5)
        raise TimeoutError("无法连接到内核 QGA (127.0.0.1:32766)")

    async def ping(self):
        return await self.send_cmd("guest-ping", {})

    async def send_cmd(self, command: str, arguments: dict):
        self.writer.write(json.dumps({"execute": command, "arguments": arguments}).encode())
        result = json.loads(await self.reader.readline())
        if result.get("error"):
            err = result.get("error")
            if isinstance(err, dict):
                raise QGAException(err.get("desc") or str(err))
            raise QGAException(str(err))
        return result.get("return")

    async def _close_handle(self, fp):
        handle = _parse_file_handle(fp)
        if handle is None:
            return
        try:
            await self.send_cmd("guest-file-close", {"handle": handle})
        except QGAException:
            pass

    async def read_file(self, path: str) -> str:
        try:
            fp = await self.send_cmd("guest-file-open", {"path": path})
        except QGAException:
            return ""
        handle = _parse_file_handle(fp)
        if handle is None:
            return ""
        try:
            raw_result = await self.send_cmd("guest-file-read", {"handle": handle, "count": 48000000})
            if not raw_result or "buf-b64" not in raw_result:
                return ""
            return base64.standard_b64decode(raw_result["buf-b64"]).decode()
        finally:
            await self._close_handle(handle)

    async def write_file(self, path: str, content: str):
        fp = await self.send_cmd("guest-file-open", {"path": path, "mode": "w"})
        handle = _parse_file_handle(fp)
        if handle is None:
            raise QGAException(f"无法打开文件: {path}")
        try:
            await self.send_cmd(
                "guest-file-write",
                {"handle": handle, "buf-b64": base64.standard_b64encode(content.encode()).decode()},
            )
        finally:
            await self._close_handle(handle)

    async def execute(self, path: str, args: list[str]):
        return await self.send_cmd("guest-exec", {"path": path, "arg": args})


class QemuInstance:
    proc = None
    client = QGAClient()

    @staticmethod
    def _pids_listening_on_kernel_ports() -> list[int]:
        """Find PIDs that hold 127.0.0.1:32766/32767 (Windows)."""
        if os.name != "nt":
            return []
        pids: set[int] = set()
        try:
            r = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                timeout=8,
                **hidden_subprocess_kwargs(),
            )
            text = (r.stdout or b"").decode("utf-8", errors="replace")
        except Exception:
            return []
        for line in text.splitlines():
            #  TCP    127.0.0.1:32767    0.0.0.0:0    LISTENING    12345
            if "127.0.0.1:32766" not in line and "127.0.0.1:32767" not in line:
                continue
            if "LISTENING" not in line.upper() and "ESTABLISHED" not in line.upper():
                continue
            parts = line.split()
            if not parts:
                continue
            try:
                pid = int(parts[-1])
                if pid > 0:
                    pids.add(pid)
            except ValueError:
                continue
        return list(pids)

    @staticmethod
    def force_kill_qemu_sync() -> str:
        """Synchronously kill all local QEMU kernel processes. Safe to call on exit."""
        notes: list[str] = []
        if os.name == "nt":
            # 1) By image name (covers CREATE_NO_WINDOW / no-console qemu)
            try:
                r = subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", "qemu-system-x86_64.exe"],
                    capture_output=True,
                    timeout=15,
                    **hidden_subprocess_kwargs(),
                )
                raw = (r.stdout or b"") + (r.stderr or b"")
                out = raw.decode("utf-8", errors="replace") + raw.decode("gbk", errors="replace")
                if r.returncode == 0:
                    notes.append("已结束 qemu-system-x86_64.exe")
                elif (
                    "not found" in out.lower()
                    or "没有找到" in out
                    or "not running" in out.lower()
                    or r.returncode == 128
                ):
                    notes.append("未发现 qemu 进程（可能已退出）")
                else:
                    notes.append("taskkill 已执行")
            except Exception as exc:
                notes.append(f"taskkill 异常: {exc}")
            # 2) By port owner (orphan qemu holding 32766/32767)
            for pid in QemuInstance._pids_listening_on_kernel_ports():
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        **hidden_subprocess_kwargs(),
                    )
                    notes.append(f"结束占用端口进程 PID={pid}")
                except Exception:
                    pass
        else:
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", "qemu-system-x86_64"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                notes.append("已 pkill qemu")
            except Exception as exc:
                notes.append(str(exc))
        return "; ".join(notes) if notes else "清理完成"

    @staticmethod
    async def _force_kill_qemu_process():
        await asyncio.to_thread(QemuInstance.force_kill_qemu_sync)

    @staticmethod
    async def wait_ports_closed(timeout_sec: float = 30) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if not await _port_open("127.0.0.1", 32766, timeout=0.3):
                if not await _port_open("127.0.0.1", 32767, timeout=0.3):
                    return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    async def ensure_ports_free(on_wait: Callable[[str], None] | None = None) -> None:
        """Force-release kernel ports 32766/32767 before a fresh launch."""
        if not await _port_open("127.0.0.1", 32766, timeout=0.3):
            if not await _port_open("127.0.0.1", 32767, timeout=0.3):
                return
        if on_wait:
            on_wait("检测到残留内核端口，正在清理...")
        helper = QemuInstance()
        try:
            await helper.stop_existing_kernel(on_wait=on_wait)
        except Exception:
            pass
        if await _port_open("127.0.0.1", 32766, timeout=0.3) or await _port_open(
            "127.0.0.1", 32767, timeout=0.3,
        ):
            if on_wait:
                on_wait("残留端口仍未释放，正在强制结束 QEMU...")
            await QemuInstance._force_kill_qemu_process()
            await asyncio.sleep(2)
        if not await QemuInstance.wait_ports_closed(timeout_sec=20):
            raise RuntimeError(
                "无法释放内核端口 32766/32767。请在任务管理器中结束 qemu-system-x86_64.exe 后重试。",
            )

    async def stop_existing_kernel(self, on_wait: Callable[[str], None] | None = None):
        """Shut down any kernel left from a previous GUI session."""
        if not await _port_open("127.0.0.1", 32766, timeout=0.5):
            if not await _port_open("127.0.0.1", 32767, timeout=0.5):
                return
        if on_wait:
            on_wait("正在关闭残留内核...")
        helper = QemuInstance()
        try:
            await helper.client.wait_for_guest(timeout_sec=8, on_wait=on_wait)
            await helper.terminate(poweroff=True)
        except Exception:
            await self._force_kill_qemu_process()
        if not await self.wait_ports_closed(timeout_sec=20):
            await self._force_kill_qemu_process()
            await asyncio.sleep(2)
            await self.wait_ports_closed(timeout_sec=15)

    async def _read_wm_args_file(self) -> str:
        try:
            text = await self.client.read_file("/etc/wm-args")
            return (text or "").strip()
        except Exception:
            return ""

    async def _write_wm_args(self, *, force_log: bool = True) -> str:
        args = build_wm_args()
        # Persist normalized args back so next boots keep -mirror/-proxy
        try:
            cfg = it(Config)
            if (cfg.localInstance.startArgs or "").strip() != args:
                cfg.localInstance.startArgs = args
        except Exception:
            pass
        for _ in range(10):
            try:
                await self.client.write_file("/etc/wm-args", args)
                if force_log:
                    it(GlobalLogger).logger.info(f"Writing wrapper-manager args: {args}")
                return args
            except QGAException:
                await asyncio.sleep(0.5)
        raise QGAException("无法写入 /etc/wm-args，内核可能尚未完全启动")

    async def _ensure_wrapper_service(self, on_wait: Callable[[str], None] | None = None):
        """Start wrapper-manager once. Do NOT restart if already running.

        Restarting while ready=false often aborts in-progress wrapper downloads
        and causes a restart loop.
        """
        desired = build_wm_args()
        running = await self.service_ready()
        current_args = await self._read_wm_args_file() if running else ""

        if running and current_args.strip() == desired.strip():
            it(GlobalLogger).logger.info(
                "wrapper-manager already running with current args; leave it alone",
            )
            return

        if running and current_args.strip() != desired.strip():
            # Args changed (e.g. proxy added) — one intentional restart only
            it(GlobalLogger).logger.info(
                f"wrapper-manager args changed, restarting once: {desired}",
            )
            await self._write_wm_args()
            try:
                await self.client.execute("/sbin/rc-service", ["wrapper-manager", "restart"])
            except Exception:
                await self.client.execute("/sbin/rc-service", ["wrapper-manager", "start"])
        else:
            # Not running: write args and start (no restart loop)
            await self._write_wm_args()
            await self.client.execute("/sbin/rc-service", ["wrapper-manager", "start"])

        it(GlobalLogger).logger.info("Waiting for wrapper-manager service inside VM...")
        for i in range(120):
            if await self.service_ready():
                it(GlobalLogger).logger.info("wrapper-manager process is running inside VM")
                return
            if on_wait and i % 4 == 0:
                on_wait("等待内核内 wrapper-manager 进程启动...")
            await asyncio.sleep(0.25 if i < 20 else 0.5)
        raise TimeoutError("wrapper-manager 在内核内启动超时（120秒）")

    async def restart_wrapper_service(self, on_wait: Callable[[str], None] | None = None):
        """Explicit one-shot restart (not used on the normal ready-wait path)."""
        if on_wait:
            on_wait("正在重启 VM 内 wrapper-manager…")
        try:
            await self.client.init()
            await self._write_wm_args()
            await self.client.execute("/sbin/rc-service", ["wrapper-manager", "restart"])
        except Exception as exc:
            raise RuntimeError(f"无法重启 wrapper-manager: {exc}") from exc
        for i in range(80):
            if await self.service_ready():
                if on_wait:
                    on_wait("wrapper-manager 进程已重启")
                return
            if on_wait and i % 4 == 0:
                on_wait("等待 wrapper-manager 重启后监听端口...")
            await asyncio.sleep(0.5)
        raise TimeoutError("wrapper-manager 重启后端口仍未就绪")

    async def read_wrapper_log_tail(self, max_chars: int = 1500) -> str:
        try:
            await self.client.init()
            text = await self.client.read_file("/var/run/wrapper-manager/wrapper-manager.log")
            text = (text or "").strip()
            if not text:
                return ""
            return text[-max_chars:]
        except Exception:
            return ""

    async def _spawn_qemu(self, args: list[str], base: Path):
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(base),
            **hidden_subprocess_kwargs(),
        )

    async def _read_proc_output(self) -> str:
        if not self.proc:
            return ""
        chunks: list[str] = []
        try:
            if self.proc.stderr:
                err = await asyncio.wait_for(self.proc.stderr.read(), timeout=1.0)
                if err:
                    chunks.append(err.decode("utf-8", errors="replace"))
        except Exception:
            pass
        try:
            if self.proc.stdout:
                out = await asyncio.wait_for(self.proc.stdout.read(), timeout=0.5)
                if out:
                    chunks.append(out.decode("utf-8", errors="replace"))
        except Exception:
            pass
        return "\n".join(chunks).strip()

    async def launch_instance(
        self,
        loop: asyncio.AbstractEventLoop,
        base_dir: Path | None = None,
        on_wait: Callable[[str], None] | None = None,
    ):
        base = (base_dir or Path.cwd()).resolve()
        await self.ensure_ports_free(on_wait=on_wait)

        if not self.image_available(base):
            await self.get_instance_image()
        self.client = QGAClient()
        cfg = it(Config)
        used_hw = bool(
            cfg.localInstance.enableHardwareAcceleration
            and cfg.localInstance.hardwareAccelerator
        )
        args = build_qemu_arguments(base)
        it(GlobalLogger).logger.info(
            f"Starting local wrapper-manager (QEMU) -> 127.0.0.1:32767 | "
            f"accel={cfg.localInstance.hardwareAccelerator or 'tcg'} "
            f"cpu={cfg.localInstance.cpuModel} | image: {base / 'assets' / 'wrapper-manager.qcow2'}"
        )
        if on_wait:
            on_wait(
                f"启动内核 "
                f"({'WHPX' if used_hw else '软件模拟'})…",
            )
        await self._spawn_qemu(args, base)

        # Early crash detection (WHPX mis-config often dies within 1–2s)
        early_dead = False
        early_log = ""
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=2.5)
            early_dead = True
            early_log = await self._read_proc_output()
        except asyncio.TimeoutError:
            early_dead = False

        if early_dead and used_hw:
            it(GlobalLogger).logger.warning(
                f"QEMU exited early under WHPX (code={self.proc.returncode}): {early_log[-400:]}",
            )
            if on_wait:
                on_wait("WHPX 启动失败，自动回退软件模拟并重试…")
            from src.hwaccel import disable_hardware_acceleration

            disable_hardware_acceleration(
                cfg,
                reason=early_log.splitlines()[-1] if early_log else f"exit={self.proc.returncode}",
            )
            try:
                cfg.save_to_file(str(base / "config.toml"))
            except Exception:
                pass
            await self._force_kill_qemu_process()
            await asyncio.sleep(1)
            await self.ensure_ports_free(on_wait=on_wait)
            args = build_qemu_arguments(base)
            it(GlobalLogger).logger.info(
                f"Retrying QEMU with software emulation | cpu={cfg.localInstance.cpuModel}",
            )
            await self._spawn_qemu(args, base)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2.5)
                log2 = await self._read_proc_output()
                raise QemuCrashedException(
                    "",
                    f"软件模拟启动也失败 (exit={self.proc.returncode})。\n{log2[-500:]}",
                )
            except asyncio.TimeoutError:
                pass  # running under TCG
        elif early_dead:
            log = early_log or await self._read_proc_output()
            raise QemuCrashedException(
                "",
                f"QEMU 进程已退出 (exit={self.proc.returncode})。\n{log[-500:]}",
            )

        it(GlobalLogger).logger.info("Waiting for wrapper-manager VM to boot...")
        try:
            await self.client.wait_for_guest(
                timeout_sec=180,
                on_wait=on_wait,
                alive_check=lambda: self.proc is not None and self.proc.returncode is None,
            )
        except QemuCrashedException:
            raise
        except TimeoutError as exc:
            if self.proc and self.proc.returncode is not None:
                log = await self._read_proc_output()
                # Late WHPX crash during boot → one TCG retry
                if used_hw and cfg.localInstance.enableHardwareAcceleration:
                    if on_wait:
                        on_wait("WHPX 运行中崩溃，回退软件模拟…")
                    from src.hwaccel import disable_hardware_acceleration
                    disable_hardware_acceleration(cfg, reason=log[-200:] if log else "boot crash")
                    try:
                        cfg.save_to_file(str(base / "config.toml"))
                    except Exception:
                        pass
                    await self._force_kill_qemu_process()
                    await asyncio.sleep(1)
                    await self.ensure_ports_free(on_wait=on_wait)
                    await self._spawn_qemu(build_qemu_arguments(base), base)
                    try:
                        await self.client.wait_for_guest(
                            timeout_sec=180,
                            on_wait=on_wait,
                            alive_check=lambda: self.proc is not None and self.proc.returncode is None,
                        )
                    except Exception as retry_exc:
                        raise QemuCrashedException(
                            "",
                            f"软件模拟重试仍失败: {retry_exc}\n{log[-400:]}",
                        ) from retry_exc
                else:
                    hint = (
                        f"QEMU 进程已退出 (exit={self.proc.returncode})。"
                        f"{' ' + log[-300:] if log else ''}"
                    )
                    raise QemuCrashedException("", hint) from exc
            else:
                raise TimeoutError(
                    "内核启动超时（180 秒）。软件模拟下冷启动约 1–2 分钟属正常。",
                ) from exc
        await self._ensure_wrapper_service(on_wait=on_wait)

    def qemu_running(self):
        if self.proc is None:
            return False
        return self.proc.returncode is None

    async def service_ready(self) -> bool:
        if not await _port_open("127.0.0.1", 32767, timeout=0.5):
            return False
        try:
            content = await self.client.read_file("/var/run/wrapper-manager/wrapper-manager.pid")
            return bool(content and content.strip())
        except (QGAException, OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return True

    async def instance_running(self) -> bool:
        try:
            content = await self.client.read_file("/var/run/wrapper-manager/wrapper-manager.pid")
            return bool(content and content.strip())
        except (QGAException, OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return False

    async def terminate(self, poweroff: bool = True):
        """Graceful guest poweroff when possible, always force-kill host QEMU afterwards."""
        if not poweroff:
            return
        # Soft poweroff inside VM (best-effort, short timeout)
        try:
            if await _port_open("127.0.0.1", 32766, timeout=0.4):
                try:
                    await asyncio.wait_for(self.client.init(), timeout=3)
                    await asyncio.wait_for(
                        self.client.execute("/sbin/poweroff", []), timeout=3,
                    )
                    await asyncio.sleep(1.2)
                except Exception:
                    pass
        except Exception:
            pass
        # Kill tracked child first
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3)
                except Exception:
                    pass
        # Always sweep residual qemu / port holders (GUI exit must be complete)
        await self._force_kill_qemu_process()
        await self.wait_ports_closed(timeout_sec=8)
        self.proc = None
        self.client = QGAClient()

    async def logs(self):
        return await self.client.read_file("/var/run/wrapper-manager/wrapper-manager.log")

    async def get_instance_image(self):
        if os.environ.get("AMD_OFFLINE") == "1":
            raise FileNotFoundError(
                "本地 wrapper-manager 镜像缺失 (assets/wrapper-manager.qcow2)。"
                "请使用完整离线安装包重新安装。"
            )
        it(GlobalLogger).logger.warning("The wrapper-manager image does not exist. Downloading...")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                "https://nightly.link/WorldObservationLog/wrapper-manager/workflows/wrapper-manager-image/main/wrapper-manager-image.zip")
            with zipfile.ZipFile(BytesIO(resp.content), "r") as f:
                f.extractall("assets/")

    def image_available(self, base_dir: Path | None = None):
        base = base_dir or Path.cwd()
        return (base / "assets" / "wrapper-manager.qcow2").exists()
