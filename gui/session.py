import json
from pathlib import Path
from typing import Optional


def session_path(base_dir: Path) -> Path:
    return base_dir / "data" / "current_account.json"


def load_account(base_dir: Path) -> Optional[str]:
    path = session_path(base_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("username") or None
    except (json.JSONDecodeError, OSError):
        return None


def save_account(base_dir: Path, username: str):
    path = session_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"username": username}, ensure_ascii=False), encoding="utf-8")


def clear_account(base_dir: Path):
    path = session_path(base_dir)
    if path.exists():
        path.unlink()