"""
flhb-tool - Phase 2: Core Orchestration & Wrapper Engine - Configuration.

Defines the bridge configuration schema (config.json) used to orchestrate
the execution of a game or loader inside a Lutris Wine prefix.

Schema fields:
  wine_prefix_path      (str, required)  Absolute path to the Lutris Wine prefix.
  target_executable     (str, required)  Path to the game or loader binary.
  loader_arguments      (list[str])      CLI arguments for the loader.
  environment_overrides (dict[str,str])  Custom env vars (DXVK, VKD3D, WINEDEBUG...).
  working_directory     (str, optional)  Defaults to dirname(target_executable).
  wine_arch             (str, optional)  "win64" or "win32".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("flhb-tool.bridge.config")

REQUIRED_FIELDS = ("wine_prefix_path", "target_executable")


@dataclass
class BridgeConfig:
    """Validated bridge configuration loaded from config.json."""

    wine_prefix_path: str
    target_executable: str
    loader_arguments: List[str] = field(default_factory=list)
    environment_overrides: Dict[str, str] = field(default_factory=dict)
    working_directory: Optional[str] = None
    wine_arch: Optional[str] = None  # e.g. "win64" or "win32"

    def __post_init__(self) -> None:
        # Default working directory to the directory of the target executable.
        if self.working_directory is None:
            self.working_directory = os.path.dirname(
                os.path.abspath(self.target_executable)
            )

    @property
    def prefix_path(self) -> str:
        """Alias for the Wine prefix path."""
        return self.wine_prefix_path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "wine_prefix_path": self.wine_prefix_path,
            "target_executable": self.target_executable,
            "loader_arguments": self.loader_arguments,
            "environment_overrides": self.environment_overrides,
            "working_directory": self.working_directory,
            "wine_arch": self.wine_arch,
        }


class ConfigError(ValueError):
    """Raised when the bridge configuration is invalid or missing."""


def load_config(path: str) -> BridgeConfig:
    """Load and validate a bridge configuration from a JSON file.

    Args:
        path: Filesystem path to config.json.

    Returns:
        A validated :class:`BridgeConfig`.

    Raises:
        ConfigError: If the file is missing, invalid JSON, or fails schema
            validation.
    """
    if not os.path.isfile(path):
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Configuration root must be a JSON object, got {type(raw).__name__}"
        )

    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ConfigError(f"Missing required configuration fields: {', '.join(missing)}")

    wine_prefix_path = str(raw["wine_prefix_path"])
    target_executable = str(raw["target_executable"])

    if not os.path.isabs(wine_prefix_path):
        logger.warning("wine_prefix_path is not absolute: %s", wine_prefix_path)
    if not os.path.isabs(target_executable):
        logger.warning("target_executable is not absolute: %s", target_executable)

    loader_arguments = list(raw.get("loader_arguments", []))
    if not all(isinstance(a, str) for a in loader_arguments):
        raise ConfigError("loader_arguments must be a list of strings")

    environment_overrides = raw.get("environment_overrides", {})
    if not isinstance(environment_overrides, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in environment_overrides.items()
    ):
        raise ConfigError("environment_overrides must be a mapping of string -> string")

    working_directory = raw.get("working_directory")
    if working_directory is not None:
        working_directory = str(working_directory)

    wine_arch = raw.get("wine_arch")
    if wine_arch is not None:
        wine_arch = str(wine_arch)

    config = BridgeConfig(
        wine_prefix_path=wine_prefix_path,
        target_executable=target_executable,
        loader_arguments=loader_arguments,
        environment_overrides=environment_overrides,
        working_directory=working_directory,
        wine_arch=wine_arch,
    )

    logger.info("Loaded bridge configuration from %s", path)
    logger.debug("Config: %s", config.to_dict())
    return config


def write_example_config(path: str) -> None:
    """Write a documented example config.json to *path*."""
    example = {
        "$schema": "flhb-tool bridge config v1",
        "wine_prefix_path": "/home/user/Games/lutris-pfx",
        "target_executable": "/home/user/Games/game/bin/game.exe",
        "loader_arguments": ["--loader-flag", "value"],
        "environment_overrides": {
            "DXVK": "1",
            "VKD3D": "1",
            "WINEDEBUG": "-all",
            "WINEESYNC": "1",
            "WINEFSYNC": "1",
        },
        "working_directory": "/home/user/Games/game/bin",
        "wine_arch": "win64",
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(example, fh, indent=2)
        fh.write("\n")
    logger.info("Wrote example configuration to %s", path)
