"""
AppleMusicDecrypt GUI 启动入口。
开箱即用: 启动本地 QEMU wrapper-manager，然后打开图形界面。
"""
import os
import sys
from pathlib import Path


REQUIRED_PROJECT_PATHS = ("main.py", "src", "assets", "deps", "config.toml")


def _show_startup_error(message: str):
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "AppleMusicDecrypt GUI", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _project_root() -> Path:
    """Return the external AppleMusicDecrypt root, never PyInstaller's temp dir."""
    override = os.environ.get("AMD_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _validate_project_root(base_dir: Path):
    missing = [name for name in REQUIRED_PROJECT_PATHS if not (base_dir / name).exists()]
    if missing:
        names = "、".join(missing)
        raise RuntimeError(
            "GUI 插件必须复制到完整的 AppleMusicDecrypt 项目根目录后运行。\n\n"
            f"当前目录：{base_dir}\n缺少：{names}"
        )


def _set_windows_app_id():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AppleMusicDecrypt.GUI.TouchIDLogoV2",
        )
    except Exception:
        pass


def _register_creators():
    """与 main.py 相同：在任何 src 模块被加载前注册 creart 依赖。"""
    from asyncio import AbstractEventLoop
    from creart import add_creator, supported
    from src.config import Config

    if not supported(AbstractEventLoop):
        from creart.builtins.loop import EventLoopCreator

        add_creator(EventLoopCreator)

    if supported(Config):
        return

    from src.logger import LoggerCreator
    add_creator(LoggerCreator)
    from src.config import ConfigCreator
    add_creator(ConfigCreator)
    from src.api import APICreator
    add_creator(APICreator)
    from src.grpc.manager import WMCreator
    add_creator(WMCreator)
    from src.measurer import MeasurerCreator
    add_creator(MeasurerCreator)


def _verify_external_core():
    """Import the preserved core chain without starting the GUI or QEMU."""
    import asyncio

    from asyncio import AbstractEventLoop
    from creart import supported
    from gui.app import AppleMusicGUI  # noqa: F401
    from src.grpc.manager import WrapperManager  # noqa: F401
    from src.rip import Ripper  # noqa: F401
    from src.save import save  # noqa: F401
    from src.utils import run_sync

    if not supported(AbstractEventLoop):
        raise RuntimeError("creart event-loop support is unavailable")

    async def _probe_run_sync():
        return await run_sync(lambda: "ok")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if loop.run_until_complete(_probe_run_sync()) != "ok":
            raise RuntimeError("external run_sync probe failed")
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def main():
    _set_windows_app_id()
    base_dir = _project_root()
    try:
        _validate_project_root(base_dir)
    except RuntimeError as exc:
        _show_startup_error(str(exc))
        raise SystemExit(2) from exc

    os.environ["AMD_PROJECT_ROOT"] = str(base_dir)
    os.chdir(base_dir)
    sys.path.insert(0, str(base_dir))
    deps = base_dir / "deps"
    os.environ["PATH"] = str(deps) + os.pathsep + os.environ.get("PATH", "")
    os.environ["AMD_OFFLINE"] = "1"
    os.environ["AMD_GUI"] = "1"

    _register_creators()

    if "--verify-plugin" in sys.argv:
        _verify_external_core()
        return

    from gui.app import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
