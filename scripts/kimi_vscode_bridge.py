#!/usr/bin/env python3
"""VSCode Extension ↔ WebSocket Bridge (IPC via filesystem).

VSCode 扩展没有原生的 WebSocket 接口，此脚本通过**文件系统 IPC** 桥接：

工作原理：
1. 本脚本作为 WebSocket 服务端（ACP Agent 角色）
2. VSCode 扩展监听同一个 IPC 目录，通过 JSON 文件交换消息
3. 桥接器把 WebSocket 消息写入 `.in` 文件，扩展读取后回复到 `.out` 文件

配套的 VSCode 扩展代码见 `vscode-bridge/` 目录。

Usage:
    # 1. 启动此桥接器
    python scripts/kimi_vscode_bridge.py

    # 2. 在 VSCode 中安装并启用配套扩展
    # 3. 在 VSCode 中按 Ctrl+Shift+P → "Kimi Bridge: Enable"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
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
logger = get_logger("vscode_bridge")

WS_HOST = "127.0.0.1"
WS_PORT = 8766  # 使用不同端口，可与 CLI Bridge 同时运行
IPC_DIR = Path(tempfile.gettempdir()) / "kimi_vscode_bridge"
IPC_DIR.mkdir(exist_ok=True)


class VSCodeBridgeServer:
    """Bridge between WebSocket and VSCode Extension via filesystem IPC."""

    def __init__(self) -> None:
        self._websocket: WebSocketServerProtocol | None = None
        self._sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # WebSocket handlers
    # ------------------------------------------------------------------ #

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        client_addr = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"  # type: ignore[index]
        logger.info("Bridge connected from %s", client_addr)
        self._websocket = websocket

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

                session_id = msg.session_id
                self._sessions.add(session_id)
                logger.info(
                    "[%s] action=%s text=%r",
                    session_id,
                    msg.action,
                    msg.payload.text,
                )

                if msg.action == "interrupt":
                    await self._write_ipc(session_id, "interrupt", msg.payload.text)
                    await self._send_ws(session_id, "interrupted", "任务已被打断")
                elif msg.action in ("user_input", "inject"):
                    await self._write_ipc(session_id, "input", msg.payload.text)
                    # Status updates will come from the poll loop
                    await self._send_ws(session_id, "thinking", "已转发到 VSCode...")

        except websockets.exceptions.ConnectionClosed:
            logger.info("Bridge disconnected")
        finally:
            self._websocket = None

    # ------------------------------------------------------------------ #
    # IPC filesystem helpers
    # ------------------------------------------------------------------ #

    async def _write_ipc(self, session_id: str, action: str, text: str) -> None:
        """Write an incoming message to the IPC directory for VSCode to read."""
        path = IPC_DIR / f"{session_id}.in"
        data = {
            "timestamp": time.time(),
            "session_id": session_id,
            "action": action,
            "text": text,
        }
        await asyncio.to_thread(path.write_text, json.dumps(data, ensure_ascii=False))
        logger.debug("Wrote IPC in-file: %s", path)

    async def _poll_ipc(self) -> None:
        """Background task: poll IPC directory for VSCode replies."""
        while True:
            try:
                await asyncio.sleep(0.5)
                if self._websocket is None or self._websocket.close_code is not None:
                    continue

                for path in list(IPC_DIR.glob("*.out")):
                    try:
                        text = await asyncio.to_thread(path.read_text, "utf-8")
                        data = json.loads(text)
                        session_id = data.get("session_id", "unknown")
                        status = data.get("status", "idle")
                        content = data.get("content", "")

                        await self._send_ws(session_id, status, content)
                        await asyncio.to_thread(path.unlink)
                        logger.debug("Processed IPC out-file: %s", path.name)
                    except Exception:
                        logger.exception("IPC poll error for %s", path)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Poll loop error")

    async def _send_ws(self, session_id: str, status: str, content: str) -> None:
        if self._websocket is None:
            return
        msg = DownstreamMessage(
            source="vscode-extension",
            session=SessionState(session_id=session_id, status=status),
            payload=Payload(
                type="status_update" if status != "idle" else "text",
                content=content,
            ),
        )
        try:
            await self._websocket.send(serialize_message(msg))
        except Exception:
            logger.exception("Failed to send downstream")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def run(self, port: int) -> None:
        logger.info("Starting VSCode Bridge on ws://%s:%d", WS_HOST, port)
        logger.info("IPC directory: %s", IPC_DIR)
        logger.info("Waiting for VSCode extension to write .out files...")

        self._poll_task = asyncio.create_task(self._poll_ipc(), name="ipc-poll")

        async with websockets.serve(self._handle_client, WS_HOST, port):
            logger.info("VSCode Bridge ready.")
            await asyncio.Future()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="VSCode Bridge")
    parser.add_argument("--port", type=int, default=WS_PORT)
    args = parser.parse_args()

    server = VSCodeBridgeServer()
    try:
        asyncio.run(server.run(port=args.port))
    except KeyboardInterrupt:
        logger.info("VSCode Bridge stopped.")


if __name__ == "__main__":
    main()
