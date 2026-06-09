#!/usr/bin/env python3
"""Kimi Code CLI 0.6.0 ↔ ACP WebSocket Bridge (Production-Ready).

专为 kimi-code CLI 设计，利用 --output-format stream-json 和 session 恢复机制：
- 每个 QQ 会话绑定一个独立的 kimi session
- 首次对话自动创建新 session
- 后续对话通过 -S <session_id> 恢复上下文
- 解析 stream-json 提取 assistant 回复

Usage:
    python scripts/kimi_code_bridge.py

Requirements:
    kimi CLI 0.6.0+ 已安装且在 PATH 中
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import websockets
from websockets.server import WebSocketServerProtocol

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
logger = get_logger("kimi_code_bridge")

WS_HOST = "127.0.0.1"
WS_PORT = 8765

KIMI_BIN = shutil.which("kimi") or shutil.which("kimi-code") or "kimi"


class KimiSession:
    """Manages a single kimi CLI session tied to a QQ session."""

    def __init__(self, session_id: str, work_dir: Path) -> None:
        self.qq_session_id = session_id
        self.work_dir = work_dir
        self.kimi_session_id: str | None = None
        self._lock = asyncio.Lock()

    async def ask(self, text: str) -> str:
        """Send a message to kimi CLI and return the assistant reply."""
        async with self._lock:
            # Build command
            cmd = [
                KIMI_BIN,
                "-p", text,
                "--output-format", "stream-json",
            ]
            if self.kimi_session_id:
                cmd += ["-S", self.kimi_session_id]

            logger.info(
                "[%s] Calling kimi (session=%s)",
                self.qq_session_id,
                self.kimi_session_id or "new",
            )

            # Run kimi CLI
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.work_dir),
                env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1"},
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                logger.error("[%s] kimi exited %d: %s", self.qq_session_id, proc.returncode, err)
                return f"Kimi CLI 执行出错 (code={proc.returncode}):\n{err}"[:2000]

            return self._parse_output(stdout.decode("utf-8", errors="replace"))

    def _parse_output(self, raw: str) -> str:
        """Parse stream-json output from kimi CLI."""
        reply_lines: list[str] = []
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                # Fallback: treat as plain text
                reply_lines.append(line)
                continue

            role = data.get("role")
            if role == "assistant":
                content = data.get("content", "")
                if content:
                    reply_lines.append(content)
            elif role == "meta" and data.get("type") == "session.resume_hint":
                # Extract session_id for future continuity
                sid = data.get("session_id")
                if sid:
                    self.kimi_session_id = sid
                    logger.info("[%s] Bound to kimi session %s", self.qq_session_id, sid)

        if not reply_lines:
            return "（Kimi 未返回有效内容）"

        return "\n".join(reply_lines)


class KimiCodeBridgeServer:
    """WebSocket server bridging ACP messages to Kimi Code CLI."""

    def __init__(self) -> None:
        self._sessions: dict[str, KimiSession] = {}
        self._lock = asyncio.Lock()
        self._base_dir = Path(tempfile.gettempdir()) / "kimi_code_sessions"
        self._base_dir.mkdir(exist_ok=True)

    async def _get_session(self, session_id: str) -> KimiSession:
        async with self._lock:
            if session_id not in self._sessions:
                work_dir = self._base_dir / session_id
                work_dir.mkdir(exist_ok=True)
                self._sessions[session_id] = KimiSession(session_id, work_dir)
            return self._sessions[session_id]

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        client_addr = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"  # type: ignore[index]
        logger.info("Bridge connected from %s", client_addr)

        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                try:
                    msg = parse_message(raw)
                except Exception as exc:
                    logger.warning("Parse error: %s", exc)
                    continue

                if not isinstance(msg, UpstreamMessage):
                    continue

                await self._process_message(websocket, msg)

        except websockets.exceptions.ConnectionClosed:
            logger.info("Bridge disconnected")
        except Exception:
            logger.exception("Handler error")

    async def _process_message(
        self, websocket: WebSocketServerProtocol, msg: UpstreamMessage
    ) -> None:
        session_id = msg.session_id
        text = msg.payload.text

        logger.info("[%s] action=%s text=%r", session_id, msg.action, text)

        if msg.action == "interrupt":
            await self._send(
                websocket, session_id, "interrupted", "任务已被用户打断。"
            )
            return

        if msg.action not in ("user_input", "inject"):
            return

        # Send thinking status
        await self._send(websocket, session_id, "thinking", "正在分析问题...")

        try:
            kimi_session = await self._get_session(session_id)

            # Send executing status
            await self._send(websocket, session_id, "executing", "正在调用 Kimi Code...")

            # Call kimi CLI
            reply = await kimi_session.ask(text)

            # Send final reply
            await self._send(websocket, session_id, "idle", reply)

        except Exception as exc:
            logger.exception("[%s] Processing error", session_id)
            await self._send(
                websocket, session_id, "idle", f"处理出错: {exc}"
            )

    async def _send(
        self,
        websocket: WebSocketServerProtocol,
        session_id: str,
        status: str,
        content: str,
    ) -> None:
        msg = DownstreamMessage(
            source="cli",
            session=SessionState(session_id=session_id, status=status),
            payload=Payload(
                type="status_update" if status != "idle" else "text",
                content=content,
            ),
        )
        try:
            await websocket.send(serialize_message(msg))
        except Exception:
            logger.exception("Failed to send downstream")

    async def run(self, port: int) -> None:
        logger.info("Starting Kimi Code Bridge on ws://%s:%d", WS_HOST, port)
        logger.info("Kimi binary: %s", KIMI_BIN)
        logger.info("Session workspace: %s", self._base_dir)
        async with websockets.serve(self._handle_client, WS_HOST, port):
            logger.info("Ready. Waiting for ACP-QQ-Bridge connection...")
            await asyncio.Future()


_server_stop_event: asyncio.Event | None = None
_server_task: asyncio.Task[None] | None = None


async def start_server(port: int = WS_PORT) -> None:
    """Start the Kimi Code Bridge server (non-blocking)."""
    global _server_stop_event, _server_task

    if not shutil.which(KIMI_BIN):
        logger.error(
            "kimi CLI not found in PATH.\n"
            "Install from: https://moonshotai.github.io/kimi-code/"
        )
        raise RuntimeError("kimi CLI not found")

    _server_stop_event = asyncio.Event()
    server = KimiCodeBridgeServer()

    async def _run() -> None:
        async with websockets.serve(server._handle_client, WS_HOST, port):
            logger.info("Kimi Code Bridge ready on ws://%s:%d", WS_HOST, port)
            await _server_stop_event.wait()

    _server_task = asyncio.create_task(_run(), name="kimi-bridge-server")
    await asyncio.sleep(1.0)  # wait for bind


async def stop_server() -> None:
    """Stop the Kimi Code Bridge server."""
    global _server_stop_event, _server_task
    if _server_stop_event is not None:
        _server_stop_event.set()
    if _server_task is not None:
        _server_task.cancel()
        try:
            await _server_task
        except asyncio.CancelledError:
            pass
    logger.info("Kimi Code Bridge stopped")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Kimi Code CLI Bridge")
    parser.add_argument("--port", type=int, default=WS_PORT)
    args = parser.parse_args()

    try:
        asyncio.run(_run_main(port=args.port))
    except KeyboardInterrupt:
        logger.info("Bridge stopped.")


async def _run_main(port: int) -> None:
    await start_server(port)
    try:
        await asyncio.Future()
    finally:
        await stop_server()


if __name__ == "__main__":
    main()
