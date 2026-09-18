"""Tiny JSON-file index: technician phone number -> [profile_id, ...].

Without a `mobile` (or `phone`) identifier on tech profiles, Memory can't answer
"which profiles belong to this phone?" — so the app has to remember it locally.
Every profile ever created for a given phone is recorded here; `find_active_profile`
iterates and filters by `Job.status == "active"`.

The file lives at STATE_FILE (default: `.state/phone_index.json` next to this
module). It's plain JSON: `{"phone_number": ["profile_id_1", ...]}`.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_STATE_DIR = Path(__file__).parent / ".state"
STATE_FILE = Path(os.environ.get("PHONE_INDEX_PATH", _STATE_DIR / "phone_index.json"))

_lock = threading.Lock()


def _load() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict[str, list[str]]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def record_profile(phone: str, profile_id: str) -> None:
    """Add profile_id under phone. Idempotent."""
    with _lock:
        data = _load()
        ids = data.setdefault(phone, [])
        if profile_id not in ids:
            ids.append(profile_id)
            _save(data)


def forget_profile(phone: str, profile_id: str) -> None:
    """Remove profile_id from phone's list, and drop the phone entry if empty."""
    with _lock:
        data = _load()
        ids = data.get(phone)
        if not ids or profile_id not in ids:
            return
        ids.remove(profile_id)
        if not ids:
            data.pop(phone, None)
        _save(data)


def profiles_for_phone(phone: str) -> list[str]:
    """Every profile_id ever recorded for this phone (in insertion order)."""
    return list(_load().get(phone, []))


def all_phones() -> list[str]:
    return list(_load().keys())
