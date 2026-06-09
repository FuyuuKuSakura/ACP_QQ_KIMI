#!/usr/bin/env python3
"""Mock Agent WebSocket Server for end-to-end testing.

Simulates a Kimi Code Agent (VSCode / CLI) that:
- Accepts WebSocket connections on ws://localhost:8765
- Handles ACP v1.0 UpstreamMessages
- Responds with DownstreamMessages after a short delay
- Supports interrupt (SIGINT simulation)
- Answers to WebSocket ping frames with pong

Usage:
    python scripts/mock_agent_server.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acp_qq_bridge.core.protocol import (
    DownstreamMessage,
    Payload,
    SessionState,
    UpstreamMessage,
    parse_message,
    serialize_message,
)
from acp_qq_bridge.utils.logger import configure_logging, get_logger

configure_logging()
logger = get_logger("mock_agent")

HOST = "127.0.0.1"
PORT = 18765


async def _echo_handler(websocket) -> None:  # type: ignore[no-untyped-def]
    """Handle a single WebSocket client connection."""
    client_addr = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    logger.info("Agent connected from %s", client_addr)

    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            try:
                msg = parse_message(raw)
            except Exception as exc:
                logger.warning("Failed to parse message: %s", exc)
                continue

            if not isinstance(msg, UpstreamMessage):
                logger.debug("Ignoring non-upstream message")
                continue

            logger.info(
                "Received [%s] session=%s action=%s text=%r",
                msg.trace_id,
                msg.session_id,
                msg.action,
                msg.payload.text,
            )

            if msg.action == "interrupt":
                # Simulate task interruption
                reply = DownstreamMessage(
                    source="cli",
                    session=SessionState(
                        session_id=msg.session_id,
                        status="interrupted",
                    ),
                    payload=Payload(
                        type="status_update",
                        content="任务已被用户打断。",
                    ),
                )
                await websocket.send(serialize_message(reply))
                logger.info("Sent interrupt acknowledgment for %s", msg.session_id)

            elif msg.action == "user_input":
                # Simulate "thinking" -> "executing" -> "done" sequence
                await _send_status(websocket, msg.session_id, "thinking", "正在分析问题...")
                await asyncio.sleep(0.3)
                await _send_status(websocket, msg.session_id, "executing", "正在执行操作...")
                await asyncio.sleep(0.3)

                text = msg.payload.text.strip()
                if text.startswith("/"):
                    cmd = text.split(None, 1)[0]
                    reply_text = f"收到指令 `{cmd}`，执行结果如下:\n\n```\n模拟执行输出\nline 1\nline 2\n```"
                else:
                    reply_text = f"收到消息: {text}\n\n模拟回复: 已处理完毕 ✅"

                reply = DownstreamMessage(
                    source="cli",
                    session=SessionState(
                        session_id=msg.session_id,
                        status="idle",
                    ),
                    payload=Payload(
                        type="text",
                        content=reply_text,
                        artifacts={"charts": [], "emojis": ["🤖", "🚀"]},
                    ),
                )
                await websocket.send(serialize_message(reply))
                logger.info("Sent reply for %s", msg.session_id)

            elif msg.action == "inject":
                reply = DownstreamMessage(
                    source="cli",
                    session=SessionState(
                        session_id=msg.session_id,
                        status="idle",
                    ),
                    payload=Payload(
                        type="text",
                        content="注入指令已执行。",
                    ),
                )
                await websocket.send(serialize_message(reply))

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Handler error for %s", client_addr)
    finally:
        logger.info("Agent disconnected from %s", client_addr)


async def _send_status(websocket, session_id: str, status: str, content: str) -> None:  # type: ignore[no-untyped-def]
    """Send a status_update downstream message."""
    msg = DownstreamMessage(
        source="cli",
        session=SessionState(session_id=session_id, status=status),
        payload=Payload(type="status_update", content=content),
    )
    await websocket.send(serialize_message(msg))


_server_stop_event: asyncio.Event | None = None
_server_task: asyncio.Task[None] | None = None


async def start_server() -> None:
    """Start the mock Agent WebSocket server (non-blocking)."""
    global _server_stop_event, _server_task
    import websockets

    _server_stop_event = asyncio.Event()
    logger.info("Starting Mock Agent Server on ws://%s:%d", HOST, PORT)

    async def _run() -> None:
        async with websockets.serve(_echo_handler, HOST, PORT):
            logger.info("Mock Agent Server ready")
            await _server_stop_event.wait()

    _server_task = asyncio.create_task(_run(), name="mock-agent-server")
    # Give it time to bind
    await asyncio.sleep(0.3)


async def stop_server() -> None:
    """Stop the mock Agent WebSocket server."""
    global _server_stop_event, _server_task
    if _server_stop_event is not None:
        _server_stop_event.set()
    if _server_task is not None:
        _server_task.cancel()
        try:
            await _server_task
        except asyncio.CancelledError:
            pass
    logger.info("Mock Agent Server stopped")


async def main() -> None:
    """CLI entry point for the mock Agent WebSocket server."""
    await start_server()
    logger.info("Press Ctrl+C to stop")
    try:
        await asyncio.Future()
    finally:
        await stop_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
