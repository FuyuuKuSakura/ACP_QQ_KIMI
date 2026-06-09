#!/usr/bin/env python3
"""OpenAI-compatible API Agent Server (Fallback).

当 Kimi Code CLI 无法直接桥接时，此脚本作为替代 Agent：
- 直接调用 Moonshot / OpenAI API
- 暴露与 Kimi Code CLI Bridge 相同的 ACP WebSocket 接口
- 支持代码分析、文件操作等能力（通过 System Prompt 模拟）

Usage:
    export MOONSHOT_API_KEY="sk-xxx"
    python scripts/openai_agent_server.py

Requirements:
    pip install openai
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

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
logger = get_logger("openai_agent")

WS_HOST = "127.0.0.1"
WS_PORT = 8765

SYSTEM_PROMPT = """你是一个专业的编程助手，通过 QQ 与用户交互。

当前能力：
- 分析和解释代码
- 提供重构建议
- 编写测试用例
- 回答编程问题
- 执行 Shell 命令（仅在安全白名单内）

输出规则：
1. 使用 Markdown 格式
2. 代码块标注语言
3. 复杂操作分步骤说明
4. 危险操作必须警告用户
"""


class SessionContext:
    """Per-session conversation context for the API agent."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        self._lock = asyncio.Lock()

    async def add_user(self, text: str) -> None:
        async with self._lock:
            self.messages.append({"role": "user", "content": text})

    async def add_assistant(self, text: str) -> None:
        async with self._lock:
            self.messages.append({"role": "assistant", "content": text})

    def get_messages(self) -> list[dict[str, str]]:
        return list(self.messages)


class OpenAIAgentServer:
    """WebSocket server backed by OpenAI-compatible API."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._sessions: dict[str, SessionContext] = {}
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazy import and initialize OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                logger.error("openai package not installed. Run: pip install openai")
                raise
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    async def _call_api(self, session: SessionContext) -> str:
        """Call the LLM API with the session context."""
        client = self._get_client()
        try:
            resp = await client.chat.completions.create(
                model=self.model,
                messages=session.get_messages(),
                temperature=0.3,
                max_tokens=4096,
            )
            return resp.choices[0].message.content or "（无返回内容）"
        except Exception as exc:
            logger.exception("API call failed")
            return f"调用 API 出错: {exc}"

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

                session_id = msg.session_id
                logger.info(
                    "[%s] action=%s text=%r",
                    session_id,
                    msg.action,
                    msg.payload.text,
                )

                if msg.action == "interrupt":
                    await self._send(
                        websocket, session_id, "interrupted", "任务已被打断。"
                    )
                    continue

                if msg.action not in ("user_input", "inject"):
                    continue

                # Get or create session context
                if session_id not in self._sessions:
                    self._sessions[session_id] = SessionContext(session_id)
                ctx = self._sessions[session_id]

                # Add user message
                await ctx.add_user(msg.payload.text)

                # Send thinking status
                await self._send(
                    websocket, session_id, "thinking", "正在分析问题..."
                )
                await asyncio.sleep(0.3)

                # Call API
                await self._send(
                    websocket, session_id, "executing", "正在调用模型..."
                )
                reply = await self._call_api(ctx)

                # Add assistant message to context
                await ctx.add_assistant(reply)

                # Send final reply
                await self._send(
                    websocket,
                    session_id,
                    "idle",
                    reply,
                )

        except websockets.exceptions.ConnectionClosed:
            logger.info("Bridge disconnected")
        except Exception:
            logger.exception("Handler error")

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
        logger.info("Starting OpenAI Agent on ws://%s:%d", WS_HOST, port)
        logger.info("Model: %s | Base URL: %s", self.model, self.base_url)
        async with websockets.serve(self._handle_client, WS_HOST, port):
            logger.info("Agent ready. Waiting for bridge connection...")
            await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI-compatible API Agent")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("MOONSHOT_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        help="API key (or set MOONSHOT_API_KEY env var)",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.moonshot.cn/v1",
        help="API base URL",
    )
    parser.add_argument(
        "--model",
        default="moonshot-v1-8k",
        help="Model name",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=WS_PORT,
        help="WebSocket listen port",
    )
    args = parser.parse_args()

    if not args.api_key:
        logger.error(
            "API key not provided.\n"
            "Set MOONSHOT_API_KEY environment variable or use --api-key.\n"
            "Get your key from: https://platform.moonshot.cn/"
        )
        sys.exit(1)

    server = OpenAIAgentServer(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    try:
        asyncio.run(server.run(port=args.port))
    except KeyboardInterrupt:
        logger.info("Agent stopped.")


if __name__ == "__main__":
    main()
