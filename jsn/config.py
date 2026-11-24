from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path("config/settings.yaml")


def load_config() -> dict[str, Any]:
    path = Path(os.getenv("JSN_CONFIG_PATH", DEFAULT_CONFIG_PATH))
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    if not isinstance(config, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")

    profile = config.get("profile", {})
    if not profile.get("skills") and not profile.get("job_titles"):
        raise ValueError("Set at least one profile skill or job title in config/settings.yaml")

    for section in ("gmail", "filters", "llm", "discord", "database"):
        if not isinstance(config.get(section), dict):
            raise ValueError(
                f"Configuration {path} must define a top-level {section} mapping; "
                "see config/settings.example.yaml. In GitHub Actions, update the "
                "JSN_SETTINGS_YAML secret."
            )
    if not isinstance(config.get("job_details", {}), dict):
        raise ValueError("Configuration job_details must be a mapping")

    database_path = os.getenv("JSN_DATABASE_PATH")
    if database_path is not None:
        if not database_path.strip():
            raise ValueError("JSN_DATABASE_PATH must not be empty")
        config["database"]["path"] = database_path

    return config
