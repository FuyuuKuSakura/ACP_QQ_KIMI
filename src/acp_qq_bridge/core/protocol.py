"""ACP (Agent Communication Protocol) v1.0 数据模型与序列化工具.

本模块使用 Pydantic v2 定义严格的类型化消息模型，支持下行消息
(Agent -> Bridge -> QQ) 和上行消息 (QQ -> Bridge -> Agent) 的
自动解析与序列化。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ACPProtocolError(Exception):
    """ACP 协议解析或验证失败时抛出的自定义异常."""

    pass


def _default_timestamp() -> int:
    """返回当前 UTC Unix 时间戳（秒级）."""
    return int(datetime.now(timezone.utc).timestamp())


def _default_trace_id() -> str:
    """生成 UUID v4 字符串作为默认 trace_id."""
    return str(uuid.uuid4())


class ACPMessage(BaseModel):
    """ACP 消息基类，包含所有消息共有的协议头字段."""

    protocol: Literal["ACP/1.0"] = "ACP/1.0"
    trace_id: str = Field(default_factory=_default_trace_id)
    timestamp: int = Field(default_factory=_default_timestamp)


class SessionState(BaseModel):
    """会话状态模型，用于下行消息中的 session 字段."""

    session_id: str
    status: Literal["thinking", "executing", "idle", "interrupted"]


class ArtifactChart(BaseModel):
    """图表类工件模型."""

    type: str
    data: dict[str, Any]


class Artifacts(BaseModel):
    """下行消息 payload 中携带的工件集合."""

    charts: list[ArtifactChart] = Field(default_factory=list)
    emojis: list[str] = Field(default_factory=list)


class Payload(BaseModel):
    """下行消息 (Agent -> Bridge -> QQ) 的 payload 模型."""

    type: Literal["text", "rich_media", "status_update"]
    content: str
    artifacts: Artifacts | None = None


class UpstreamPayload(BaseModel):
    """上行消息 (QQ -> Bridge -> Agent) 的 payload 模型."""

    text: str
    raw_signal: str | None = None
    work_dir: str | None = None
    kimi_session_id: str | None = None


class DownstreamMessage(ACPMessage):
    """下行消息：Agent / Bridge 发往 QQ 端.

    典型场景包括：状态更新、文本回复、富媒体消息等。
    """

    source: Literal["vscode-extension", "cli"]
    session: SessionState
    payload: Payload


class UpstreamMessage(ACPMessage):
    """上行消息：QQ 端发往 Agent / Bridge.

    典型场景包括：用户输入、中断指令、注入指令等。
    """

    session_id: str
    action: Literal["user_input", "interrupt", "inject"]
    payload: UpstreamPayload


def parse_message(raw: dict[str, Any] | str) -> ACPMessage:
    """自动解析原始消息并返回对应的 ACPMessage 子类实例.

    根据字典中是否存在 ``source`` 与 ``session`` 字段判断为
    :class:`DownstreamMessage`；根据是否存在 ``session_id`` 与
    ``action`` 字段判断为 :class:`UpstreamMessage`。

    Args:
        raw: 原始 JSON 字符串或已反序列化的字典。

    Returns:
        解析后的消息实例（DownstreamMessage 或 UpstreamMessage）。

    Raises:
        ACPProtocolError: 当 JSON 解析失败或无法识别消息类型时抛出。
    """
    if isinstance(raw, str):
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ACPProtocolError(f"Invalid JSON string: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise ACPProtocolError(f"Message must be a JSON object, got {type(data).__name__}")

    # 协议版本校验（可选但建议）
    proto = data.get("protocol")
    if proto is not None and proto != "ACP/1.0":
        raise ACPProtocolError(f"Unsupported protocol version: {proto!r}")

    if "source" in data and "session" in data:
        try:
            return DownstreamMessage.model_validate(data)
        except Exception as exc:
            raise ACPProtocolError(f"DownstreamMessage validation failed: {exc}") from exc

    if "session_id" in data and "action" in data:
        try:
            return UpstreamMessage.model_validate(data)
        except Exception as exc:
            raise ACPProtocolError(f"UpstreamMessage validation failed: {exc}") from exc

    raise ACPProtocolError(
        "Unable to determine message type: missing required fields for both "
        "DownstreamMessage ('source', 'session') and UpstreamMessage ('session_id', 'action')."
    )


def serialize_message(msg: ACPMessage) -> str:
    """将 ACPMessage 实例序列化为 JSON 字符串.

    Args:
        msg: 待序列化的消息实例。

    Returns:
        JSON 字符串表示。

    Raises:
        ACPProtocolError: 当序列化过程中发生异常时抛出。
    """
    try:
        return msg.model_dump_json(by_alias=True, exclude_none=False)
    except Exception as exc:
        raise ACPProtocolError(f"Message serialization failed: {exc}") from exc
