"""Configuration loading from .env / environment."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    spoolman_url: str | None  # from the environment; the setup page can choose another
    td_database: Path
    td_user: Path
    catalog: Path
    data_dir: Path


def _find_env_file() -> Path | None:
    for candidate in (Path.cwd() / ".env", REPO_ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


def _warn_if_world_readable(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(f"warning: {path} is readable by others; run `chmod 600 {path}`", file=sys.stderr)


def load_config() -> Config:
    env_file = _find_env_file()
    if env_file:
        _warn_if_world_readable(env_file)
        load_dotenv(env_file, override=False)
    data_dir = Path(os.environ.get("SPOOLMIX_DATA_DIR", REPO_ROOT / "data"))
    return Config(
        spoolman_url=(os.environ.get("SPOOLMAN_URL") or "").rstrip("/") or None,
        td_database=Path(os.environ.get("SPOOLMIX_TD_DATABASE", REPO_ROOT / "td_database.yaml")),
        catalog=Path(os.environ.get("SPOOLMIX_CATALOG", REPO_ROOT / "catalog.yaml")),
        data_dir=data_dir,
        td_user=Path(os.environ.get("SPOOLMIX_TD_USER", data_dir / "td_user.yaml")),
    )
