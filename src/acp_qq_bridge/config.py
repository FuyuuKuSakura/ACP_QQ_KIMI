"""ACP-QQ Bridge configuration loader.

Supports loading ``BridgeConfig`` from a YAML file with nested sections
for ``agent``, ``qq``, ``security`` and ``persona``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentConfig:
    """Agent WebSocket connection settings."""

    ws_url: str
    token: str
    heartbeat_interval: int
    reconnect_max_interval: int
    response_timeout: int
    # kimi CLI 模型别名（如 "moonshot-cn/kimi-k2.6"）；留空 = 用 CLI 默认模型
    model: str = ""


@dataclass
class QQConfig:
    """QQ bot runtime settings."""

    command_prefixes: list[str]
    superusers: list[str]
    session_ttl: int


@dataclass
class SecurityConfig:
    """Security engine settings."""

    enable_ast_audit: bool
    enable_sensitive_filter: bool
    allowed_commands: list[str]
    sensitive_patterns: list[str]
    max_message_length: int


@dataclass
class PersonaConfig:
    """Persona / role-play settings."""

    default_persona: str
    personas_dir: str


@dataclass
class LifeOSConfig:
    """LifeOS 任务管理扩展设置（Obsidian vault + QQ 遥控）。"""

    enabled: bool
    vault_path: str
    target_qq: str
    plain_message_mode: str
    daily_brief_time: str
    reminder_time: str
    weekly_reminder: dict[str, Any]
    persona: str
    state_file: str


@dataclass
class BridgeConfig:
    """Top-level configuration object for ACP-QQ Bridge."""

    agent: AgentConfig
    qq: QQConfig
    security: SecurityConfig
    persona: PersonaConfig
    lifeos: LifeOSConfig


_DEFAULT_CONFIG: dict[str, Any] = {
    "agent": {
        "ws_url": "ws://localhost:8765",
        "token": "",
        "heartbeat_interval": 30,
        "reconnect_max_interval": 30,
        "response_timeout": 60,
    },
    "qq": {
        "command_prefixes": ["/", "!"],
        "superusers": [],
        "session_ttl": 3600,
    },
    "security": {
        "enable_ast_audit": True,
        "enable_sensitive_filter": True,
        "allowed_commands": ["ls", "cat", "grep", "find", "pwd", "echo", "python", "pytest"],
        "sensitive_patterns": ["rm -rf", "mkfs", "dd if=/dev/zero", ">/dev/sda", "format", "del /f /s /q"],
        "max_message_length": 4096,
    },
    "persona": {
        "default_persona": "assistant",
        "personas_dir": "./personas",
    },
    "lifeos": {
        "enabled": True,
        "vault_path": "",
        "target_qq": "3058442393",
        "plain_message_mode": "agent",
        "daily_brief_time": "04:00",
        "reminder_time": "22:30",
        "weekly_reminder": {"weekday": 6, "time": "10:00"},
        "persona": "kaltsit",
        "state_file": "logs/lifeos_state.json",
    },
}


def load_config(path: str | Path | None = None) -> BridgeConfig:
    """Load configuration from a YAML file.

    If *path* is ``None`` or the file does not exist, default values are used.

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        A fully populated :class:`BridgeConfig` instance.
    """
    data = _DEFAULT_CONFIG.copy()
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                file_data: dict[str, Any] = yaml.safe_load(fh) or {}
            for key in data:
                if key in file_data and isinstance(file_data[key], dict):
                    data[key].update(file_data[key])
        else:
            import logging
            logging.getLogger(__name__).warning("Config file not found: %s, using defaults", p)

    return BridgeConfig(
        agent=AgentConfig(**data["agent"]),
        qq=QQConfig(**data["qq"]),
        security=SecurityConfig(**data["security"]),
        persona=PersonaConfig(**data["persona"]),
        lifeos=LifeOSConfig(**data["lifeos"]),
    )


def save_config_template(path: str | Path) -> None:
    """Write the default configuration template to *path*."""
    p = Path(path)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(_DEFAULT_CONFIG, fh, default_flow_style=False, allow_unicode=True, sort_keys=False)
    import logging
    logging.getLogger(__name__).info("Config template written to: %s", p)
