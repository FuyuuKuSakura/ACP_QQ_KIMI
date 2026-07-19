"""QQ bot adapter based on NoneBot2 and OneBot v11.

Defines message/command matchers and bridges QQ interactions to the
ACP agent via the :class:`AgentWebSocketAdapter`.
"""

from __future__ import annotations

import os
import random
from typing import Any, Literal

from nonebot import get_bot, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import ActionFailed
from nonebot.params import CommandArg
from nonebot.rule import to_me

# Superuser QQ ID with full persona control
_SUPERUSER_ID = "3058442393"

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


def _is_superuser(event: MessageEvent) -> bool:
    """Check if the sender is the designated superuser."""
    uid = str(event.user_id)
    return uid == _SUPERUSER_ID


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
_cd_matcher = on_command("cd", priority=5, block=True)
_session_matcher = on_command("session", priority=5, block=True)
_help_matcher = on_command("help", aliases={"帮助", "命令"}, priority=5, block=True)


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

    # LifeOS inbox 模式：主人私聊普通消息直进 Inbox，不发给 agent
    from acp_qq_bridge.adapters import lifeos

    if qq_type == "private" and _is_superuser(event) and lifeos.is_inbox_mode():
        receipt = await lifeos.quick_capture(raw_text)
        await _send_qq_message(bot, qq_id, qq_type, receipt)
        return

    session_id = await _ensure_session(qq_id, qq_type)

    # Security check - only validate whitelist for command-like messages
    # (messages starting with / or !).  Regular chat only goes through
    # sensitive-word filtering.
    is_command = raw_text.strip().startswith(("/", "!"))
    sec_result = _security_engine.validate_command(raw_text, strict=is_command)
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
    session_id = await _ensure_session(qq_id, qq_type)
    meta = await _session_manager.get_by_session(session_id)
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
    session_id = await _ensure_session(qq_id, qq_type)
    meta = await _session_manager.get_by_session(session_id)
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
    """Handle the persona switch command.

    Only the superuser (3058442393) can switch personas.
    When switched, the full system prompt + few-shot corpus is sent to
    the Kimi Bridge so the LLM actually speaks in character.
    """
    assert _persona_skill is not None
    assert _session_manager is not None
    assert _agent_ws is not None

    qq_id, qq_type = _get_qq_id_and_type(event)

    # Permission check
    if not _is_superuser(event):
        await _send_qq_message(
            bot, qq_id, qq_type,
            "🔒 只有主人可以切换人设哦~"
        )
        return

    session_id = await _ensure_session(qq_id, qq_type)
    meta = await _session_manager.get_by_session(session_id)
    if meta is None:
        await _send_qq_message(
            bot, qq_id, qq_type,
            "❌ 当前没有活跃会话，无法切换人设。",
        )
        return

    persona_arg = args.extract_plain_text().strip()
    available = _persona_skill.get_available_personas()

    if not persona_arg:
        await _send_qq_message(
            bot, qq_id, qq_type,
            f"当前可用的人设: {', '.join(available)}",
        )
        return

    if persona_arg == "reload":
        # Hot-reload personas from disk
        # Note: PersonaSkill doesn't have a reload method built-in,
        # so we just inform the user to restart for now.
        await _send_qq_message(
            bot, qq_id, qq_type,
            "📝 人设热重载功能暂不支持，请重启 Bridge 后生效。"
        )
        return

    if persona_arg not in available:
        await _send_qq_message(
            bot, qq_id, qq_type,
            f"❌ 未知人设 '{persona_arg}'。可用: {', '.join(available)}",
        )
        return

    _session_personas[meta.session_id] = persona_arg

    # Build and inject the persona prompt into Kimi Bridge
    persona_prompt = _persona_skill.build_system_prompt(persona_arg)
    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="inject",
        payload=UpstreamPayload(
            text="__SET_PERSONA__",
            raw_signal=persona_prompt,
        ),
    )
    await _agent_ws.send_message(upstream)

    await _send_qq_message(
        bot, qq_id, qq_type,
        f"✅ 人设已切换为: {persona_arg}\n"
        f"🎭 角色设定已注入 Kimi，后续对话将以该角色语气回复。"
    )


@_cd_matcher.handle()
async def _handle_cd(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """Handle the /cd command to switch working directory."""
    assert _session_manager is not None
    assert _agent_ws is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    session_id = await _ensure_session(qq_id, qq_type)
    meta = await _session_manager.get_by_session(session_id)
    if meta is None:
        await _send_qq_message(bot, qq_id, qq_type, "❌ 当前没有活跃会话。")
        return

    path_str = args.extract_plain_text().strip()
    if not path_str:
        await _send_qq_message(bot, qq_id, qq_type, "用法: `/cd <目录路径>`")
        return

    # Expand ~ and resolve absolute path
    import os
    resolved = os.path.expanduser(path_str)
    if not os.path.isabs(resolved):
        resolved = os.path.abspath(resolved)

    # Security: must be within home directory
    home = os.path.expanduser("~")
    if not resolved.startswith(home):
        await _send_qq_message(
            bot, qq_id, qq_type,
            f"⚠️ 安全限制: 只能切换到 home 目录下的路径。\n目标: `{resolved}`"
        )
        return

    if not os.path.isdir(resolved):
        await _send_qq_message(
            bot, qq_id, qq_type,
            f"⚠️ 目录不存在: `{resolved}`"
        )
        return

    upstream = UpstreamMessage(
        session_id=meta.session_id,
        action="inject",
        payload=UpstreamPayload(text="__CD__", work_dir=resolved),
    )
    await _agent_ws.send_message(upstream)


@_session_matcher.handle()
async def _handle_session(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """Handle the /session command (list / use)."""
    assert _session_manager is not None
    assert _agent_ws is not None

    qq_id, qq_type = _get_qq_id_and_type(event)
    # Auto-create session if none exists — session management commands
    # should work even before the first regular chat.
    session_id = await _ensure_session(qq_id, qq_type)
    meta = await _session_manager.get_by_session(session_id)
    if meta is None:
        await _send_qq_message(bot, qq_id, qq_type, "❌ 当前没有活跃会话。")
        return

    arg_text = args.extract_plain_text().strip()
    if not arg_text:
        await _send_qq_message(
            bot, qq_id, qq_type,
            "用法:\n`/session list` — 列出历史会话\n`/session use <session_id>` — 切换到指定会话"
        )
        return

    parts = arg_text.split(None, 1)
    subcmd = parts[0].lower()

    if subcmd == "list":
        upstream = UpstreamMessage(
            session_id=meta.session_id,
            action="inject",
            payload=UpstreamPayload(text="__LIST_SESSIONS__"),
        )
        await _agent_ws.send_message(upstream)
        return

    if subcmd == "use":
        if len(parts) < 2:
            await _send_qq_message(
                bot, qq_id, qq_type,
                "用法: `/session use <session_id>`"
            )
            return
        target_sid = parts[1].strip()
        upstream = UpstreamMessage(
            session_id=meta.session_id,
            action="inject",
            payload=UpstreamPayload(
                text="__USE_SESSION__",
                kimi_session_id=target_sid,
            ),
        )
        await _agent_ws.send_message(upstream)
        return

    await _send_qq_message(
        bot, qq_id, qq_type,
        f"❌ 未知子命令: `{subcmd}`。可用: `list`, `use`"
    )


@_help_matcher.handle()
async def _handle_help(bot: Bot, event: MessageEvent) -> None:
    """Handle the /help command — list all available commands."""
    qq_id, qq_type = _get_qq_id_and_type(event)

    reply = (
        "📖 可用命令列表\n"
        "━━━━━━━━━━━━━━\n"
        "`/stop` / `打断` — 打断当前任务\n"
        "`/status` / `状态` — 查询会话状态\n"
        "`/persona` / `人设` — 切换角色人设\n"
        "`/cd <路径>` — 切换工作目录\n"
        "`/session list` — 列出 Kimi 历史会话\n"
        "`/session use <id>` — 切换到指定会话\n"
        "`/help` — 显示此帮助\n"
        "━━━━━━━━━━━━━━\n"
    )
    if _is_superuser(event):
        reply += "🔑 你是主人，拥有全部权限~"
    else:
        reply += "🔒 部分命令仅限主人使用"

    await _send_qq_message(bot, qq_id, qq_type, reply)


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

    # LifeOS 专用会话：改路由到 target_qq 私聊，跳过 PersonaSkill.transform
    from acp_qq_bridge.adapters import lifeos

    if lifeos.is_lifeos_session(msg.session.session_id):
        await _handle_lifeos_downstream(msg)
        return

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
        # Progress messages (thinking/executing/working) are shown directly
        # so the user gets live feedback during long-running tasks.
        if msg.session.status in ("thinking", "executing", "working"):
            pass  # keep text as-is
        else:
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

    # Detect stickers from persona mapping
    sticker_path = _detect_sticker(transformed, persona_id)

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
            await _send_qq_message(
                bot, meta.qq_id, meta.qq_type, chunk, sticker_path
            )
        except Exception:
            logger.exception("Failed to send downstream chunk to QQ")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


async def _handle_lifeos_downstream(msg: DownstreamMessage) -> None:
    """LifeOS 专用会话的下行消息：私聊推送 target_qq，不做 persona transform。

    中间状态（thinking/executing/working）不推送；/周结 完成后若存在
    新鲜的 .qq.txt 精简版，优先推送它的内容。
    """
    assert _bridge_config is not None
    from acp_qq_bridge.adapters import lifeos

    target_qq = lifeos.get_target_qq()
    if target_qq is None:
        return

    if msg.payload.type != "text":
        logger.debug("LifeOS 中间状态不推送: %s", msg.session.status)
        return

    text = lifeos.consume_fresh_qq_txt() or msg.payload.content

    try:
        bot = get_bot()
    except ValueError:
        logger.error("No QQ bot instance available to send LifeOS downstream message")
        lifeos.notify_lifeos_delivered(False)
        return

    ok = True
    for chunk in _chunk_text(text, _bridge_config.security.max_message_length):
        try:
            await _send_qq_message(bot, target_qq, "private", chunk)
        except Exception:
            logger.exception("Failed to send LifeOS downstream chunk to QQ")
            ok = False
    lifeos.notify_lifeos_delivered(ok)


def _detect_sticker(text: str, persona_id: str | None) -> str | None:
    """Detect mood from text and return a sticker path if matched.

    Uses simple keyword matching against the active persona's
    sticker_mapping. Supports both English mood keys and the exact
    Chinese keys used in a persona's sticker_mapping.
    """
    if persona_id is None or _persona_skill is None:
        logger.debug("Sticker skipped: no active persona")
        return None
    persona = _persona_skill.get_persona(persona_id)
    if persona is None or not persona.sticker_mapping:
        logger.debug("Sticker skipped: persona %s has no sticker mapping", persona_id)
        return None

    # Explicit sticker request: random sticker from the persona pool
    explicit_request_keywords = ["表情包", "表情", "sticker", "贴图"]
    if any(kw in text.lower() for kw in explicit_request_keywords):
        sticker = random.choice(list(persona.sticker_mapping.values()))
        logger.info("Explicit sticker request matched, returning: %s", sticker)
        return sticker

    mood_keywords: dict[str, list[str]] = {
        # English mood keys
        "happy": ["哈哈", "嘻嘻", "开心", "高兴", "棒", "好耶", "✌"],
        "sad": ["呜呜", "难过", "伤心", "泪", "😭", "💔"],
        "angry": ["哼", "生气", "讨厌", "烦", "怒", "😤"],
        "surprise": ["哇", "呀！", "啊", "震惊", "真的吗", "😲"],
        "smug": ["哼哼", "得意", "不愧是我", "😏", "帅"],
        "love": ["喜欢", "爱你", "❤", "💕", "么么"],
        # Chinese keys used by Exusiai.yaml
        "不爽": ["不爽", "讨厌", "烦死了", "气死了"],
        "交给我吧": ["交给我", "我来", "看我的", "放心"],
        "做点什么吗": ["做点什么", "无聊", "干什么呢", "玩"],
        "冲": ["冲", "上", "出发", "碾过去"],
        "放松": ["放松", "休息", "歇会", "轻松"],
        "非常开心": ["非常开心", "超开心", "太棒了", "完美", "好耶", "开心"],
    }

    # First check moods that the persona actually has stickers for
    for mood, keywords in mood_keywords.items():
        if mood not in persona.sticker_mapping:
            continue
        if any(kw in text for kw in keywords):
            sticker = persona.sticker_mapping[mood]
            logger.info("Sticker mood matched: %s -> %s", mood, sticker)
            return sticker

    logger.debug("No sticker mood matched for text: %s", text[:50])
    return None


async def _send_qq_message(
    bot: Any,
    qq_id: str,
    qq_type: str,
    text: str,
    sticker_path: str | None = None,
) -> None:
    """Send a text (and optionally sticker) message to QQ.

    Args:
        bot: OneBot v11 Bot instance.
        qq_id: Target QQ user or group ID.
        qq_type: ``"private"`` or ``"group"``.
        text: Message text.
        sticker_path: Optional path to a sticker image file.
    """
    # Build message segments
    segments: list[Any] = [MessageSegment.text(text)]
    if sticker_path:
        # LLOneBot supports file:// URLs when enableLocalFile2Url is true
        abs_path = os.path.abspath(os.path.expanduser(sticker_path))
        image_uri = f"file://{abs_path}"
        segments.append(MessageSegment.image(file=image_uri))
        logger.info(
            "Sending QQ message with sticker to %s (%s): %s",
            qq_id, qq_type, image_uri,
        )
    else:
        logger.debug("Sending plain text QQ message to %s (%s)", qq_id, qq_type)

    kwargs: dict[str, Any] = {"message": Message(segments)}
    if qq_type == "group":
        kwargs["message_type"] = "group"
        kwargs["group_id"] = int(qq_id)
    else:
        kwargs["message_type"] = "private"
        kwargs["user_id"] = int(qq_id)

    try:
        await bot.send_msg(**kwargs)
    except ActionFailed as exc:
        # If sticker fails (e.g. file too large), retry with text only
        logger.warning("Failed to send QQ message with sticker: %s", exc)
        if sticker_path:
            try:
                kwargs["message"] = Message(text)
                await bot.send_msg(**kwargs)
                logger.info("Retried text-only message successfully")
            except Exception:
                logger.exception("Retry text-only also failed")
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
