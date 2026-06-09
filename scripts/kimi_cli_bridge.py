#!/usr/bin/env python3
"""Kimi Code CLI ↔ WebSocket Bridge Agent.

将本地运行的 Kimi Code CLI 进程桥接到 ACP WebSocket 协议，
使 QQ 用户能够通过 ACP-QQ-Bridge 与 Kimi Code CLI 交互。

支持两种模式：
    - 单例模式 (singleton): 一个 CLI 进程服务所有会话（消息串行处理）
    - 多例模式 (multi): 每个 QQ 会话启动独立的 CLI 进程

Usage:
    # 单例模式（默认）
    python scripts/kimi_cli_bridge.py

    # 多例模式
    python scripts/kimi_cli_bridge.py --mode multi

    # 指定自定义 CLI 命令
    python scripts/kimi_cli_bridge.py --cmd "kimi chat"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import tempfile
from collections import defaultdict
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
logger = get_logger("kimi_cli_bridge")

WS_HOST = "127.0.0.1"
WS_PORT = 8765

# ---------------------------------------------------------------------------
# CLI Process Manager
# ---------------------------------------------------------------------------


class CliProcess:
    """Wrapper around a single Kimi Code CLI subprocess."""

    def __init__(self, cmd: list[str], session_id: str) -> None:
        self.session_id = session_id
        self.cmd = cmd
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._buffer: list[str] = []
        self._read_task: asyncio.Task[None] | None = None
        self._ready_event = asyncio.Event()

    async def start(self) -> None:
        """Start the CLI subprocess."""
        logger.info(
            "[%s] Starting CLI: %s",
            self.session_id,
            " ".join(shlex.quote(c) for c in self.cmd),
        )
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()),
        )
        self._read_task = asyncio.create_task(
            self._read_loop(),
            name=f"cli-reader-{self.session_id}",
        )
        # Give CLI time to initialize
        await asyncio.sleep(1.0)
        self._ready_event.set()
        logger.info("[%s] CLI started (pid=%s)", self.session_id, self._proc.pid)

    async def stop(self) -> None:
        """Gracefully terminate the CLI subprocess."""
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        logger.info("[%s] CLI stopped", self.session_id)

    async def send_input(self, text: str) -> None:
        """Send text to CLI stdin."""
        await self._ready_event.wait()
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("CLI not running")
        async with self._lock:
            line = text.strip() + "\n"
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
            logger.debug("[%s] Sent to CLI: %r", self.session_id, text)

    async def read_output(self, timeout: float = 10.0) -> str:
        """Read accumulated output with timeout."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            async with self._lock:
                if self._buffer:
                    out = "\n".join(self._buffer)
                    self._buffer.clear()
                    return out
            await asyncio.sleep(0.2)
        return ""

    async def _read_loop(self) -> None:
        """Background task: read CLI stdout/stderr."""
        if self._proc is None:
            return
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None

        async def _read_stream(stream: asyncio.StreamReader, tag: str) -> None:
            while True:
                try:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        async with self._lock:
                            self._buffer.append(f"[{tag}] {text}")
                except Exception:
                    break

        await asyncio.gather(
            _read_stream(self._proc.stdout, "out"),
            _read_stream(self._proc.stderr, "err"),
        )
        logger.info("[%s] CLI read loop ended", self.session_id)


# ---------------------------------------------------------------------------
# WebSocket Server
# ---------------------------------------------------------------------------


class CliBridgeServer:
    """WebSocket server that bridges ACP messages to CLI processes."""

    def __init__(self, cmd: list[str], mode: str = "singleton") -> None:
        self.cmd = cmd
        self.mode = mode
        self._sessions: dict[str, CliProcess] = {}
        self._lock = asyncio.Lock()
        self._singleton: CliProcess | None = None

    async def _get_or_create_process(self, session_id: str) -> CliProcess:
        """Return a CLI process for the given session."""
        if self.mode == "singleton":
            if self._singleton is None:
                self._singleton = CliProcess(self.cmd, "singleton")
                await self._singleton.start()
            return self._singleton

        async with self._lock:
            if session_id not in self._sessions:
                proc = CliProcess(self.cmd, session_id)
                await proc.start()
                self._sessions[session_id] = proc
            return self._sessions[session_id]

    async def _cleanup_session(self, session_id: str) -> None:
        """Stop and remove a CLI process."""
        if self.mode == "singleton":
            return
        async with self._lock:
            proc = self._sessions.pop(session_id, None)
            if proc is not None:
                await proc.stop()

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a single WebSocket client (the ACP-QQ-Bridge)."""
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

                logger.info(
                    "[%s] action=%s text=%r",
                    msg.session_id,
                    msg.action,
                    msg.payload.text,
                )

                if msg.action == "interrupt":
                    await self._handle_interrupt(websocket, msg)
                elif msg.action == "user_input":
                    await self._handle_user_input(websocket, msg)
                elif msg.action == "inject":
                    await self._handle_user_input(websocket, msg)

        except websockets.exceptions.ConnectionClosed:
            logger.info("Bridge disconnected from %s", client_addr)
        except Exception:
            logger.exception("Handler error")

    async def _handle_user_input(
        self, websocket: WebSocketServerProtocol, msg: UpstreamMessage
    ) -> None:
        """Forward user input to CLI and stream back the output."""
        session_id = msg.session_id

        # Send "thinking" status
        await self._send_downstream(
            websocket, session_id, "thinking", "正在分析输入..."
        )

        try:
            proc = await self._get_or_create_process(session_id)
        except Exception as exc:
            logger.exception("Failed to start CLI")
            await self._send_downstream(
                websocket, session_id, "idle", f"启动 CLI 失败: {exc}"
            )
            return

        # Send input to CLI
        await proc.send_input(msg.payload.text)

        # Send "executing" status
        await self._send_downstream(
            websocket, session_id, "executing", "正在执行..."
        )

        # Read output with progressive timeout
        output = ""
        for attempt in range(5):
            chunk = await proc.read_output(timeout=2.0)
            if chunk:
                output += chunk + "\n"
                # Stream partial progress
                await self._send_downstream(
                    websocket, session_id, "executing", chunk[:500]
                )
            else:
                break

        if not output.strip():
            output = "（CLI 未返回输出，可能命令正在后台运行或需要交互确认）"

        # Final reply
        await self._send_downstream(
            websocket,
            session_id,
            "idle",
            output.strip(),
        )

    async def _handle_interrupt(
        self, websocket: WebSocketServerProtocol, msg: UpstreamMessage
    ) -> None:
        """Handle interrupt by terminating the CLI process."""
        session_id = msg.session_id
        logger.info("[%s] Interrupt requested", session_id)

        if self.mode == "singleton" and self._singleton is not None:
            # Send Ctrl+C to singleton process
            if self._singleton._proc is not None:
                self._singleton._proc.send_signal(
                    getattr(__import__("signal"), "SIGINT")
                )
        else:
            async with self._lock:
                proc = self._sessions.pop(session_id, None)
                if proc is not None:
                    await proc.stop()

        await self._send_downstream(
            websocket,
            session_id,
            "interrupted",
            "任务已被用户打断。",
        )

    async def _send_downstream(
        self,
        websocket: WebSocketServerProtocol,
        session_id: str,
        status: str,
        content: str,
    ) -> None:
        """Send a DownstreamMessage to the bridge."""
        msg = DownstreamMessage(
            source="cli",
            session=SessionState(session_id=session_id, status=status),
            payload=Payload(type="status_update" if status != "idle" else "text", content=content),
        )
        try:
            await websocket.send(serialize_message(msg))
        except Exception:
            logger.exception("Failed to send downstream")

    async def run(self) -> None:
        """Start the WebSocket server."""
        logger.info("Starting CLI Bridge on ws://%s:%d", WS_HOST, WS_PORT)
        logger.info("Mode: %s | Command: %s", self.mode, self.cmd)
        async with websockets.serve(self._handle_client, WS_HOST, WS_PORT):
            logger.info("CLI Bridge ready. Waiting for ACP-QQ-Bridge connection...")
            await asyncio.Future()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _detect_kimi_cli() -> list[str] | None:
    """Attempt to detect Kimi Code CLI in PATH."""
    candidates = [
        "kimi",
        "kimi-code",
        "kk",
    ]
    for c in candidates:
        import shutil
        if shutil.which(c):
            return [c]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimi Code CLI Bridge")
    parser.add_argument(
        "--cmd",
        default="",
        help='CLI command to bridge, e.g. "kimi chat" or "python -i"',
    )
    parser.add_argument(
        "--mode",
        choices=["singleton", "multi"],
        default="singleton",
        help="Process mode: singleton=one CLI for all, multi=one CLI per session",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=WS_PORT,
        help="WebSocket listen port",
    )
    args = parser.parse_args()

    if args.cmd:
        cmd = shlex.split(args.cmd)
    else:
        detected = _detect_kimi_cli()
        if detected:
            cmd = detected
            logger.info("Auto-detected CLI: %s", cmd[0])
        else:
            logger.error(
                "Kimi Code CLI not found in PATH.\n"
                "Please install it or specify --cmd manually.\n"
                "Examples:\n"
                "  python scripts/kimi_cli_bridge.py --cmd 'kimi chat'\n"
                "  python scripts/kimi_cli_bridge.py --cmd 'python -i'\n"
            )
            sys.exit(1)

    server = CliBridgeServer(cmd=cmd, mode=args.mode)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("CLI Bridge stopped.")


if __name__ == "__main__":
    main()
