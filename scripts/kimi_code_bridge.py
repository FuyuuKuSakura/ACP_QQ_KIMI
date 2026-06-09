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
        self.persona_prompt: str = ""
        self._lock = asyncio.Lock()

    def set_work_dir(self, path: Path) -> None:
        """Switch the working directory for subsequent kimi CLI calls."""
        self.work_dir = path
        logger.info("[%s] Work dir switched to %s", self.qq_session_id, path)

    def set_kimi_session_id(self, sid: str) -> None:
        """Bind to an existing Kimi session ID."""
        self.kimi_session_id = sid
        logger.info("[%s] Kimi session ID set to %s", self.qq_session_id, sid)

    def set_persona_prompt(self, prompt: str) -> None:
        """Set the persona system prompt injected into every LLM call."""
        self.persona_prompt = prompt
        logger.info(
            "[%s] Persona prompt set (%d chars)", self.qq_session_id, len(prompt)
        )

    async def summarize(self) -> str:
        """Ask kimi to summarize the current session progress."""
        return await self.ask(
            "请总结一下当前会话到目前为止的进度，包括已经做了什么、当前的计划和下一步行动。"
        )

    async def ask(self, text: str) -> str:
        """Send a message to kimi CLI and return the assistant reply."""
        async with self._lock:
            # Inject persona prompt if set
            if self.persona_prompt:
                full_text = (
                    f"{self.persona_prompt}\n\n"
                    f"[Current Turn]\n"
                    f"用户: {text}\n"
                    f"角色:"
                )
            else:
                full_text = text

            # Build command
            cmd = [
                KIMI_BIN,
                "-p", full_text,
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

    def _read_session_index(self) -> list[dict[str, Any]]:
        """Parse ~/.kimi-code/session_index.jsonl into a list of session records."""
        index_path = Path.home() / ".kimi-code" / "session_index.jsonl"
        sessions: list[dict[str, Any]] = []
        if not index_path.exists():
            return sessions
        with open(index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    sessions.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return sessions

    def _find_session_work_dir(self, session_id: str) -> Path | None:
        """Look up the workDir for a given Kimi session ID."""
        for s in self._read_session_index():
            if s.get("sessionId") == session_id:
                wd = s.get("workDir")
                if wd:
                    return Path(wd)
        return None

    def _list_kimi_sessions(self) -> str:
        """Read ~/.kimi-code/session_index.jsonl and format session list."""
        sessions = self._read_session_index()
        if not sessions:
            return "暂无 Kimi 历史会话记录。"

        # Keep latest session per workDir
        latest_by_dir: dict[str, dict[str, Any]] = {}
        for s in sessions:
            wd = s.get("workDir", "未知")
            sid = s.get("sessionId", "未知")
            if wd not in latest_by_dir or sid > latest_by_dir[wd].get("sessionId", ""):
                latest_by_dir[wd] = s

        lines = ["📋 Kimi 历史会话列表：", ""]
        for i, (wd, s) in enumerate(sorted(latest_by_dir.items()), 1):
            sid = s.get("sessionId", "未知")
            display_wd = wd.replace(str(Path.home()), "~")
            lines.append(f"{i}. {display_wd}")
            lines.append(f"   ID: `{sid}`")
            lines.append("")

        return "\n".join(lines)

    async def _summarize_and_send(
        self,
        websocket: WebSocketServerProtocol,
        session_id: str,
        kimi_session: KimiSession,
    ) -> None:
        """Trigger session summary and send result back."""
        if not kimi_session.kimi_session_id:
            await self._send(
                websocket, session_id, "idle",
                "⚠️ 当前没有绑定的 Kimi 会话，无法总结。"
            )
            return

        await self._send(websocket, session_id, "thinking", "正在总结会话进度...")
        try:
            summary = await kimi_session.summarize()
            await self._send(
                websocket, session_id, "idle",
                f"📋 会话进度总结：\n\n{summary}"
            )
        except Exception as exc:
            logger.exception("Summarize failed")
            await self._send(
                websocket, session_id, "idle", f"总结失败: {exc}"
            )

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

        # Get or create kimi session
        try:
            kimi_session = await self._get_session(session_id)
        except Exception as exc:
            logger.exception("[%s] Failed to get/create session", session_id)
            await self._send(websocket, session_id, "idle", f"会话初始化失败: {exc}")
            return

        # Apply control parameters from payload
        if msg.payload.work_dir:
            kimi_session.set_work_dir(Path(msg.payload.work_dir))
        if msg.payload.kimi_session_id:
            kimi_session.set_kimi_session_id(msg.payload.kimi_session_id)

        # Handle control commands (inject action)
        if msg.action == "inject":
            if text == "__LIST_SESSIONS__":
                reply = self._list_kimi_sessions()
                await self._send(websocket, session_id, "idle", reply)
                return
            elif text == "__USE_SESSION__":
                # Kimi CLI requires the session to be resumed in its original workDir.
                # Look it up from the session index and switch before summarizing.
                if msg.payload.kimi_session_id:
                    original_wd = self._find_session_work_dir(msg.payload.kimi_session_id)
                    if original_wd:
                        kimi_session.set_work_dir(original_wd)
                        logger.info(
                            "[%s] Auto-switched workDir to %s for session %s",
                            session_id, original_wd, msg.payload.kimi_session_id,
                        )
                    else:
                        logger.warning(
                            "[%s] Could not find workDir for session %s",
                            session_id, msg.payload.kimi_session_id,
                        )
                await self._summarize_and_send(websocket, session_id, kimi_session)
                return
            elif text == "__CD__":
                await self._send(
                    websocket, session_id, "idle",
                    f"✅ 工作目录已切换至: `{kimi_session.work_dir}`"
                )
                return
            elif text == "__SET_PERSONA__":
                # Payload.text may carry the persona prompt
                persona_prompt = msg.payload.raw_signal or ""
                if persona_prompt:
                    kimi_session.set_persona_prompt(persona_prompt)
                    await self._send(
                        websocket, session_id, "idle",
                        "✅ 角色设定已加载，后续对话将以此角色语气回复。"
                    )
                else:
                    kimi_session.set_persona_prompt("")
                    await self._send(
                        websocket, session_id, "idle",
                        "✅ 角色设定已清除，恢复默认语气。"
                    )
                return

        # Normal message flow
        await self._send(websocket, session_id, "thinking", "正在分析问题...")

        try:
            await self._send(websocket, session_id, "executing", "正在调用 Kimi Code...")
            reply = await kimi_session.ask(text)
            await self._send(websocket, session_id, "idle", reply)
        except Exception as exc:
            logger.exception("[%s] Processing error", session_id)
            await self._send(websocket, session_id, "idle", f"处理出错: {exc}")

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
