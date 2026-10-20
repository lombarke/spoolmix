"""User settings saved from the web UI (data/settings.json)."""

from __future__ import annotations

import json
import threading

from spoolmix.config import Config

SETTINGS_FILE = "settings.json"
_lock = threading.Lock()


def load_settings(cfg: Config) -> dict:
    try:
        return json.loads((cfg.data_dir / SETTINGS_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(cfg: Config, **changes) -> dict:
    with _lock:
        s = load_settings(cfg) | changes
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = cfg.data_dir / (SETTINGS_FILE + ".part")
        tmp.write_text(json.dumps(s, indent=1))
        tmp.replace(cfg.data_dir / SETTINGS_FILE)
    return s
