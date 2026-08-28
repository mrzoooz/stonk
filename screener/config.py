"""Configuration loading.

The screener reads every threshold from config.yaml so that tuning the screen
never requires touching code.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


class Config(dict):
    """A dict with dotted-path lookup: cfg.get_path("vcp.zigzag_pct")."""

    def get_path(self, path: str, default: Any = None) -> Any:
        node: Any = self
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path) as fh:
        return Config(yaml.safe_load(fh) or {})
