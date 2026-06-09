from __future__ import annotations

import json

import pytest

from acp_qq_bridge.core.protocol import (
    ACPProtocolError,
    DownstreamMessage,
    Payload,
    SessionState,
    UpstreamMessage,
    UpstreamPayload,
    parse_message,
    serialize_message,
)


def test_downstream_message_creation() -> None:
    """创建 DownstreamMessage，验证 protocol、trace_id、timestamp 等默认值。"""
    payload = Payload(type="text", content="hello")
    session = SessionState(session_id="s1", status="idle")
    msg = DownstreamMessage(source="cli", session=session, payload=payload)

    assert msg.protocol == "ACP/1.0"
    assert isinstance(msg.trace_id, str) and len(msg.trace_id) > 0
    assert isinstance(msg.timestamp, int) and msg.timestamp > 0
    assert msg.source == "cli"
    assert msg.session.session_id == "s1"
    assert msg.payload.content == "hello"


def test_upstream_message_creation() -> None:
    """创建 UpstreamMessage，验证字段正确性。"""
    payload = UpstreamPayload(text="/help", raw_signal=None)
    msg = UpstreamMessage(session_id="s1", action="user_input", payload=payload)

    assert msg.protocol == "ACP/1.0"
    assert isinstance(msg.trace_id, str) and len(msg.trace_id) > 0
    assert isinstance(msg.timestamp, int) and msg.timestamp > 0
    assert msg.session_id == "s1"
    assert msg.action == "user_input"
    assert msg.payload.text == "/help"
    assert msg.payload.raw_signal is None


def test_parse_message_downstream() -> None:
    """从 dict 解析下行消息。"""
    data = {
        "protocol": "ACP/1.0",
        "trace_id": "t1",
        "timestamp": 123,
        "source": "cli",
        "session": {"session_id": "s1", "status": "idle"},
        "payload": {"type": "text", "content": "hello"},
    }
    msg = parse_message(data)

    assert isinstance(msg, DownstreamMessage)
    assert msg.source == "cli"
    assert msg.session.session_id == "s1"
    assert msg.session.status == "idle"
    assert msg.payload.content == "hello"


def test_parse_message_upstream() -> None:
    """从 dict 解析上行消息。"""
    data = {
        "protocol": "ACP/1.0",
        "trace_id": "t2",
        "timestamp": 456,
        "session_id": "s1",
        "action": "interrupt",
        "payload": {"text": "stop", "raw_signal": "SIGINT"},
    }
    msg = parse_message(data)

    assert isinstance(msg, UpstreamMessage)
    assert msg.session_id == "s1"
    assert msg.action == "interrupt"
    assert msg.payload.text == "stop"
    assert msg.payload.raw_signal == "SIGINT"


def test_parse_message_from_json_string() -> None:
    """从 JSON 字符串解析消息。"""
    raw = json.dumps({
        "protocol": "ACP/1.0",
        "trace_id": "t3",
        "timestamp": 789,
        "source": "vscode-extension",
        "session": {"session_id": "s2", "status": "thinking"},
        "payload": {"type": "status_update", "content": "working"},
    })
    msg = parse_message(raw)

    assert isinstance(msg, DownstreamMessage)
    assert msg.source == "vscode-extension"
    assert msg.session.status == "thinking"
    assert msg.payload.type == "status_update"


def test_parse_message_invalid_json() -> None:
    """非法 JSON 字符串应抛 ACPProtocolError。"""
    with pytest.raises(ACPProtocolError):
        parse_message("not json {")


def test_parse_message_unknown_type() -> None:
    """缺少必要字段应抛 ACPProtocolError。"""
    with pytest.raises(ACPProtocolError):
        parse_message({"protocol": "ACP/1.0", "trace_id": "t", "timestamp": 1})


def test_parse_message_unsupported_protocol() -> None:
    """协议版本不是 ACP/1.0 应抛异常。"""
    data = {
        "protocol": "ACP/2.0",
        "source": "cli",
        "session": {"session_id": "s1", "status": "idle"},
        "payload": {"type": "text", "content": "hello"},
    }
    with pytest.raises(ACPProtocolError):
        parse_message(data)


def test_serialize_message() -> None:
    """序列化后 JSON 可反序列化回相同内容。"""
    payload = Payload(type="text", content="hello")
    session = SessionState(session_id="s1", status="idle")
    msg = DownstreamMessage(source="cli", session=session, payload=payload)

    raw = serialize_message(msg)
    data = json.loads(raw)

    assert data["protocol"] == "ACP/1.0"
    assert data["source"] == "cli"
    assert data["session"]["session_id"] == "s1"
    assert data["payload"]["content"] == "hello"
    assert "trace_id" in data
    assert "timestamp" in data


def test_round_trip() -> None:
    """parse(serialize(msg)) == msg。"""
    payload = Payload(type="rich_media", content="media")
    session = SessionState(session_id="s3", status="executing")
    original = DownstreamMessage(source="vscode-extension", session=session, payload=payload)

    raw = serialize_message(original)
    restored = parse_message(raw)

    assert isinstance(restored, DownstreamMessage)
    assert restored == original
