"""LifeOS 任务管理扩展（QQ ↔ Obsidian vault 桥接）。

提供四组能力：

* 直写命令：/记 /打卡 /能量 /休闲 /总览 /下步 /决策 /任务 —— 直接读写 vault
  文件并 git commit，一行回执。
* agent 命令：/周结 /复活 —— 走固定 LifeOS 会话（``lifeos_main``）向
  kimi_code_bridge 发送固定 SOP 提示词，回复经 qq_bot 下行路由推送给主人。
* 定时任务：每日日结简报（agent 通道）、每日未记录提醒（直写判定）、
  周日周结提醒、自定义提醒（/提醒，30 秒 tick）；支持关机/断连后的
  错过补发（state_file 记录 last_fired，自定义提醒按 next_fire 补推）。
* 图片灵感记录：superuser 私聊发来的图片存到 灵感草稿/assets，
  链接与附注追加 Inbox 并 git commit。

安全约定：所有 vault 路径拼接做 resolve() 越界检查；写入内容先过
``SecurityEngine`` 敏感词过滤；git commit 失败（如无改动）不视为错误。
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import ssl
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from nonebot import get_bot, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule

from acp_qq_bridge.adapters.agent_ws import AgentWebSocketAdapter
from acp_qq_bridge.config import BridgeConfig, LifeOSConfig
from acp_qq_bridge.core.protocol import UpstreamMessage, UpstreamPayload
from acp_qq_bridge.core.security import SecurityEngine
from acp_qq_bridge.middleware.persona import PersonaSkill
from acp_qq_bridge.utils.logger import get_logger

logger = get_logger(__name__)

# nonebot 命令参数依赖（单例复用，避免 B008）
_CMD_ARG = CommandArg()

# LifeOS agent 任务使用的固定会话 ID（不污染用户的聊天会话）
LIFEOS_SESSION_ID = "lifeos_main"

# agent_ws 断连时每日简报的重试间隔（秒），测试可 monkeypatch
_RETRY_DELAY = 60

# 每日简报等待 agent 回执投递成功的超时（秒）；周结级任务可能跑几分钟
_BRIEF_TIMEOUT = 900

# 日结简报的投递确认 Future：qq_bot 下行投递后调 notify_lifeos_delivered 解析
_brief_waiter: asyncio.Future[bool] | None = None

# 仓库根目录（…/src/acp_qq_bridge/adapters/lifeos.py → 上四级）
_REPO_ROOT = Path(__file__).resolve().parents[3]

# 固定 SOP 提示词（agent 通道）
WEEKLY_PROMPT = "阅读 00_系统/AGENT.md 并执行周结 SOP。完成后直接输出要推送给主人的纯文本汇报。"
REVIVE_PROMPT = "阅读 00_系统/AGENT.md 并执行复活 SOP。完成后直接输出要推送给主人的纯文本汇报。"
DAILY_BRIEF_PROMPT = (
    "阅读 00_系统/AGENT.md 并执行日结 SOP。"
    "完成后直接输出要推送给主人的纯文本汇报（昨日日结 + 今日建议 top-3 + 保底项）。"
)

# 日记模板兜底（vault 内 tpl_日记.md 缺失时使用）
_FALLBACK_DIARY_TEMPLATE = """---
date: {date}
能量:
---
## 推进

## 休闲

## 状态与灵感（随便写）


## 明天第一件事
"""

# LifeOS 命令表（/任务 返回）
_HELP_TEXT = """LifeOS 命令表
/记 <内容> — 追加到 Inbox
/打卡 <轨道> <内容> — 记入当日日记"推进"
/能量 <0-10> — 记录当日能量值
/休闲 <内容> — 记入当日日记"休闲"
/总览 — 九条轨道进度一览
/下步 — 各轨道未勾选的下一步
/决策 <N> <内容> — 裁决周结待决策第 N 项
/周结 — 让 agent 执行周结 SOP
/复活 — 断档后让 agent 推断近况
/提醒 [每天] HH:mm <内容> — 自定义提醒
/提醒列表 — 查看全部自定义提醒
/删提醒 <编号> — 删除指定提醒
直接发图片 — 存入灵感草稿并记 Inbox
/任务 — 显示本命令表"""

# ------------------------------------------------------------------ #
# 模块级状态（由 setup_lifeos 填充）
# ------------------------------------------------------------------ #

_lifeos_config: LifeOSConfig | None = None
_agent_ws: AgentWebSocketAdapter | None = None
_security: SecurityEngine | None = None
_persona: PersonaSkill | None = None
_vault: Path | None = None

# LifeOS 专用会话集合（qq_bot 下行路由命中即改私聊推送 + 跳过 persona transform）
_lifeos_sessions: set[str] = {LIFEOS_SESSION_ID}
# 已注入过 persona 提示词的会话
_persona_injected: set[str] = set()
# /周结 发出后等待 .qq.txt 精简版的起始时间戳
_weekly_pending_since: float | None = None


# ------------------------------------------------------------------ #
# 对外查询接口（供 qq_bot 下行路由使用）
# ------------------------------------------------------------------ #


def is_lifeos_session(session_id: str) -> bool:
    """判断 *session_id* 是否为 LifeOS 专用会话。"""
    return session_id in _lifeos_sessions


def get_target_qq() -> str | None:
    """返回 LifeOS 私聊推送目标 QQ（未启用时为 ``None``）。"""
    if _lifeos_config is None or not _lifeos_config.enabled:
        return None
    return _lifeos_config.target_qq


def is_inbox_mode() -> bool:
    """普通私聊消息是否直进 Inbox（``plain_message_mode == "inbox"``）。"""
    return _lifeos_config is not None and _lifeos_config.plain_message_mode == "inbox"


def consume_fresh_qq_txt() -> str | None:
    """/周结 完成后，若 30_日志/周结/ 出现新的 .qq.txt 精简版则返回其内容。

    只消费一次：返回内容后清除等待标记；无新文件时返回 ``None``。
    """
    global _weekly_pending_since
    if _weekly_pending_since is None or _vault is None:
        return None
    try:
        weekly_dir = _vault / "30_日志" / "周结"
        candidates = [
            p for p in weekly_dir.glob("*.qq.txt") if p.stat().st_mtime >= _weekly_pending_since
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        _weekly_pending_since = None
        content = latest.read_text(encoding="utf-8").strip()
        logger.info("周结精简版命中: %s", latest.name)
        return content or None
    except Exception:
        logger.exception("读取周结 .qq.txt 失败")
        return None


# ------------------------------------------------------------------ #
# 初始化
# ------------------------------------------------------------------ #


def setup_lifeos(
    config: LifeOSConfig,
    agent_ws: AgentWebSocketAdapter,
    security: SecurityEngine,
    persona: PersonaSkill,
) -> LifeOSManager:
    """填充模块级状态并返回调度器（不注册 nonebot matcher，便于测试）。"""
    global _lifeos_config, _agent_ws, _security, _persona, _vault, _weekly_pending_since
    _lifeos_config = config
    _agent_ws = agent_ws
    _security = security
    _persona = persona
    _vault = Path(config.vault_path).expanduser().resolve()
    _persona_injected.clear()
    _weekly_pending_since = None
    logger.info("LifeOS 已配置: vault=%s target=%s", _vault, config.target_qq)
    return LifeOSManager()


def init_lifeos(
    config: BridgeConfig,
    agent_ws: AgentWebSocketAdapter,
    security: SecurityEngine,
    persona: PersonaSkill,
) -> LifeOSManager | None:
    """初始化 LifeOS 扩展：注册命令 matcher 并返回定时任务调度器。

    由 ``__main__.py`` 在 ``init_qq_bot`` 之后调用；``lifeos.enabled``
    为 ``False`` 时返回 ``None``。
    """
    lc = config.lifeos
    if not lc.enabled:
        logger.info("LifeOS 扩展未启用")
        return None
    if not lc.vault_path:
        logger.error("LifeOS 已启用但 vault_path 为空，扩展不生效")
        return None

    manager = setup_lifeos(lc, agent_ws, security, persona)

    on_command("记", priority=5, block=True).handle()(_handle_ji)
    on_command("打卡", priority=5, block=True).handle()(_handle_daka)
    on_command("能量", priority=5, block=True).handle()(_handle_nengliang)
    on_command("休闲", priority=5, block=True).handle()(_handle_xiuxian)
    on_command("总览", priority=5, block=True).handle()(_handle_zonglan)
    on_command("下步", priority=5, block=True).handle()(_handle_xiabu)
    on_command("决策", priority=5, block=True).handle()(_handle_juece)
    on_command("周结", priority=5, block=True).handle()(_handle_zhoujie)
    on_command("复活", priority=5, block=True).handle()(_handle_fuhuo)
    on_command("提醒", priority=5, block=True).handle()(_handle_tixing)
    on_command("提醒列表", priority=5, block=True).handle()(_handle_tixing_list)
    on_command("删提醒", priority=5, block=True).handle()(_handle_shantixing)
    # 注：/帮助 已被现有通用帮助占用（同优先级双回复），LifeOS 命令表改用 /任务
    on_command("任务", aliases={"lifeos"}, priority=5, block=True).handle()(_handle_renwu)
    # 图片灵感捕获：priority=4 先于命令与普通消息，rule 不满足则放行到现有流程
    on_message(rule=Rule(_has_superuser_image), priority=4, block=True).handle()(_handle_image)

    logger.info("LifeOS 命令 matcher 已注册")
    return manager


# ------------------------------------------------------------------ #
# 鉴权与发送辅助
# ------------------------------------------------------------------ #


def _is_superuser(event: MessageEvent) -> bool:
    """复用 qq_bot 的 superuser 判定；qq_bot 不可导入时（测试）退化到硬编码。"""
    try:
        from acp_qq_bridge.adapters.qq_bot import _is_superuser as _qq_is_superuser

        return _qq_is_superuser(event)
    except Exception:
        return str(event.user_id) == "3058442393"


async def _reply(bot: Bot, event: MessageEvent, text: str) -> None:
    """按消息来源（私聊/群聊）回执。"""
    from acp_qq_bridge.adapters.qq_bot import _get_qq_id_and_type, _send_qq_message

    qq_id, qq_type = _get_qq_id_and_type(event)
    await _send_qq_message(bot, qq_id, qq_type, text)


async def _push_text(text: str) -> bool:
    """主动向 target_qq 私聊推送文本；bot 未连接时记日志并返回 ``False``。"""
    if _lifeos_config is None:
        return False
    try:
        bot = get_bot()
    except ValueError:
        logger.warning("LLOneBot 未连接，LifeOS 推送暂缓: %s", text[:40])
        return False
    from acp_qq_bridge.adapters.qq_bot import _send_qq_message

    await _send_qq_message(bot, _lifeos_config.target_qq, "private", text)
    return True


def _check_sensitive(content: str) -> str | None:
    """敏感词过滤兜底；命中时返回原因，否则返回 ``None``。"""
    if _security is None:
        return None
    result = _security.validate_command(content, strict=False)
    if not result.passed:
        return result.reason or "敏感内容"
    return None


# ------------------------------------------------------------------ #
# vault 路径与文件读写（纯逻辑，可测）
# ------------------------------------------------------------------ #


def _safe_join(vault: Path, *parts: str) -> Path:
    """拼接 vault 内路径并做 resolve() 越界检查。"""
    base = vault.resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径越界: {target}")
    return target


def _inbox_path(vault: Path) -> Path:
    return _safe_join(vault, "40_收集箱", "Inbox.md")


def _diary_path(vault: Path, day: date) -> Path:
    return _safe_join(vault, "30_日志", "每日", f"{day.isoformat()}.md")


def _render_diary_template(vault: Path, day: date) -> str:
    """按 tpl_日记.md 渲染当日日记（剥离模板里的示例行），缺失时用兜底模板。"""
    try:
        tpl_path = _safe_join(vault, "00_系统", "模板", "tpl_日记.md")
        tpl = tpl_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return _FALLBACK_DIARY_TEMPLATE.format(date=day.isoformat())
    tpl = tpl.replace("{{date}}", day.isoformat())
    lines: list[str] = []
    for line in tpl.splitlines():
        # 去掉模板中的引导性示例内容行（保留小节标题与 frontmatter）
        if line.startswith("- [轨道名]") or line.startswith("- 游戏 / 散步"):
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def _ensure_diary(vault: Path, day: date) -> Path:
    """返回当日日记路径，不存在则按模板创建。"""
    path = _diary_path(vault, day)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_diary_template(vault, day), encoding="utf-8")
        logger.info("创建当日日记: %s", path)
    return path


def _extract_section_lines(text: str, heading: str) -> list[str]:
    """取出 ``## <heading>`` 小节到下一 ``## `` 标题之间的内容行。"""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return lines[start:end]


def _append_to_section(text: str, heading: str, entry: str) -> str:
    """向 ``## <heading>`` 小节末尾追加一行，保持小节间空行分隔。"""
    lines = text.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = i + 1
            break
    if start is None:
        raise ValueError(f"小节不存在: {heading}")
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    # 插入点：小节末尾（跳过尾部空行）
    idx = end
    while idx > start and not lines[idx - 1].strip():
        idx -= 1
    lines.insert(idx, entry)
    # 保证与下一标题之间有空行
    if idx + 1 < len(lines) and lines[idx + 1].startswith("## "):
        lines.insert(idx + 1, "")
    return "\n".join(lines) + "\n"


def append_inbox(vault: Path, content: str, now: datetime) -> Path:
    """向 Inbox.md 末尾追加一条捕获记录，格式 ``- YYYY-MM-DD HH:mm | 内容``。"""
    path = _inbox_path(vault)
    line = f"- {now.strftime('%Y-%m-%d %H:%M')} | {content}"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Inbox（唯一捕获入口）\n"
    if not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + line + "\n", encoding="utf-8")
    return path


def append_diary_section(vault: Path, day: date, heading: str, entry: str) -> Path:
    """向当日日记指定小节追加一行（日记不存在则按模板创建）。"""
    path = _ensure_diary(vault, day)
    text = path.read_text(encoding="utf-8")
    path.write_text(_append_to_section(text, heading, entry), encoding="utf-8")
    return path


def set_diary_energy(vault: Path, day: date, energy: int) -> Path:
    """写入当日日记 frontmatter 的 ``能量`` 键。"""
    path = _ensure_diary(vault, day)
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("日记缺少 frontmatter")
    try:
        close = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("日记 frontmatter 未闭合") from exc
    for i in range(1, close):
        if lines[i].startswith("能量:"):
            lines[i] = f"能量: {energy}"
            break
    else:
        lines.insert(close, f"能量: {energy}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def render_overview(vault: Path) -> str:
    """读 20_进度/_总览.md，把 markdown 表格转成逐行纯文本。"""
    path = _safe_join(vault, "20_进度", "_总览.md")
    if not path.exists():
        return "总览文件不存在。"
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(set(c) <= set("-: ") for c in cells):
            continue  # 表头分隔行
        rows.append(cells)
    if len(rows) <= 1:
        return "总览表为空。"
    out = ["进度总览："]
    for cells in rows[1:]:
        cells += [""] * (4 - len(cells))
        out.append(f"- {cells[0]}：{cells[1]}，{cells[2]}（{cells[3]}）")
    return "\n".join(out)


def render_next_steps(vault: Path) -> str:
    """聚合全部 STATE 文件"下一步"小节的未勾选项，按轨道分组。"""
    states_dir = _safe_join(vault, "20_进度")
    out: list[str] = ["未勾选的下一步："]
    found = False
    for state_file in sorted(states_dir.glob("*_STATE.md")):
        track = state_file.name[: -len("_STATE.md")]
        text = state_file.read_text(encoding="utf-8")
        pending = [
            line.strip()[len("- [ ]") :].strip()
            for line in _extract_section_lines(text, "下一步")
            if line.strip().startswith("- [ ]")
        ]
        if pending:
            found = True
            out.append(f"【{track}】")
            out.extend(f"- {item}" for item in pending)
    if not found:
        return "所有轨道的下一步都已清空。"
    return "\n".join(out)


def apply_decision(vault: Path, n: int, decision: str) -> str:
    """勾选最新周结"需要我决策的"第 N 项并追加决定，返回被决策的事项文本。"""
    weekly_dir = _safe_join(vault, "30_日志", "周结")
    files = sorted(weekly_dir.glob("*.md"))
    if not files:
        raise ValueError("还没有任何周结文件")
    path = files[-1]
    lines = path.read_text(encoding="utf-8").splitlines()
    section = _extract_section_lines("\n".join(lines), "需要我决策的")
    # 定位小节在全文中的行号区间
    start = next(i for i, line in enumerate(lines) if line.strip() == "## 需要我决策的") + 1
    pending_idx = [
        start + i for i, line in enumerate(section) if line.strip().startswith("- [ ]")
    ]
    if not 1 <= n <= len(pending_idx):
        raise ValueError(f"第 {n} 项不存在（当前共 {len(pending_idx)} 项待决策）")
    idx = pending_idx[n - 1]
    item = lines[idx].strip()[len("- [ ]") :].strip()
    lines[idx] = f"- [x] {item} → 决定：{decision}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return item


def should_remind(diary_text: str | None) -> bool:
    """未记录提醒判定：当日日记不存在或"推进"小节为空则应提醒。"""
    if diary_text is None:
        return True
    entries = [
        line
        for line in _extract_section_lines(diary_text, "推进")
        if line.strip().startswith("- ")
    ]
    return not entries


# ------------------------------------------------------------------ #
# git 提交（范式见 kimi_code_bridge.py 的子进程调用）
# ------------------------------------------------------------------ #


async def git_commit(vault: Path, message: str) -> bool:
    """``git add -A && git commit``；无改动不算错误，返回是否产生了提交。"""
    add = await asyncio.create_subprocess_exec(
        "git", "-C", str(vault), "add", "-A",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, add_err = await add.communicate()
    if add.returncode != 0:
        logger.warning("git add 失败: %s", add_err.decode("utf-8", errors="replace")[:200])
        return False
    commit = await asyncio.create_subprocess_exec(
        "git", "-C", str(vault), "commit", "-m", message,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await commit.communicate()
    if commit.returncode != 0:
        # 无改动时 commit 返回 1，属正常情况
        logger.info("git commit 无新提交: %s", out.decode("utf-8", errors="replace")[:120])
        return False
    return True


# ------------------------------------------------------------------ #
# agent 通道（固定 LifeOS 会话 + persona 注入）
# ------------------------------------------------------------------ #


async def _ensure_persona_injected() -> None:
    """LifeOS 会话创建时自动注入一次 persona 提示词（复用 __SET_PERSONA__ 链路）。"""
    if _lifeos_config is None or _agent_ws is None or _persona is None:
        return
    persona_id = _lifeos_config.persona
    if not persona_id or LIFEOS_SESSION_ID in _persona_injected:
        return
    prompt = _persona.build_system_prompt(persona_id)
    if prompt:
        await _agent_ws.send_message(
            UpstreamMessage(
                session_id=LIFEOS_SESSION_ID,
                action="inject",
                payload=UpstreamPayload(text="__SET_PERSONA__", raw_signal=prompt),
            )
        )
    _persona_injected.add(LIFEOS_SESSION_ID)


async def send_lifeos_prompt(text: str) -> None:
    """向 LifeOS 固定会话发送 agent 任务提示词（cwd 锁定 vault）。"""
    assert _agent_ws is not None
    assert _vault is not None
    await _ensure_persona_injected()
    await _agent_ws.send_message(
        UpstreamMessage(
            session_id=LIFEOS_SESSION_ID,
            action="user_input",
            payload=UpstreamPayload(text=text, work_dir=str(_vault)),
        )
    )


async def quick_capture(content: str) -> str:
    """plain_message_mode == "inbox" 时普通私聊消息的直进入口。"""
    assert _vault is not None
    hit = _check_sensitive(content)
    if hit is not None:
        return f"⚠️ 安全警告: {hit}"
    append_inbox(_vault, content, datetime.now())
    committed = await git_commit(_vault, f"qq: 记 {content[:30]}")
    return "已记入 Inbox。" if committed else "已记入 Inbox（无新提交）。"


# ------------------------------------------------------------------ #
# 命令 handler（由 init_lifeos 注册；非 superuser 零响应）
# ------------------------------------------------------------------ #


async def _handle_ji(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/记 <内容> —— 追加 Inbox.md。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    content = args.extract_plain_text().strip()
    if not content:
        await _reply(bot, event, "用法: /记 <内容>")
        return
    hit = _check_sensitive(content)
    if hit is not None:
        await _reply(bot, event, f"⚠️ 安全警告: {hit}")
        return
    append_inbox(_vault, content, datetime.now())
    committed = await git_commit(_vault, f"qq: 记 {content[:30]}")
    await _reply(bot, event, "已记入 Inbox。" if committed else "已记入 Inbox（无新提交）。")


async def _handle_daka(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/打卡 <轨道> <内容> —— 记入当日日记"推进"小节。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    raw = args.extract_plain_text().strip()
    parts = raw.split(None, 1)
    if len(parts) < 2:
        await _reply(bot, event, "用法: /打卡 <轨道> <内容>")
        return
    track = parts[0].strip("[]")
    content = parts[1].strip()
    hit = _check_sensitive(raw)
    if hit is not None:
        await _reply(bot, event, f"⚠️ 安全警告: {hit}")
        return
    entry = f"[{track}] {content}"
    append_diary_section(_vault, date.today(), "推进", f"- {entry}")
    committed = await git_commit(_vault, f"qq: 打卡 {entry[:30]}")
    suffix = "" if committed else "（无新提交）"
    await _reply(bot, event, f"已打卡：{entry}。{suffix}")


async def _handle_nengliang(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/能量 <N> —— 写入当日日记 frontmatter。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    raw = args.extract_plain_text().strip()
    try:
        energy = int(raw)
    except ValueError:
        await _reply(bot, event, "用法: /能量 <0-10 整数>")
        return
    if not 0 <= energy <= 10:
        await _reply(bot, event, "能量值需在 0-10 之间。")
        return
    set_diary_energy(_vault, date.today(), energy)
    committed = await git_commit(_vault, f"qq: 能量 {energy}")
    suffix = "" if committed else "（无新提交）"
    await _reply(bot, event, f"已记录能量：{energy}。{suffix}")


async def _handle_xiuxian(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/休闲 <内容> —— 记入当日日记"休闲"小节。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    content = args.extract_plain_text().strip()
    if not content:
        await _reply(bot, event, "用法: /休闲 <内容>")
        return
    hit = _check_sensitive(content)
    if hit is not None:
        await _reply(bot, event, f"⚠️ 安全警告: {hit}")
        return
    append_diary_section(_vault, date.today(), "休闲", f"- {content}")
    committed = await git_commit(_vault, f"qq: 休闲 {content[:30]}")
    suffix = "" if committed else "（无新提交）"
    await _reply(bot, event, f"已记录休闲：{content}。{suffix}")


async def _handle_zonglan(bot: Bot, event: MessageEvent) -> None:
    """/总览 —— 九条轨道进度一览（纯文本）。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    try:
        await _reply(bot, event, render_overview(_vault))
    except Exception as exc:
        logger.exception("生成总览失败")
        await _reply(bot, event, f"总览读取失败: {exc}")


async def _handle_xiabu(bot: Bot, event: MessageEvent) -> None:
    """/下步 —— 各轨道未勾选的下一步（纯文本）。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    try:
        await _reply(bot, event, render_next_steps(_vault))
    except Exception as exc:
        logger.exception("生成下步失败")
        await _reply(bot, event, f"下步读取失败: {exc}")


async def _handle_juece(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/决策 <N> <内容> —— 裁决最新周结待决策第 N 项。"""
    if not _is_superuser(event):
        return
    assert _vault is not None
    raw = args.extract_plain_text().strip()
    parts = raw.split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        await _reply(bot, event, "用法: /决策 <序号> <决定内容>")
        return
    n, decision = int(parts[0]), parts[1].strip()
    hit = _check_sensitive(decision)
    if hit is not None:
        await _reply(bot, event, f"⚠️ 安全警告: {hit}")
        return
    try:
        item = apply_decision(_vault, n, decision)
    except ValueError as exc:
        await _reply(bot, event, f"⚠️ {exc}")
        return
    committed = await git_commit(_vault, f"qq: 决策 {n} {decision[:30]}")
    suffix = "" if committed else "（无新提交）"
    await _reply(bot, event, f"已裁决第 {n} 项：{item} → {decision}。{suffix}")


async def _handle_zhoujie(bot: Bot, event: MessageEvent) -> None:
    """/周结 —— 立即回执，然后让 agent 执行周结 SOP。"""
    global _weekly_pending_since
    if not _is_superuser(event):
        return
    await _reply(bot, event, "收到，在做。完成后我把精简版发你。")
    _weekly_pending_since = time.time()
    await send_lifeos_prompt(WEEKLY_PROMPT)


async def _handle_fuhuo(bot: Bot, event: MessageEvent) -> None:
    """/复活 —— 立即回执，然后让 agent 执行复活 SOP。"""
    if not _is_superuser(event):
        return
    await _reply(bot, event, "收到，在做。我先把近况推断整理出来。")
    await send_lifeos_prompt(REVIVE_PROMPT)


async def _handle_renwu(bot: Bot, event: MessageEvent) -> None:
    """/任务 —— LifeOS 命令表。"""
    if not _is_superuser(event):
        return
    await _reply(bot, event, _HELP_TEXT)


async def _handle_tixing(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/提醒 [每天] HH:mm <内容> —— 新增自定义提醒。"""
    if not _is_superuser(event):
        return
    raw = args.extract_plain_text().strip()
    try:
        daily, time_str, content, next_fire = parse_reminder_args(raw, datetime.now())
    except ValueError as exc:
        await _reply(bot, event, str(exc))
        return
    hit = _check_sensitive(content)
    if hit is not None:
        await _reply(bot, event, f"⚠️ 安全警告: {hit}")
        return
    reminder = add_reminder(_reminders_path(), time_str, content, daily, next_fire)
    kind = "每天" if reminder.daily else f"{reminder.next_fire} 触发"
    text = f"已设提醒 {reminder.id}：{reminder.time} {reminder.content}（{kind}）。"
    await _reply(bot, event, text)

async def _handle_tixing_list(bot: Bot, event: MessageEvent) -> None:
    """/提醒列表 —— 编号列出全部自定义提醒。"""
    if not _is_superuser(event):
        return
    await _reply(bot, event, render_reminders(load_reminders(_reminders_path())))


async def _handle_shantixing(bot: Bot, event: MessageEvent, args: Message = _CMD_ARG) -> None:
    """/删提醒 <编号> —— 按编号删除提醒。"""
    if not _is_superuser(event):
        return
    raw = args.extract_plain_text().strip()
    if not raw.isdigit():
        await _reply(bot, event, "用法: /删提醒 <编号>（编号见 /提醒列表）")
        return
    removed = delete_reminder(_reminders_path(), int(raw))
    if removed is None:
        await _reply(bot, event, f"⚠️ 提醒 {raw} 不存在。")
        return
    await _reply(bot, event, f"已删除提醒 {removed.id}：{removed.time} {removed.content}。")


# ------------------------------------------------------------------ #
# 定时任务（照抄 SessionManager.start_cleanup_task 的 asyncio loop 范式）
# ------------------------------------------------------------------ #


def _parse_hhmm(spec: str) -> dtime:
    """解析 ``HH:mm`` 为 :class:`datetime.time`。"""
    hour, minute = spec.split(":", 1)
    return dtime(hour=int(hour), minute=int(minute))


def next_scheduled(now: datetime, at: dtime, weekday: int | None = None) -> datetime:
    """计算下次触发时刻；*weekday* 非空时按周触发（0=周一）。"""
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if weekday is None:
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    days_ahead = (weekday - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def last_scheduled(now: datetime, at: dtime, weekday: int | None = None) -> datetime:
    """最近一次应发时刻（next_scheduled 往前推一个周期）。"""
    period = timedelta(days=1 if weekday is None else 7)
    return next_scheduled(now, at, weekday) - period


def should_catchup(last_fired: str | None, last: datetime, now: datetime) -> bool:
    """补发判定：上次应发时刻已过且 last_fired 日期早于它（只补最近一次）。"""
    if last > now:
        return False
    if not last_fired:
        return True
    try:
        fired_date = date.fromisoformat(last_fired)
    except ValueError:
        return True
    return fired_date < last.date()


def _state_file_path() -> Path:
    """state_file 路径（相对路径基于仓库根目录解析）。"""
    assert _lifeos_config is not None
    p = Path(_lifeos_config.state_file).expanduser()
    if not p.is_absolute():
        p = _REPO_ROOT / p
    return p


def load_state(path: Path) -> dict[str, str]:
    """读取 last_fired 状态文件；缺失或损坏时返回空表。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_state(path: Path, state: dict[str, str]) -> None:
    """写回 last_fired 状态文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _mark_fired(job_name: str) -> None:
    """记录某任务今天已成功触发。"""
    path = _state_file_path()
    state = load_state(path)
    state[job_name] = date.today().isoformat()
    save_state(path, state)


JobAction = Callable[[], Awaitable[bool]]


@dataclass
class JobSpec:
    """一个定时任务的定义。"""

    name: str
    at: dtime
    weekday: int | None
    action: JobAction


def _job_specs() -> list[JobSpec]:
    """按当前配置生成任务清单。"""
    assert _lifeos_config is not None
    weekly = _lifeos_config.weekly_reminder
    return [
        JobSpec(
            "daily_brief", _parse_hhmm(_lifeos_config.daily_brief_time), None, fire_daily_brief
        ),
        JobSpec("reminder", _parse_hhmm(_lifeos_config.reminder_time), None, fire_reminder),
        JobSpec(
            "weekly_reminder",
            _parse_hhmm(str(weekly.get("time", "10:00"))),
            int(weekly.get("weekday", 6)),
            fire_weekly_reminder,
        ),
    ]


async def fire_daily_brief() -> bool:
    """每日日结简报（agent 通道）；以"回复成功投递到 QQ"为完成标准。

    agent_ws 断连时重试 1 次；投递失败/超时返回 ``False`` 等待补发巡检。
    """
    global _brief_waiter
    for attempt in (1, 2):
        if _agent_ws is not None and _agent_ws.is_connected:
            _brief_waiter = asyncio.get_running_loop().create_future()
            try:
                await send_lifeos_prompt(DAILY_BRIEF_PROMPT)
                return await asyncio.wait_for(_brief_waiter, timeout=_BRIEF_TIMEOUT)
            except TimeoutError:
                logger.warning("日结简报等待 agent 回执超时（%ds）", _BRIEF_TIMEOUT)
                return False
            finally:
                _brief_waiter = None
        logger.warning("agent_ws 未连接，日结简报第 %d 次尝试失败", attempt)
        if attempt == 1:
            await asyncio.sleep(_RETRY_DELAY)
    logger.warning("日结简报发送失败，标记为待补发")
    return False


def notify_lifeos_delivered(ok: bool) -> None:
    """qq_bot 下行投递结果回调：日结简报据此判定任务是否真正完成。"""
    if _brief_waiter is not None and not _brief_waiter.done():
        _brief_waiter.set_result(ok)


async def fire_reminder(today: date | None = None) -> bool:
    """每日未记录提醒（直写判定）；无需提醒时也算任务已完成。"""
    assert _vault is not None
    day = today or date.today()
    path = _diary_path(_vault, day)
    diary_text = path.read_text(encoding="utf-8") if path.exists() else None
    if not should_remind(diary_text):
        logger.info("今日已有记录，跳过提醒")
        return True
    return await _push_text("博士，今天还没有记录。一句话也行，回 /记 加内容即可。")


async def fire_weekly_reminder() -> bool:
    """周日周结提醒。"""
    return await _push_text("博士，这周该做周结了。回 /周结 我就开始。")


async def _job_loop(spec: JobSpec) -> None:
    """单个任务的"算下次触发 → sleep → 执行"循环。"""
    while True:
        try:
            now = datetime.now()
            wait = (next_scheduled(now, spec.at, spec.weekday) - now).total_seconds()
            await asyncio.sleep(max(wait, 0.0))
            if load_state(_state_file_path()).get(spec.name) == date.today().isoformat():
                logger.info("LifeOS 任务 %s 今日已完成（含补发），跳过本次定时触发", spec.name)
                continue
            fired = await spec.action()
            if fired:
                _mark_fired(spec.name)
            else:
                logger.warning("LifeOS 任务 %s 未能完成，等待补发", spec.name)
        except asyncio.CancelledError:
            logger.debug("LifeOS 任务 %s 已取消", spec.name)
            raise
        except Exception:
            logger.exception("LifeOS 任务 %s 执行异常", spec.name)


async def run_catchup(now: datetime | None) -> list[str]:
    """逐 job 检查"上次应发时刻 > last_fired"则立即补发一次（只补最近一次）。"""
    now = now or datetime.now()
    state = load_state(_state_file_path())
    fired_jobs: list[str] = []
    for spec in _job_specs():
        last = last_scheduled(now, spec.at, spec.weekday)
        if not should_catchup(state.get(spec.name), last, now):
            continue
        logger.info("LifeOS 补发任务: %s（上次应发 %s）", spec.name, last)
        try:
            if await spec.action():
                _mark_fired(spec.name)
                fired_jobs.append(spec.name)
        except Exception:
            logger.exception("LifeOS 补发 %s 失败", spec.name)
    return fired_jobs


async def _pending_loop(interval: int = 300) -> None:
    """周期性补发巡检：覆盖开机错过与 agent_ws/LLOneBot 恢复后的补发。"""
    while True:
        try:
            await run_catchup(None)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.debug("LifeOS 补发巡检已取消")
            raise
        except Exception:
            logger.exception("LifeOS 补发巡检异常")


# ------------------------------------------------------------------ #
# 自定义提醒（/提醒，持久化到 logs/lifeos_reminders.json）
# ------------------------------------------------------------------ #

_REMINDER_USAGE = "用法: /提醒 [每天] HH:mm <内容>\n如: /提醒 20:30 收快递、/提醒 每天 07:50 吃药"


@dataclass
class Reminder:
    """一条自定义提醒。

    Attributes:
        id: 自增编号（/提醒列表 展示、/删提醒 按它删除）。
        time: 触发时刻 ``HH:mm``。
        content: 提醒内容。
        daily: 是否每天重复。
        next_fire: 下次触发日期（ISO 格式）。
    """

    id: int
    time: str
    content: str
    daily: bool
    next_fire: str

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可写字典。"""
        return {
            "id": self.id,
            "time": self.time,
            "content": self.content,
            "daily": self.daily,
            "next_fire": self.next_fire,
        }


def _reminders_path() -> Path:
    """自定义提醒持久化文件（仓库 logs/ 下）。"""
    return _REPO_ROOT / "logs" / "lifeos_reminders.json"


def load_reminders(path: Path) -> list[Reminder]:
    """读取提醒列表；缺失/损坏/单条字段异常均容错（跳过坏条目）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    items: list[Reminder] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(
                Reminder(
                    id=int(entry["id"]),
                    time=str(entry["time"]),
                    content=str(entry["content"]),
                    daily=bool(entry["daily"]),
                    next_fire=str(entry["next_fire"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return items


def save_reminders(path: Path, items: list[Reminder]) -> None:
    """原子写回提醒列表（tmp + replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps([r.to_dict() for r in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def parse_reminder_args(raw: str, now: datetime) -> tuple[bool, str, str, str]:
    """解析 /提醒 参数，返回 ``(daily, "HH:mm", content, next_fire ISO 日期)``。

    触发时间 = 当天 HH:mm，若已过则顺延到明天同时间（每天重复同理）。
    解析失败抛 :class:`ValueError`（内容为用法说明）。
    """
    parts = raw.strip().split(None, 2)
    daily = False
    if parts and parts[0] == "每天":
        daily = True
        parts = parts[1:]
    if len(parts) < 2 or not re.fullmatch(r"\d{1,2}:\d{2}", parts[0]):
        raise ValueError(_REMINDER_USAGE)
    try:
        at = _parse_hhmm(parts[0])
    except ValueError:
        raise ValueError(_REMINDER_USAGE) from None
    content = parts[1].strip()
    if not content:
        raise ValueError(_REMINDER_USAGE)
    time_str = f"{at.hour:02d}:{at.minute:02d}"
    day = now.date()
    if datetime.combine(day, at) <= now:
        day += timedelta(days=1)
    return daily, time_str, content, day.isoformat()


def add_reminder(
    path: Path, time_str: str, content: str, daily: bool, next_fire: str
) -> Reminder:
    """追加一条提醒（id 自增）并持久化。"""
    items = load_reminders(path)
    reminder = Reminder(
        id=max((r.id for r in items), default=0) + 1,
        time=time_str,
        content=content,
        daily=daily,
        next_fire=next_fire,
    )
    items.append(reminder)
    save_reminders(path, items)
    return reminder


def delete_reminder(path: Path, reminder_id: int) -> Reminder | None:
    """按编号删除提醒；不存在返回 ``None``。"""
    items = load_reminders(path)
    for i, reminder in enumerate(items):
        if reminder.id == reminder_id:
            removed = items.pop(i)
            save_reminders(path, items)
            return removed
    return None


def render_reminders(items: list[Reminder]) -> str:
    """/提醒列表 的纯文本渲染。"""
    if not items:
        return "当前没有自定义提醒。"
    lines = ["自定义提醒："]
    for r in items:
        kind = "每天" if r.daily else f"{r.next_fire} 单次"
        lines.append(f"{r.id}. {r.time} {r.content}（{kind}）")
    return "\n".join(lines)


def due_reminders(items: list[Reminder], now: datetime) -> list[Reminder]:
    """筛出到期条目：next_fire<=今天 且触发时刻已过（过期日子一并补推）。"""
    due: list[Reminder] = []
    for r in items:
        try:
            fire_dt = datetime.combine(date.fromisoformat(r.next_fire), _parse_hhmm(r.time))
        except ValueError:
            continue
        if fire_dt <= now:
            due.append(r)
    return due


async def fire_due_reminders(
    now: datetime | None = None, path: Path | None = None
) -> list[int]:
    """扫描并推送到期提醒，返回成功推送的 id 列表。

    推送成功：单次提醒删除，每天提醒 next_fire 推到明天；
    推送失败（LLOneBot 掉线）：保留不动，下个 tick 重试。
    """
    now = now or datetime.now()
    path = path or _reminders_path()
    items = load_reminders(path)
    if not items:
        return []
    due_ids = {r.id for r in due_reminders(items, now)}
    fired: list[int] = []
    remaining: list[Reminder] = []
    changed = False
    for r in items:
        if r.id not in due_ids:
            remaining.append(r)
            continue
        if not await _push_text(f"⏰ {r.content}"):
            remaining.append(r)
            continue
        fired.append(r.id)
        changed = True
        if r.daily:
            r.next_fire = (now.date() + timedelta(days=1)).isoformat()
            remaining.append(r)
        # 单次提醒推送成功后删除
    if changed:
        save_reminders(path, remaining)
    return fired


async def _reminders_loop(interval: int = 30) -> None:
    """自定义提醒巡检（30 秒 tick，照 _pending_loop 范式）。"""
    while True:
        try:
            await fire_due_reminders(None)
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.debug("LifeOS 自定义提醒巡检已取消")
            raise
        except Exception:
            logger.exception("LifeOS 自定义提醒巡检异常")


# ------------------------------------------------------------------ #
# 图片灵感记录（superuser 私聊图片 → 灵感草稿/assets + Inbox）
# ------------------------------------------------------------------ #


async def _has_superuser_image(event: MessageEvent) -> bool:
    """图片灵感捕获 rule：私聊 + superuser + 消息含 image segment。

    不满足时返回 ``False``，消息落回现有命令/聊天流程。
    """
    if not isinstance(event, PrivateMessageEvent):
        return False
    if not _is_superuser(event):
        return False
    return any(seg.type == "image" for seg in event.message)


def _guess_ext(source: str) -> str:
    """从 url/路径取图片后缀（剥查询串），取不到默认 ``.jpg``。"""
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{2,5}", suffix):
        return suffix
    return ".jpg"


def _download_image(url: str, dest: Path) -> None:
    """urllib 下载图片（10s 超时；同步函数，经 ``asyncio.to_thread`` 调用）。

    macOS Framework Python 不携带系统 CA 证书，优先用 certifi 的证书库；
    直连不经过系统代理（本机代理可能未运行，QQ 图床国内直连即可）。
    """
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )
    with opener.open(url, timeout=10) as resp:
        dest.write_bytes(resp.read())


async def _save_image(seg: MessageSegment, now: datetime, idx: int) -> str:
    """保存单个 image segment 到 assets，返回 vault 相对路径。

    优先 ``data["url"]``（http/https 下载），其次 ``data["file"]``
    （本地路径存在则复制，LLOneBot 常给本地路径），再次本地 url。
    """
    assert _vault is not None
    url = str(seg.data.get("url") or "")
    file = str(seg.data.get("file") or "")
    # url 常是 CDN 链接不带后缀（此时 _guess_ext 回退 .jpg），优先用 file 字段的真实后缀
    ext = _guess_ext(url)
    if ext == ".jpg" and _guess_ext(file) != ".jpg":
        ext = _guess_ext(file)
    name = f"{now.strftime('%Y-%m-%d_%H-%M-%S')}_{idx}{ext}"
    dest = _safe_join(_vault, "40_收集箱", "灵感草稿", "assets", name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if url.startswith(("http://", "https://")):
        await asyncio.to_thread(_download_image, url, dest)
    elif file and Path(file).exists():
        await asyncio.to_thread(shutil.copyfile, file, dest)
    elif url and Path(url).exists():
        await asyncio.to_thread(shutil.copyfile, url, dest)
    else:
        raise ValueError(f"图片无可用来源: url={url!r} file={file!r}")
    return f"40_收集箱/灵感草稿/assets/{name}"


async def capture_images(segs: list[MessageSegment], now: datetime) -> tuple[list[str], int]:
    """逐张保存图片，返回 ``(vault 相对路径列表, 失败张数)``；单张失败不影响其他。"""
    saved: list[str] = []
    failed = 0
    for idx, seg in enumerate(segs, 1):
        try:
            saved.append(await _save_image(seg, now, idx))
        except Exception:
            logger.exception("第 %d 张图片保存失败", idx)
            failed += 1
    return saved, failed


def record_image_inspiration(vault: Path, saved: list[str], note: str, now: datetime) -> Path:
    """图片链接 + 附注追加 Inbox（一条条目，多张图空格分隔）。"""
    entry = f"[图片] {' '.join(saved)}"
    if note:
        entry += f" {note}"
    return append_inbox(vault, entry, now)


async def _handle_image(bot: Bot, event: MessageEvent) -> None:
    """私聊图片消息：存 assets → 链接+附注追加 Inbox → git 提交 → 回执。"""
    assert _vault is not None
    segs = [seg for seg in event.message if seg.type == "image"]
    if not segs:
        return
    now = datetime.now()
    saved, failed = await capture_images(segs, now)
    note = event.get_plaintext().strip()
    if note and _check_sensitive(note) is not None:
        note = ""
    if saved:
        record_image_inspiration(_vault, saved, note, now)
        await git_commit(_vault, "qq: 图片灵感")
    parts: list[str] = []
    if saved:
        parts.append(f"已存 {len(saved)} 张图到灵感草稿。")
    if failed:
        parts.append(f"{failed} 张保存失败。")
    await _reply(bot, event, "".join(parts) or "图片保存失败。")


class LifeOSManager:
    """LifeOS 定时任务调度器（每日简报 / 每日提醒 / 周日提醒 / 补发巡检 / 自定义提醒）。"""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """启动全部后台任务（在 nonebot on_startup 中调用）。"""
        if self._tasks:
            raise RuntimeError("LifeOS scheduler is already running")
        specs = _job_specs()
        self._tasks = [
            asyncio.create_task(_job_loop(spec), name=f"lifeos-{spec.name}")
            for spec in specs
        ]
        self._tasks.append(asyncio.create_task(_pending_loop(), name="lifeos-catchup"))
        self._tasks.append(asyncio.create_task(_reminders_loop(), name="lifeos-reminders"))
        logger.info("LifeOS 定时任务已启动: %s", [spec.name for spec in specs])

    def stop(self) -> None:
        """取消全部后台任务。"""
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks = []
        logger.info("LifeOS 定时任务已停止")
