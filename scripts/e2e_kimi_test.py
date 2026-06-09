#!/usr/bin/env python3
"""End-to-end test using REAL Kimi Code CLI as the Agent.

验证完整链路：
    QQ Message → ACP Bridge → Kimi Code Bridge → Kimi CLI → Moonshot API
    → Kimi CLI → Kimi Code Bridge → ACP Bridge → QQ Reply

Usage:
    python scripts/e2e_kimi_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from acp_qq_bridge.adapters.agent_ws import AgentWebSocketAdapter
from acp_qq_bridge.config import AgentConfig, BridgeConfig, PersonaConfig, QQConfig, SecurityConfig
from acp_qq_bridge.core.protocol import (
    DownstreamMessage,
    UpstreamMessage,
    UpstreamPayload,
)
from acp_qq_bridge.core.runtime import SessionManager
from acp_qq_bridge.core.security import SecurityEngine
from acp_qq_bridge.middleware.persona import PersonaSkill, load_personas_from_dir
from acp_qq_bridge.utils.logger import configure_logging, get_logger

configure_logging()
logger = get_logger("e2e_kimi_test")

WS_PORT = 8765
TEST_TIMEOUT = 60.0  # kimi API 可能需要几秒钟


class DownstreamCollector:
    def __init__(self) -> None:
        self.messages: list[DownstreamMessage] = []
        self._event = asyncio.Event()

    async def handler(self, msg: DownstreamMessage) -> None:
        self.messages.append(msg)
        self._event.set()

    async def wait_for_messages(self, count: int, timeout: float = TEST_TIMEOUT) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(self.messages) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
        return True

    def reset(self) -> None:
        self.messages.clear()
        self._event.clear()


async def _start_kimi_bridge() -> asyncio.Task[None]:
    """Start the real Kimi Code Bridge in background."""
    from kimi_code_bridge import start_server, stop_server

    await start_server(port=WS_PORT)

    async def _monitor() -> None:
        await asyncio.Future()

    task = asyncio.create_task(_monitor(), name="kimi-bridge")
    return task


async def _init_bridge() -> tuple[AgentWebSocketAdapter, SessionManager, DownstreamCollector]:
    config = BridgeConfig(
        agent=AgentConfig(
            ws_url=f"ws://127.0.0.1:{WS_PORT}",
            token="",
            heartbeat_interval=5,
            reconnect_max_interval=5,
            response_timeout=TEST_TIMEOUT,
        ),
        qq=QQConfig(
            command_prefixes=["/", "!"],
            superusers=[],
            session_ttl=3600,
        ),
        security=SecurityConfig(
            enable_ast_audit=True,
            enable_sensitive_filter=True,
            allowed_commands=["ls", "cat", "echo", "python"],
            sensitive_patterns=["rm -rf"],
            max_message_length=4096,
        ),
        persona=PersonaConfig(
            default_persona="assistant",
            personas_dir="./personas",
        ),
    )

    session_manager = SessionManager(ttl=3600)
    personas = load_personas_from_dir(config.persona.personas_dir)
    persona = PersonaSkill(personas=personas, default=config.persona.default_persona)

    agent_ws = AgentWebSocketAdapter(config, session_manager)
    collector = DownstreamCollector()
    agent_ws.register_downstream_handler(collector.handler)

    await agent_ws.connect()
    for _ in range(50):
        if agent_ws.is_connected:
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Failed to connect to Kimi Bridge within 5s")

    logger.info("Connected to Kimi Code Bridge")
    return agent_ws, session_manager, collector


async def test_basic_math(agent_ws, session_manager, collector) -> None:
    logger.info("=== TEST 1: Basic math question ===")
    collector.reset()

    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_test_1", "private")

    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="1+1等于几？请只回答数字"),
    )
    await agent_ws.send_message(upstream)

    ok = await collector.wait_for_messages(3)  # thinking + executing + idle
    assert ok, f"Expected 3 messages, got {len(collector.messages)}"

    final = [m for m in collector.messages if m.payload.type == "text"][-1]
    assert "2" in final.payload.content, f"Expected '2' in reply, got: {final.payload.content}"
    logger.info("✅ Basic math passed: %s", final.payload.content[:50])


async def test_code_analysis(agent_ws, session_manager, collector) -> None:
    logger.info("=== TEST 2: Code analysis ===")
    collector.reset()

    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_test_2", "private")

    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="用 Python 写一个快速排序，只输出代码"),
    )
    await agent_ws.send_message(upstream)

    ok = await collector.wait_for_messages(3)
    assert ok, f"Expected 3 messages, got {len(collector.messages)}"

    final = [m for m in collector.messages if m.payload.type == "text"][-1]
    content = final.payload.content
    assert "def" in content or "sort" in content, f"Expected code in reply, got: {content[:200]}"
    logger.info("✅ Code analysis passed (len=%d)", len(content))


async def test_session_memory(agent_ws, session_manager, collector) -> None:
    logger.info("=== TEST 3: Session memory (context continuity) ===")
    collector.reset()

    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_test_3", "private")

    # First message
    upstream1 = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="我叫张三，请记住"),
    )
    await agent_ws.send_message(upstream1)
    ok = await collector.wait_for_messages(3)
    assert ok
    collector.reset()

    # Second message (should remember)
    upstream2 = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="我叫什么名字？"),
    )
    await agent_ws.send_message(upstream2)
    ok = await collector.wait_for_messages(3)
    assert ok

    final = [m for m in collector.messages if m.payload.type == "text"][-1]
    assert "张三" in final.payload.content, f"Expected '张三' in reply, got: {final.payload.content}"
    logger.info("✅ Session memory passed: %s", final.payload.content[:50])


async def run_all_tests() -> None:
    logger.info("╔══════════════════════════════════════════════════════╗")
    logger.info("║  E2E Test with REAL Kimi Code CLI                    ║")
    logger.info("╚══════════════════════════════════════════════════════╝")

    mock_task = await _start_kimi_bridge()
    agent_ws: AgentWebSocketAdapter | None = None
    session_manager: SessionManager | None = None

    try:
        agent_ws, session_manager, collector = await _init_bridge()

        await test_basic_math(agent_ws, session_manager, collector)
        await test_code_analysis(agent_ws, session_manager, collector)
        await test_session_memory(agent_ws, session_manager, collector)

        logger.info("")
        logger.info("╔══════════════════════════════════════════════════════╗")
        logger.info("║  ✅ ALL KIMI E2E TESTS PASSED                        ║")
        logger.info("╚══════════════════════════════════════════════════════╝")

    finally:
        logger.info("Cleaning up...")
        if agent_ws is not None:
            await agent_ws.disconnect()
        if session_manager is not None:
            session_manager.stop_cleanup_task()
        from kimi_code_bridge import stop_server
        await stop_server()
        mock_task.cancel()
        try:
            await mock_task
        except asyncio.CancelledError:
            pass
        logger.info("Cleanup complete.")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
