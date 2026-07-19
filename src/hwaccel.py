"""Detect and auto-apply QEMU hardware acceleration (no user toggle)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.utils import hidden_subprocess_kwargs


def _qemu_binary(base_dir: Path) -> str:
    name = "qemu-system-x86_64.exe" if os.name == "nt" else "qemu-system-x86_64"
    return str((base_dir / "deps" / name).resolve())


def _run_ps(command: str, timeout: float = 25) -> str:
    if os.name != "nt":
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
        raw = (r.stdout or b"") + b"\n" + (r.stderr or b"")
        return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _feature_state(feature_name: str) -> str:
    out = _run_ps(
        f"try {{ (Get-WindowsOptionalFeature -Online -FeatureName {feature_name} "
        f"-ErrorAction Stop).State }} catch {{ "
        f"try {{ (Get-WindowsOptionalFeature -FeatureName {feature_name} "
        f"-ErrorAction Stop).State }} catch {{ 'Unknown' }} }}",
    )
    for line in out.splitlines():
        line = line.strip()
        if line in ("Enabled", "Disabled", "EnablePending", "DisablePending", "Unknown"):
            return line
    if "Enabled" in out:
        return "Enabled"
    if "Disabled" in out:
        return "Disabled"
    return "Unknown"


def _hypervisor_present() -> bool | None:
    if os.name != "nt":
        return False
    out = _run_ps(
        "try { (Get-CimInstance Win32_ComputerSystem).HypervisorPresent } catch { '' }",
        timeout=15,
    )
    if re.search(r"\bTrue\b", out, re.I):
        return True
    if re.search(r"\bFalse\b", out, re.I):
        return False
    return None


def _firmware_virt_enabled() -> bool | None:
    out = _run_ps(
        "try { (Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled } catch { '' }",
        timeout=15,
    )
    if re.search(r"\bTrue\b", out, re.I):
        return True
    if re.search(r"\bFalse\b", out, re.I):
        return False
    return None


def _vmware_or_vbox_hint() -> str:
    out = _run_ps(
        "Get-CimInstance Win32_NetworkAdapter -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Name",
        timeout=15,
    )
    lower = out.lower()
    notes = []
    if "vmware" in lower:
        notes.append("检测到 VMware 网卡")
    if "virtualbox" in lower or "vbox" in lower:
        notes.append("检测到 VirtualBox")
    return "；".join(notes)


def _qemu_accel_names(base_dir: Path) -> set[str]:
    qemu = _qemu_binary(base_dir)
    if not Path(qemu).exists():
        return set()
    try:
        r = subprocess.run(
            [qemu, "-accel", "help"],
            capture_output=True,
            timeout=12,
            **hidden_subprocess_kwargs(),
        )
        text = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", errors="replace")
    except Exception:
        return set()
    names: set[str] = set()
    for line in text.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token and token.isascii() and token.replace("-", "").replace("_", "").isalnum():
            names.add(token.lower())
    return names


_WHPX_HARD_FAIL = (
    "failed to initialize whpx",
    "no accelerator found",
    "whpx is not available",
    "could not load library",
    "unknown accelerator",
    "unexpected vp exit",
    "injection failed",
)


def _classify_whpx_output(text: str, returncode: int | None, timed_out: bool) -> tuple[str, str]:
    """Return (ok|fail|unknown, detail)."""
    lower = (text or "").lower()

    # Hard failures first — "operational" can still appear before crash
    for m in _WHPX_HARD_FAIL:
        if m in lower:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            # Prefer the most specific line
            for ln in reversed(lines):
                ll = ln.lower()
                if any(x in ll for x in _WHPX_HARD_FAIL):
                    return "fail", ln[:200]
            return "fail", lines[-1][:200] if lines else m

    if "accelerator is operational" in lower:
        # Only trust if process stayed alive (timeout) or clean exit without hard fail
        if timed_out:
            return "ok", "WHPX 运行稳定（探针存活）"
        if returncode == 0:
            # Exit 0 without disk often still means init only — treat as weak ok
            return "ok", "WHPX 报告 operational"
        return "unknown", f"operational 但 exit={returncode}"

    if timed_out and "failed to initialize" not in lower:
        return "ok", "探针超时且无初始化错误（视为可用）"

    if returncode == 0:
        return "ok", "QEMU 退出码 0"

    return "unknown", (text.strip().splitlines()[-1] if text.strip() else f"exit={returncode}")[:200]


def _run_qemu_probe(qemu: str, args: list[str], timeout: float = 6.0) -> tuple[str, str, str]:
    """Run probe; return (status, detail, full_output)."""
    try:
        proc = subprocess.Popen(
            [qemu, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            timed_out = False
            code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=3)
            except Exception:
                stdout, stderr = b"", b""
            code = None
        text = (stdout or b"").decode("utf-8", errors="replace") + "\n" + (
            (stderr or b"").decode("utf-8", errors="replace")
        )
        status, detail = _classify_whpx_output(text, code, timed_out)
        return status, detail, text
    except Exception as exc:
        return "fail", str(exc), ""


# Prefer stable Windows recipes first. `cpu max` + plain `whpx` often crashes
# real boots with "Unexpected VP exit code 4" on this package.
_WHPX_PROBE_MATRIX: list[tuple[str, str, list[str]]] = [
    (
        "whpx,kernel-irqchip=off",
        "qemu64-v1",
        [
            "-machine", "q35", "-cpu", "qemu64-v1", "-m", "256M",
            "-accel", "whpx,kernel-irqchip=off",
            "-display", "none", "-nodefaults", "-nographic",
        ],
    ),
    (
        "whpx,kernel-irqchip=off",
        "qemu64",
        [
            "-machine", "q35", "-cpu", "qemu64", "-m", "256M",
            "-accel", "whpx,kernel-irqchip=off",
            "-display", "none", "-nodefaults", "-nographic",
        ],
    ),
    (
        "whpx",
        "qemu64",
        [
            "-machine", "q35", "-cpu", "qemu64", "-m", "256M",
            "-accel", "whpx",
            "-display", "none", "-nodefaults", "-nographic",
        ],
    ),
    (
        "whpx,kernel-irqchip=off",
        "qemu64-v1",
        [
            "-machine", "none", "-accel", "whpx,kernel-irqchip=off",
            "-display", "none", "-nodefaults",
        ],
    ),
]


def _probe_boot_with_disk(
    base_dir: Path, accel: str, cpu: str, hold_sec: float = 4.0,
) -> tuple[bool, str]:
    """Stronger check: boot real qcow2 briefly; must stay alive."""
    qemu = _qemu_binary(base_dir)
    qcow2 = base_dir / "assets" / "wrapper-manager.qcow2"
    if not qcow2.is_file():
        return True, "无镜像，跳过实机探针"  # don't block soft probe
    share = base_dir / "deps" / "share"
    # Unique guest agent port to avoid clashing with a running instance
    qga_port = 32991
    args = [
        qemu,
        "-accel", accel,
        "-machine", "q35",
        "-cpu", cpu,
        "-m", "512M",
        "-hda", str(qcow2.resolve()),
        "-device", "virtio-net-pci,netdev=net0",
        "-chardev", f"socket,id=qga0,host=127.0.0.1,port={qga_port},server=on,wait=off",
        "-device", "virtio-serial-pci",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        "-netdev", "user,id=net0",
        "-display", "none",
    ]
    if share.is_dir():
        args.extend(["-L", str(share.resolve())])

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
    except Exception as exc:
        return False, f"无法启动探针进程: {exc}"

    time.sleep(hold_sec)
    code = proc.poll()
    if code is None:
        # Still running → good
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        return True, f"实机探针存活 {hold_sec:.0f}s"

    # Exited early → capture why
    try:
        stdout, stderr = proc.communicate(timeout=2)
    except Exception:
        stdout, stderr = b"", b""
    text = ((stdout or b"") + b"\n" + (stderr or b"")).decode("utf-8", errors="replace")
    status, detail = _classify_whpx_output(text, code, timed_out=False)
    if status == "ok" and code == 0:
        # Exit 0 quickly with disk is still suspicious
        return False, f"实机探针过早退出 code=0: {detail}"
    return False, f"实机探针失败 exit={code}: {detail}"


def _probe_whpx(base_dir: Path) -> tuple[bool, str, str, str]:
    """
    Returns (ok, accelerator, cpu_model, detail).
    Soft probe first, then validate with short real-disk boot.
    """
    qemu = _qemu_binary(base_dir)
    if not Path(qemu).exists():
        return False, "", "", "qemu 可执行文件不存在"

    soft_ok: list[tuple[str, str, str]] = []
    tried: list[str] = []

    for accel, cpu, args in _WHPX_PROBE_MATRIX:
        status, detail, _ = _run_qemu_probe(qemu, args, timeout=4.0)
        tried.append(f"{accel}/{cpu}:{status}")
        if status == "ok":
            soft_ok.append((accel, cpu, detail))
        time.sleep(0.1)

    if not soft_ok:
        return False, "", "", "软探针全部失败: " + " | ".join(tried[-4:])

    # Validate candidates with real image (prefer first stable soft hit)
    for accel, cpu, soft_detail in soft_ok:
        boot_ok, boot_detail = _probe_boot_with_disk(base_dir, accel, cpu, hold_sec=3.5)
        if boot_ok:
            return True, accel, cpu, f"{soft_detail}；{boot_detail}"
        tried.append(f"boot {accel}/{cpu}: {boot_detail}")

    return False, "", "", "WHPX 软探针通过但实机启动失败: " + " | ".join(tried[-4:])


def _probe_kvm(base_dir: Path) -> tuple[bool, str]:
    qemu = _qemu_binary(base_dir)
    status, detail, _ = _run_qemu_probe(
        qemu,
        ["-machine", "none", "-accel", "kvm", "-display", "none", "-nodefaults"],
        timeout=5.0,
    )
    return status == "ok", detail


@dataclass
class HardwareAccelInfo:
    available: bool
    enabled: bool
    platform_ready: bool
    qemu_accel: str
    accelerator: str
    cpu_model: str
    message: str
    detail_lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.enabled and self.available:
            return f"硬件加速: 已启用 ({self.qemu_accel.upper()})"
        return "硬件加速: 软件模拟"

    def display_text(self) -> str:
        lines = [self.summary(), self.message]
        lines.extend(self.detail_lines)
        return "\n".join(lines)


def disable_hardware_acceleration(cfg, reason: str = "") -> HardwareAccelInfo:
    """Force software emulation after a WHPX crash at runtime."""
    cfg.localInstance.enableHardwareAcceleration = False
    cfg.localInstance.hardwareAccelerator = ""
    cfg.localInstance.cpuModel = "Cascadelake-Server-v5"
    msg = "WHPX 启动失败，已自动回退软件模拟。"
    if reason:
        msg += f" 原因: {reason[:160]}"
    return HardwareAccelInfo(
        available=False,
        enabled=False,
        platform_ready=True,
        qemu_accel="",
        accelerator="",
        cpu_model="Cascadelake-Server-v5",
        message=msg,
        detail_lines=[reason[:200]] if reason else [],
    )


def detect_hardware_acceleration(base_dir: Path) -> HardwareAccelInfo:
    base_dir = base_dir.resolve()
    accel_names = _qemu_accel_names(base_dir)
    details: list[str] = []

    if os.name == "nt":
        hyp_plat = _feature_state("HypervisorPlatform")
        vm_plat = _feature_state("VirtualMachinePlatform")
        hv_present = _hypervisor_present()
        fw_virt = _firmware_virt_enabled()
        other = _vmware_or_vbox_hint()

        details.append(f"HypervisorPlatform: {hyp_plat}")
        details.append(f"VirtualMachinePlatform: {vm_plat}")
        details.append(
            f"管理程序运行中: "
            f"{'是' if hv_present is True else '否' if hv_present is False else '未知'}"
        )
        details.append(
            f"固件虚拟化: "
            f"{'开' if fw_virt is True else '关' if fw_virt is False else '未知'}"
        )
        if other:
            details.append(f"环境: {other}")

        qemu_has = "whpx" in accel_names
        details.append(f"QEMU 含 WHPX: {'是' if qemu_has else '否'}")

        if not qemu_has:
            return HardwareAccelInfo(
                available=False,
                enabled=False,
                platform_ready=hyp_plat == "Enabled" or hv_present is True,
                qemu_accel="",
                accelerator="",
                cpu_model="Cascadelake-Server-v5",
                message="当前 QEMU 未编译 WHPX，使用软件模拟。",
                detail_lines=details,
            )

        ok, accel, cpu, probe_detail = _probe_whpx(base_dir)
        details.append(f"探针: {probe_detail}")

        if ok:
            short = "whpx" if accel.startswith("whpx") else accel
            return HardwareAccelInfo(
                available=True,
                enabled=True,
                platform_ready=True,
                qemu_accel=short,
                accelerator=accel,
                cpu_model=cpu or "qemu64-v1",
                message="WHPX 可用（已通过实机探针），冷启动更快。",
                detail_lines=details,
            )

        if hv_present is False:
            msg = (
                "管理程序未运行。管理员执行 bcdedit /set hypervisorlaunchtype auto 后重启。"
                "当前软件模拟。"
            )
        elif other:
            msg = f"WHPX 不可用（{other} 可能冲突）。当前软件模拟。"
        else:
            msg = f"WHPX 不可用，软件模拟。{probe_detail[:140]}"

        return HardwareAccelInfo(
            available=False,
            enabled=False,
            platform_ready=hyp_plat == "Enabled" or hv_present is True,
            qemu_accel="whpx",
            accelerator="",
            cpu_model="Cascadelake-Server-v5",
            message=msg,
            detail_lines=details,
        )

    # Linux KVM
    kvm_ready = Path("/dev/kvm").exists()
    qemu_has = "kvm" in accel_names
    probe_ok, probe_detail = (False, "跳过")
    if qemu_has and kvm_ready:
        probe_ok, probe_detail = _probe_kvm(base_dir)
    details = [
        f"/dev/kvm: {'存在' if kvm_ready else '不存在'}",
        f"QEMU KVM: {'是' if qemu_has else '否'}",
        f"探针: {probe_detail}",
    ]
    if kvm_ready and qemu_has and probe_ok:
        return HardwareAccelInfo(
            available=True,
            enabled=True,
            platform_ready=True,
            qemu_accel="kvm",
            accelerator="kvm",
            cpu_model="host",
            message="已启用 KVM。",
            detail_lines=details,
        )
    return HardwareAccelInfo(
        available=False,
        enabled=False,
        platform_ready=kvm_ready,
        qemu_accel="kvm" if qemu_has else "",
        accelerator="",
        cpu_model="Cascadelake-Server-v5",
        message="KVM 不可用，软件模拟。",
        detail_lines=details,
    )


def apply_hardware_acceleration(cfg, base_dir: Path, **_ignored) -> HardwareAccelInfo:
    """Always auto-apply detection result."""
    info = detect_hardware_acceleration(base_dir)
    if info.available and info.accelerator:
        cfg.localInstance.enableHardwareAcceleration = True
        cfg.localInstance.hardwareAccelerator = info.accelerator
        cfg.localInstance.cpuModel = info.cpu_model
        info.enabled = True
    else:
        cfg.localInstance.enableHardwareAcceleration = False
        cfg.localInstance.hardwareAccelerator = ""
        if not (cfg.localInstance.cpuModel or "").strip() or info.cpu_model:
            # Prefer software model when accel off
            if not info.available:
                cfg.localInstance.cpuModel = info.cpu_model or "Cascadelake-Server-v5"
        info.enabled = False
    return info
