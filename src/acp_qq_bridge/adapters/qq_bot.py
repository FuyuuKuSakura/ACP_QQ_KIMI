"""QQ bot adapter based on NoneBot2 and OneBot v11.

Defines message/command matchers and bridges QQ interactions to the
ACP agent via the :class:`AgentWebSocketAdapter`.
"""

from __future__ import annotations

import random
from typing import Any, Literal

from nonebot import get_bot, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.exception import ActionFailed
from nonebot.params import CommandArg
from nonebot.rule import to_me

from acp_qq_bridge.adapters.agent_ws import AgentWebSocketAdapter
from acp_qq_bridge.config import BridgeConfig
from acp_qq_bridge.core.protocol import (
    DownstreamMessage,
    UpstreamMessage,
    UpstreamPayload,
)
from acp_qq_bridge.core.runtime import SessionManager
from acp_qq_bridge.core.security import SecurityEngine
from acp_qq_bridge.middleware.persona import PersonaSkill
from acp_qq_bridge.utils.logger import get_logger

logger = get_logger(__name__)

# Module-level state populated by :func:`init_qq_bot`.
_bridge_config: BridgeConfig | None = None
_session_manager: SessionManager | None = None
_security_engine: SecurityEngine | None = None
_persona_skill: PersonaSkill | None = None
_agent_ws: AgentWebSocketAdapter | None = None

# Per-session persona overrides: session_id -> persona_id
_session_personas: dict[str, str] = {}


def _get_qq_id_and_type(event: MessageEvent) -> tuple[str, Literal["private", "group"]]:
    """Extract QQ identifier and chat type from a message event.

    Args:
        event: The OneBot v11 message event.

    Returns:
        A tuple of ``(qq_id, qq_type)`` where *qq_type* is ``"private"`` or
        ``"group"``.
    """
    if isinstance(event, GroupMessageEvent):
        return str(event.group_id), "group"
    return str(event.user_id), "private"


async def _ensure_session(qq_id: str, qq_type: Literal["private", "group"]) -> str:
    """Get or create a session bound to the given QQ context.

    Args:
        qq_id: QQ user or group ID.
        qq_type: ``"private"`` or ``"group"``.

    Returns:
        The active ``session_id``.
    """
    assert _session_manager is not None
    meta = await _session_manager.get_by_qq(qq_id)
    if meta is not None:
        return meta.session_id

    meta = await _session_manager.create_session("cli")
    await _session_manager.bind_qq(meta.session_id, qq_id, qq_type)
    logger.info(
        "Auto-created session %s for QQ %s (%s)",
        meta.session_id,
        qq_id,
        qq_type,
    )
    return meta.session_id


def init_qq_bot(
    config: BridgeConfig,
    session_manager: SessionManager,
    security: SecurityEngine,
    persona: PersonaSkill,
    agent_ws: AgentWebSocketAdapter,
) -> None:
    """Initialise NoneBot2 matchers and register downstream handler.

    This function sets up:

    * Generic message handler (triggered by @bot or private chat).
    * ``stop`` / ``status`` / ``persona`` commands.
    * Downstream message routing from agent to QQ.

    Args:
        config: Bridge configuration.
        session_manager: Session manager for ACP sessions.
        security: Security engine for input validation.
        persona: Persona skill for text transformation.
        agent_ws: Agent WebSocket adapter.
    """
    global _bridge_config, _session_manager, _security_engine, _persona_skill, _agent_ws
    _bridge_config = config
    _session_manager = session_manager
    _security_engine = security
    _persona_skill = persona
    _agent_ws = agent_ws

    agent_ws.register_downstream_handler(handle_downstream)
    logger.info("QQ bot initialised")


# ------------------------------------------------------------------ #
# Matchers
# ------------------------------------------------------------------ #

_message_matcher = on_message(rule=to_me(), priority=10, block=False)
_stop_matcher = on_command("stop", aliases={"打断", "停止"}, priority=5, block=True)
_status_matcher = on_command("status", aliases={"状态"}, priority=5, block=True)
_persona_matcher = on_command("persona", aliases={"人设"}, priority=5, block=True)


@_message_matcher.handle()
async def _handle_message(bot: Bot, event: MessageEvent) -> None:
    """Handle incoming QQ messages routed to the bot.

    - Creates or reuses a bound session.
    - Performs security validation.
    - Forwards the message to the ACP agent.
    """
    assert _session_manager is not None
    assert _security_engine is not None
    assert _agent_ws is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    raw_text = event.get_plaintext()

    # Skip empty messages
    if not raw_text.strip():
        return

    session_id = await _ensure_session(qq_id, qq_type)

    # Security check
    sec_result = _security_engine.validate_command(raw_text)
    if not sec_result.passed:
        reply = f"⚠️ 安全警告: {sec_result.reason}"
        await _send_qq_message(bot, qq_id, qq_type, reply)
        _security_engine.log_event(
            qq_id=qq_id,
            raw_command=raw_text,
            result=sec_result,
        )
        return

    upstream = UpstreamMessage(
        session_id=session_id,
        action="user_input",
        payload=UpstreamPayload(text=raw_text),
    )

    await _session_manager.update_last_active(session_id)
    await _agent_ws.send_message(upstream)
    logger.debug("Forwarded message from QQ %s to session %s", qq_id, session_id)


@_stop_matcher.handle()
async def _handle_stop(bot: Bot, event: MessageEvent) -> None:
    """Handle the stop / interrupt command."""
    assert _session_manager is not None
    assert _agent_ws is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    meta = await _session_manager.get_by_qq(qq_id)
    if meta is None:
        await _send_qq_message(bot, qq_id, qq_type, "❌ 当前没有活跃会话。")
        return

    await _session_manager.interrupt_session(meta.session_id)
    await _agent_ws.send_interrupt(meta.session_id)
    await _send_qq_message(bot, qq_id, qq_type, "⏹️ 已发送打断信号。")
    logger.info("Interrupt sent for session %s (QQ %s)", meta.session_id, qq_id)


@_status_matcher.handle()
async def _handle_status(bot: Bot, event: MessageEvent) -> None:
    """Handle the status command."""
    assert _session_manager is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    meta = await _session_manager.get_by_qq(qq_id)
    if meta is None:
        await _send_qq_message(bot, qq_id, qq_type, "ℹ️ 当前没有活跃会话。")
        return

    sessions = await _session_manager.list_sessions()
    reply = (
        f"会话 ID: {meta.session_id}\n"
        f"状态: {meta.status}\n"
        f"来源: {meta.agent_source}\n"
        f"上下文长度: {len(meta.context)}\n"
        f"全局活跃会话数: {len(sessions)}"
    )
    await _send_qq_message(bot, qq_id, qq_type, reply)


@_persona_matcher.handle()
async def _handle_persona(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """Handle the persona switch command."""
    assert _persona_skill is not None
    assert _session_manager is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    meta = await _session_manager.get_by_qq(qq_id)
    if meta is None:
        await _send_qq_message(
            bot,
            qq_id,
            qq_type,
            "❌ 当前没有活跃会话，无法切换人设。",
        )
        return

    persona_arg = args.extract_plain_text().strip()
    available = _persona_skill.get_available_personas()

    if not persona_arg:
        await _send_qq_message(
            bot,
            qq_id,
            qq_type,
            f"当前可用的人设: {', '.join(available)}",
        )
        return

    if persona_arg not in available:
        await _send_qq_message(
            bot,
            qq_id,
            qq_type,
            f"❌ 未知人设 '{persona_arg}'。可用: {', '.join(available)}",
        )
        return

    _session_personas[meta.session_id] = persona_arg
    await _send_qq_message(bot, qq_id, qq_type, f"✅ 人设已切换为: {persona_arg}")


# ------------------------------------------------------------------ #
# Downstream handler
# ------------------------------------------------------------------ #


async def handle_downstream(msg: DownstreamMessage) -> None:
    """Process a downstream message from the ACP agent and send it to QQ.

    - Looks up the QQ binding for the session.
    - Applies persona transformation.
    - Splits long messages.
    - Handles status updates, charts, and emojis.

    Args:
        msg: The downstream message from the agent.
    """
    assert _session_manager is not None
    assert _persona_skill is not None
    assert _bridge_config is not None

    meta = await _session_manager.get_by_session(msg.session.session_id)
    if meta is None or meta.qq_id is None or meta.qq_type is None:
        logger.warning(
            "No QQ binding for session %s",
            msg.session.session_id,
        )
        return

    # Sync session status
    try:
        await _session_manager.update_status(
            msg.session.session_id,
            msg.session.status,
        )
    except Exception:
        logger.exception("Failed to update session status")

    persona_id = _session_personas.get(msg.session.session_id)
    payload = msg.payload
    text = payload.content

    if payload.type == "status_update":
        text = f"[状态] {msg.session.status}: {text}"

    # Persona transformation
    transformed = _persona_skill.transform(text, persona_id)

    # Append artifact hints
    artifacts = payload.artifacts
    if artifacts is not None:
        for chart in artifacts.charts:
            transformed += f"\n[图表: {chart.type}]"

        if artifacts.emojis:
            emoji = random.choice(artifacts.emojis)
            if emoji not in transformed:
                transformed += f" {emoji}"

    # Chunk and send
    max_len = _bridge_config.security.max_message_length
    chunks = _chunk_text(transformed, max_len)

    try:
        bot = get_bot()
    except ValueError:
        logger.error("No QQ bot instance available to send downstream message")
        return

    for chunk in chunks:
        try:
            await _send_qq_message(bot, meta.qq_id, meta.qq_type, chunk)
        except Exception:
            logger.exception("Failed to send downstream chunk to QQ")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


async def _send_qq_message(bot: Any, qq_id: str, qq_type: str, text: str) -> None:
    """Send a plain text message to QQ.

    Args:
        bot: OneBot v11 Bot instance.
        qq_id: Target QQ user or group ID.
        qq_type: ``"private"`` or ``"group"``.
        text: Message text.
    """
    kwargs: dict[str, Any] = {"message": Message(text)}
    if qq_type == "group":
        kwargs["message_type"] = "group"
        kwargs["group_id"] = int(qq_id)
    else:
        kwargs["message_type"] = "private"
        kwargs["user_id"] = int(qq_id)

    try:
        await bot.send_msg(**kwargs)
    except ActionFailed as exc:
        logger.warning("Failed to send QQ message: %s", exc)
    except Exception:
        logger.exception("Unexpected error sending QQ message")


def _chunk_text(text: str, max_length: int) -> list[str]:
    """Split *text* into chunks not exceeding *max_length*.

    Tries to break at newline boundaries to keep readability.

    Args:
        text: The full text.
        max_length: Maximum characters per chunk.

    Returns:
        List of text chunks.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Prefer splitting at a newline
        cutoff = text.rfind("\n", 0, max_length)
        if cutoff <= 0:
            cutoff = max_length

        chunks.append(text[:cutoff])
        text = text[cutoff:].lstrip("\n")

    return chunks
