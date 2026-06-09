#!/usr/bin/env python3
"""End-to-end integration test for ACP-QQ Bridge.

Tests the full flow without real QQ or real Agent:
- Starts a Mock Agent WebSocket server in a background task
- Initializes Bridge core components (SessionManager, Security, Persona, AgentWS)
- Registers a mock downstream handler to capture Agent replies
- Sends upstream messages and verifies:
    1. Agent receives the message
    2. Bridge correctly forwards it
    3. Downstream replies are received and processed
    4. Session state transitions (thinking -> executing -> idle)
    5. Interrupt signal works

Usage:
    python scripts/e2e_test.py
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
logger = get_logger("e2e_test")

MOCK_AGENT_URL = "ws://127.0.0.1:18765"
TEST_TIMEOUT = 10.0


class DownstreamCollector:
    """Collects downstream messages for assertion."""

    def __init__(self) -> None:
        self.messages: list[DownstreamMessage] = []
        self._event = asyncio.Event()

    async def handler(self, msg: DownstreamMessage) -> None:
        self.messages.append(msg)
        self._event.set()

    async def wait_for_messages(self, count: int, timeout: float = TEST_TIMEOUT) -> bool:
        """Wait until at least *count* messages have been collected."""
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


async def _start_mock_agent() -> asyncio.Task[None]:
    """Start the mock agent server in the background."""
    from mock_agent_server import start_server, stop_server

    await start_server()

    async def _monitor() -> None:
        await asyncio.Future()  # keep alive until cancelled

    task = asyncio.create_task(_monitor(), name="mock-agent")
    return task


async def _init_bridge() -> tuple[AgentWebSocketAdapter, SessionManager, DownstreamCollector]:
    """Initialize bridge components with test configuration."""
    config = BridgeConfig(
        agent=AgentConfig(
            ws_url=MOCK_AGENT_URL,
            token="",
            heartbeat_interval=5,
            reconnect_max_interval=5,
            response_timeout=5,
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
    security = SecurityEngine(
        allowed_commands=config.security.allowed_commands,
        sensitive_patterns=config.security.sensitive_patterns,
        enable_ast=config.security.enable_ast_audit,
    )
    personas = load_personas_from_dir(config.persona.personas_dir)
    persona = PersonaSkill(personas=personas, default=config.persona.default_persona)

    agent_ws = AgentWebSocketAdapter(config, session_manager)
    collector = DownstreamCollector()
    agent_ws.register_downstream_handler(collector.handler)

    await agent_ws.connect()
    # Wait for connection to establish
    for _ in range(50):
        if agent_ws.is_connected:
            break
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Failed to connect to mock agent within 5s")

    logger.info("Bridge connected to mock agent")
    return agent_ws, session_manager, collector


async def test_basic_message_flow(
    agent_ws: AgentWebSocketAdapter,
    session_manager: SessionManager,
    collector: DownstreamCollector,
) -> None:
    """Test: QQ user sends a message, Agent replies."""
    logger.info("=== TEST: Basic message flow ===")
    collector.reset()

    # Simulate creating a session and binding a QQ user
    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_12345", "private")
    logger.info("Created session %s for qq_12345", meta.session_id)

    # Send upstream message (simulating QQ -> Bridge -> Agent)
    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="Hello Agent"),
    )
    await agent_ws.send_message(upstream)
    logger.info("Sent upstream message")

    # Wait for replies: thinking + executing + final text = 3 messages
    ok = await collector.wait_for_messages(3, timeout=TEST_TIMEOUT)
    assert ok, f"Expected 3 downstream messages, got {len(collector.messages)}"

    statuses = [m.session.status for m in collector.messages]
    assert "thinking" in statuses, f"Expected 'thinking' status in {statuses}"
    assert "executing" in statuses, f"Expected 'executing' status in {statuses}"
    assert "idle" in statuses, f"Expected 'idle' status in {statuses}"

    # Final message should have text content
    final = [m for m in collector.messages if m.payload.type == "text"][-1]
    assert "收到消息: Hello Agent" in final.payload.content
    logger.info("✅ Basic message flow passed")


async def test_command_flow(
    agent_ws: AgentWebSocketAdapter,
    session_manager: SessionManager,
    collector: DownstreamCollector,
) -> None:
    """Test: QQ user sends a command, Agent processes it."""
    logger.info("=== TEST: Command flow ===")
    collector.reset()

    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_99999", "private")

    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="/status"),
    )
    await agent_ws.send_message(upstream)

    ok = await collector.wait_for_messages(3, timeout=TEST_TIMEOUT)
    assert ok, f"Expected 3 messages, got {len(collector.messages)}"

    final = [m for m in collector.messages if m.payload.type == "text"][-1]
    assert "收到指令 `/status`" in final.payload.content
    logger.info("✅ Command flow passed")


async def test_interrupt(
    agent_ws: AgentWebSocketAdapter,
    session_manager: SessionManager,
    collector: DownstreamCollector,
) -> None:
    """Test: Send interrupt signal to Agent."""
    logger.info("=== TEST: Interrupt signal ===")
    collector.reset()

    meta = await session_manager.create_session(agent_source="cli")
    await session_manager.bind_qq(meta.session_id, "qq_77777", "private")

    # First send a long-running request
    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="user_input",
        payload=UpstreamPayload(text="/longtask"),
    )
    await agent_ws.send_message(upstream)

    # Wait for all 3 replies (thinking + executing + final) to finish
    ok = await collector.wait_for_messages(3, timeout=TEST_TIMEOUT)
    assert ok, f"Expected 3 replies, got {len(collector.messages)}"

    # Now send interrupt
    await agent_ws.send_interrupt(meta.session_id)
    logger.info("Sent interrupt signal")

    # Wait for interrupt acknowledgment
    collector.reset()
    ok = await collector.wait_for_messages(1, timeout=TEST_TIMEOUT)
    assert ok, f"Expected interrupt reply, got {len(collector.messages)}"

    assert collector.messages[0].session.status == "interrupted"
    assert "打断" in collector.messages[0].payload.content
    logger.info("✅ Interrupt test passed")


async def test_security_rejection(
    agent_ws: AgentWebSocketAdapter,
    session_manager: SessionManager,
    security: SecurityEngine,
) -> None:
    """Test: Dangerous commands are rejected before reaching Agent."""
    logger.info("=== TEST: Security rejection ===")

    result = security.validate_command("rm -rf /home")
    assert not result.passed
    assert result.reason is not None
    logger.info("✅ Security rejection passed: %s", result.reason)


async def test_session_isolation(
    session_manager: SessionManager,
) -> None:
    """Test: Sessions are properly isolated."""
    logger.info("=== TEST: Session isolation ===")

    meta1 = await session_manager.create_session(agent_source="cli")
    meta2 = await session_manager.create_session(agent_source="vscode-extension")

    await session_manager.bind_qq(meta1.session_id, "qq_111", "private")
    await session_manager.bind_qq(meta2.session_id, "qq_222", "private")

    # Each QQ should map to its own session
    fetched1 = await session_manager.get_by_qq("qq_111")
    fetched2 = await session_manager.get_by_qq("qq_222")
    assert fetched1 is not None
    assert fetched2 is not None
    assert fetched1.session_id == meta1.session_id
    assert fetched2.session_id == meta2.session_id
    assert fetched1.session_id != fetched2.session_id
    logger.info("✅ Session isolation passed")


async def test_persona_transformation() -> None:
    """Test: Persona skill transforms text correctly."""
    logger.info("=== TEST: Persona transformation ===")

    skill = PersonaSkill()
    text = "Build completed successfully."

    geek = skill.transform(text, persona_id="geek")
    assert isinstance(geek, str)
    logger.info("Geek persona: %s", geek)

    cute = skill.transform(text, persona_id="cute")
    assert isinstance(cute, str)
    logger.info("Cute persona: %s", cute)

    # ACP tags should be preserved
    tagged = 'Result: <acp:cmd>secret</acp:cmd> Done.'
    result = skill.transform(tagged, persona_id="sarcastic")
    assert "<acp:cmd>secret</acp:cmd>" in result
    logger.info("✅ Persona transformation passed")


async def run_all_tests() -> None:
    """Orchestrate the full e2e test suite."""
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║     ACP-QQ Bridge E2E Test Suite         ║")
    logger.info("╚══════════════════════════════════════════╝")

    # Start mock agent
    mock_task = await _start_mock_agent()
    agent_ws: AgentWebSocketAdapter | None = None
    session_manager: SessionManager | None = None

    try:
        agent_ws, session_manager, collector = await _init_bridge()
        security = SecurityEngine(
            allowed_commands=["ls", "cat", "echo", "python"],
            sensitive_patterns=["rm -rf"],
            enable_ast=True,
        )

        # Run tests
        await test_basic_message_flow(agent_ws, session_manager, collector)
        await test_command_flow(agent_ws, session_manager, collector)
        await test_interrupt(agent_ws, session_manager, collector)
        await test_security_rejection(agent_ws, session_manager, security)
        await test_session_isolation(session_manager)
        await test_persona_transformation()

        logger.info("")
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║     ✅ ALL E2E TESTS PASSED              ║")
        logger.info("╚══════════════════════════════════════════╝")

    finally:
        logger.info("Cleaning up...")
        if agent_ws is not None:
            await agent_ws.disconnect()
        if session_manager is not None:
            session_manager.stop_cleanup_task()
        from mock_agent_server import stop_server
        await stop_server()
        mock_task.cancel()
        try:
            await mock_task
        except asyncio.CancelledError:
            pass
        logger.info("Cleanup complete.")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
