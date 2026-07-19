"""Read wrapper-manager account state from the local QEMU VM via QGA."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

WM_ROOT = "/root/wrapper-manager"
INSTANCES_PATHS = (
    f"{WM_ROOT}/data/instances.json",
    "/data/instances.json",
    "/root/data/instances.json",
)


@dataclass
class VmAccountState:
    vm_logged_in: bool = False
    instance_ids: list[str] | None = None
    regions: list[str] | None = None
    music_token: Optional[str] = None


async def read_vm_account_state(qga_client, regions: list[str] | None = None) -> VmAccountState:
    """Probe VM disk for persisted wrapper instances and music user token."""
    state = VmAccountState(regions=list(regions or []))
    raw = ""
    for path in INSTANCES_PATHS:
        try:
            raw = (await qga_client.read_file(path)).strip()
        except Exception:
            raw = ""
        if raw:
            break
    if not raw:
        return state

    try:
        instances = json.loads(raw)
    except json.JSONDecodeError:
        return state

    if not isinstance(instances, list) or not instances:
        return state

    ids: list[str] = []
    for item in instances:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, dict) and item.get("Id"):
            ids.append(str(item["Id"]))

    if not ids:
        return state

    state.vm_logged_in = True
    state.instance_ids = ids

    token_paths = [f"{WM_ROOT}/data/wrapper/rootfs/data/instances/{{id}}/MUSIC_TOKEN"]
    for iid in ids:
        for tmpl in token_paths:
            try:
                token = (await qga_client.read_file(tmpl.format(id=iid))).strip()
            except Exception:
                token = ""
            if token:
                state.music_token = token
                break
        if state.music_token:
            break

    return state