"""LifeOS 扩展测试：直写命令、渲染、提醒判定、定时与补发、persona 路由。

I/O 胶水（nonebot matcher 本身）不在测试范围；纯逻辑全部覆盖。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)

from acp_qq_bridge.adapters import lifeos
from acp_qq_bridge.config import LifeOSConfig, load_config

# ------------------------------------------------------------------ #
# 夹具：tmp_path 假 vault + 模块级状态装配
# ------------------------------------------------------------------ #

_TPL_DIARY = """---
date: {{date}}
能量:
---
## 推进
- [轨道名] 干了什么（一行一条，如：- [拉丁语] ch1 练习 1-5）

## 休闲
- 游戏 / 散步 / 审美积累 / 摆烂（各一行，记时长随意，不评判）

## 状态与灵感（随便写）


## 明天第一件事
"""

_OVERVIEW = """# 总览

| 轨道 | 阶段 | 一句话状态 | 更新 |
|---|---|---|---|
| 拉丁语 | S1 | 刚建档 | 2026-07-19 |
| 题库系统 | 封盘冲刺 | 砍功能中 | 2026-07-19 |
"""

_STATE_LATIN = """---
track: 拉丁语
---
## 当前阶段与验收标准
略

## 下一步
- [ ] 搭 Anki 牌组（LTRL ch1 词表）
- [x] 建档
- [ ] ch1 课文 + 练习册

## 卡点
（无）

## 日志
- 2026-07-19 建档
"""

_STATE_QUIZ = """---
track: 题库系统
---
## 下一步
- [x] 已完成的事

## 卡点
（无）
"""


def _make_vault(root: Path) -> Path:
    """在 *root* 下搭一个最小 LifeOS vault。"""
    (root / "40_收集箱").mkdir(parents=True)
    (root / "40_收集箱" / "Inbox.md").write_text(
        "# Inbox（唯一捕获入口）\n\n## 未分拣\n", encoding="utf-8"
    )
    (root / "00_系统" / "模板").mkdir(parents=True)
    (root / "00_系统" / "模板" / "tpl_日记.md").write_text(_TPL_DIARY, encoding="utf-8")
    (root / "20_进度").mkdir(parents=True)
    (root / "20_进度" / "_总览.md").write_text(_OVERVIEW, encoding="utf-8")
    (root / "20_进度" / "拉丁语_STATE.md").write_text(_STATE_LATIN, encoding="utf-8")
    (root / "20_进度" / "题库系统_STATE.md").write_text(_STATE_QUIZ, encoding="utf-8")
    (root / "30_日志" / "每日").mkdir(parents=True)
    (root / "30_日志" / "周结").mkdir(parents=True)
    return root


def _make_config(vault: Path) -> LifeOSConfig:
    return LifeOSConfig(
        enabled=True,
        vault_path=str(vault),
        target_qq="3058442393",
        plain_message_mode="agent",
        daily_brief_time="04:00",
        reminder_time="22:30",
        weekly_reminder={"weekday": 6, "time": "10:00"},
        persona="kaltsit",
        state_file=str(vault / "lifeos_state.json"),
    )


def _setup(
    vault: Path,
    agent_ws: Any | None = None,
    *,
    connected: bool = True,
) -> Any:
    """装配 lifeos 模块级状态（ duck-typed agent_ws / security / persona）。"""
    if agent_ws is None:
        agent_ws = AsyncMock()
    agent_ws.is_connected = connected
    security = SimpleNamespace(
        validate_command=lambda text, strict=False: SimpleNamespace(passed=True, reason=None)
    )
    persona = SimpleNamespace(build_system_prompt=lambda pid: f"PROMPT[{pid}]")
    lifeos.setup_lifeos(_make_config(vault), agent_ws, security, persona)
    return agent_ws


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    return _make_vault(tmp_path / "vault")


# ------------------------------------------------------------------ #
# 配置与鉴权
# ------------------------------------------------------------------ #


def test_config_defaults_include_lifeos() -> None:
    """load_config 默认值包含 lifeos section 与 agent.model。"""
    cfg = load_config(None)
    assert cfg.lifeos.enabled is True
    assert cfg.lifeos.target_qq == "3058442393"
    assert cfg.lifeos.daily_brief_time == "04:00"
    assert cfg.lifeos.weekly_reminder == {"weekday": 6, "time": "10:00"}
    assert cfg.lifeos.persona == "kaltsit"
    assert cfg.agent.model == ""


def test_is_superuser(vault: Path) -> None:
    """鉴权：硬编码 superuser 通过，其他人拒绝（零响应由 handler 早退保证）。"""
    _setup(vault)
    assert lifeos._is_superuser(SimpleNamespace(user_id=3058442393)) is True
    assert lifeos._is_superuser(SimpleNamespace(user_id=12345)) is False


# ------------------------------------------------------------------ #
# 直写命令的文件写入格式
# ------------------------------------------------------------------ #


def test_append_inbox_format(vault: Path) -> None:
    """/记：Inbox 末尾追加 `- YYYY-MM-DD HH:mm | 内容`。"""
    now = datetime(2026, 7, 19, 23, 5)
    lifeos.append_inbox(vault, "查一下铂丝电极电流密度", now)
    text = (vault / "40_收集箱" / "Inbox.md").read_text(encoding="utf-8")
    assert text.endswith("- 2026-07-19 23:05 | 查一下铂丝电极电流密度\n")


def test_daka_creates_diary_and_appends(vault: Path) -> None:
    """/打卡：日记不存在时按模板创建（剥离示例行），并写入"推进"小节。"""
    day = date(2026, 7, 20)
    lifeos.append_diary_section(vault, day, "推进", "- [拉丁语] ch1 练习 1-5")
    text = (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8")
    assert "date: 2026-07-20" in text
    assert "- [拉丁语] ch1 练习 1-5" in text
    # 模板示例行不应进入正式日记
    assert "[轨道名]" not in text
    # 条目应在"推进"小节前位于"休闲"标题之前
    assert text.index("- [拉丁语] ch1 练习 1-5") < text.index("## 休闲")


def test_daka_appends_in_order(vault: Path) -> None:
    """同一小节多次追加保持顺序。"""
    day = date(2026, 7, 20)
    lifeos.append_diary_section(vault, day, "推进", "- [拉丁语] 第一条")
    lifeos.append_diary_section(vault, day, "推进", "- [题库系统] 第二条")
    section = lifeos._extract_section_lines(
        (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8"), "推进"
    )
    entries = [line for line in section if line.strip().startswith("- ")]
    assert entries == ["- [拉丁语] 第一条", "- [题库系统] 第二条"]


def test_xiuxian_goes_to_leisure_section(vault: Path) -> None:
    """/休闲：写入"休闲"小节而非"推进"。"""
    day = date(2026, 7, 20)
    lifeos.append_diary_section(vault, day, "休闲", "- 游戏 1.5h")
    text = (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8")
    leisure = lifeos._extract_section_lines(text, "休闲")
    progress = lifeos._extract_section_lines(text, "推进")
    assert "- 游戏 1.5h" in leisure
    assert not [line for line in progress if line.strip().startswith("- ")]


def test_set_diary_energy(vault: Path) -> None:
    """/能量：写入并覆盖 frontmatter 的 能量 键。"""
    day = date(2026, 7, 20)
    lifeos.set_diary_energy(vault, day, 7)
    lifeos.set_diary_energy(vault, day, 5)
    text = (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8")
    assert "能量: 5" in text
    assert text.count("能量:") == 1


def test_safe_join_rejects_escape(vault: Path) -> None:
    """路径越界检查：resolve 后不在 vault 内即拒绝。"""
    with pytest.raises(ValueError, match="越界"):
        lifeos._safe_join(vault, "..", "evil.md")


# ------------------------------------------------------------------ #
# git 提交（tmp 下真实 init 一个 repo）
# ------------------------------------------------------------------ #


async def _git(vault: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(vault), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


async def test_git_commit_roundtrip(vault: Path) -> None:
    """git add -A && commit：有改动产生提交，无改动返回 False 不算错误。"""
    await _git(vault, "init")
    await _git(vault, "config", "user.email", "test@example.com")
    await _git(vault, "config", "user.name", "test")

    lifeos.append_inbox(vault, "第一条", datetime(2026, 7, 19, 12, 0))
    assert await lifeos.git_commit(vault, "qq: 记 第一条") is True
    log = await _git(vault, "log", "--oneline")
    assert "qq: 记 第一条" in log

    # 无改动：不算错误，返回 False
    assert await lifeos.git_commit(vault, "qq: 空提交") is False


# ------------------------------------------------------------------ #
# 只读渲染
# ------------------------------------------------------------------ #


def test_render_overview(vault: Path) -> None:
    """/总览：markdown 表格转逐行纯文本。"""
    text = lifeos.render_overview(vault)
    assert "进度总览：" in text
    assert "- 拉丁语：S1，刚建档（2026-07-19）" in text
    assert "- 题库系统：封盘冲刺，砍功能中（2026-07-19）" in text
    assert "|" not in text


def test_render_next_steps(vault: Path) -> None:
    """/下步：聚合各 STATE 未勾选项，按轨道分组，排除已完成。"""
    text = lifeos.render_next_steps(vault)
    assert "【拉丁语】" in text
    assert "- 搭 Anki 牌组（LTRL ch1 词表）" in text
    assert "- ch1 课文 + 练习册" in text
    assert "建档" not in text  # - [x] 已勾选的不出现
    # 题库系统全部完成 → 不出现在分组里
    assert "【题库系统】" not in text


def test_apply_decision(vault: Path) -> None:
    """/决策：勾选最新周结"需要我决策的"第 N 项并追加决定。"""
    weekly = vault / "30_日志" / "周结" / "2026-W29.md"
    weekly.write_text(
        "## 各线进展\n略\n\n## 需要我决策的\n- [ ] 要不要砍 TEG\n- [ ] 拉丁语是否加量\n",
        encoding="utf-8",
    )
    item = lifeos.apply_decision(vault, 2, "不加量，保持 45min")
    assert item == "拉丁语是否加量"
    text = weekly.read_text(encoding="utf-8")
    assert "- [ ] 要不要砍 TEG" in text
    assert "- [x] 拉丁语是否加量 → 决定：不加量，保持 45min" in text

    with pytest.raises(ValueError, match="第 3 项不存在"):
        lifeos.apply_decision(vault, 3, "x")


# ------------------------------------------------------------------ #
# 提醒判定与时间计算
# ------------------------------------------------------------------ #


def test_should_remind(vault: Path) -> None:
    """提醒判定：日记缺失 → 提醒；推进为空 → 提醒；已记录 → 不提醒。"""
    assert lifeos.should_remind(None) is True

    day = date(2026, 7, 20)
    lifeos._ensure_diary(vault, day)  # 模板创建的空日记
    empty_text = (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8")
    assert lifeos.should_remind(empty_text) is True

    lifeos.append_diary_section(vault, day, "推进", "- [拉丁语] ch1")
    filled_text = (vault / "30_日志" / "每日" / "2026-07-20.md").read_text(encoding="utf-8")
    assert lifeos.should_remind(filled_text) is False


def test_next_and_last_scheduled_daily() -> None:
    """每日任务：next/last 跨午夜计算正确。"""
    at = dtime(4, 0)
    early = datetime(2026, 7, 19, 3, 0)
    assert lifeos.next_scheduled(early, at) == datetime(2026, 7, 19, 4, 0)
    assert lifeos.last_scheduled(early, at) == datetime(2026, 7, 18, 4, 0)

    late = datetime(2026, 7, 19, 5, 0)
    assert lifeos.next_scheduled(late, at) == datetime(2026, 7, 20, 4, 0)
    assert lifeos.last_scheduled(late, at) == datetime(2026, 7, 19, 4, 0)


def test_next_and_last_scheduled_weekly() -> None:
    """每周任务：2026-07-19 是周日（weekday=6）。"""
    at = dtime(10, 0)
    before = datetime(2026, 7, 19, 9, 0)  # 周日 09:00
    assert lifeos.next_scheduled(before, at, weekday=6) == datetime(2026, 7, 19, 10, 0)
    assert lifeos.last_scheduled(before, at, weekday=6) == datetime(2026, 7, 12, 10, 0)

    after = datetime(2026, 7, 19, 11, 0)  # 周日 11:00
    assert lifeos.next_scheduled(after, at, weekday=6) == datetime(2026, 7, 26, 10, 0)
    assert lifeos.last_scheduled(after, at, weekday=6) == datetime(2026, 7, 19, 10, 0)


def test_should_catchup() -> None:
    """补发判定：未发过/发过但过期 → 补；发过当期 → 不补；应发未到 → 不补。"""
    now = datetime(2026, 7, 19, 5, 0)
    last = datetime(2026, 7, 19, 4, 0)
    assert lifeos.should_catchup(None, last, now) is True
    assert lifeos.should_catchup("2026-07-18", last, now) is True
    assert lifeos.should_catchup("2026-07-19", last, now) is False
    assert lifeos.should_catchup("bad-date", last, now) is True
    future = datetime(2026, 7, 20, 4, 0)
    assert lifeos.should_catchup(None, future, now) is False


def test_state_file_roundtrip(vault: Path) -> None:
    """state_file 读写往返；缺失/损坏时返回空表。"""
    path = vault / "state.json"
    assert lifeos.load_state(path) == {}
    lifeos.save_state(path, {"daily_brief": "2026-07-19"})
    assert lifeos.load_state(path) == {"daily_brief": "2026-07-19"}
    path.write_text("not json", encoding="utf-8")
    assert lifeos.load_state(path) == {}


# ------------------------------------------------------------------ #
# 04:00 简报（agent 通道）与重试
# ------------------------------------------------------------------ #


async def test_daily_brief_upstream_message(vault: Path) -> None:
    """04:00 简报：UpstreamMessage 含固定提示词 + work_dir=vault，persona 注入一次。"""
    agent_ws = _setup(vault)

    async def _deliver_on_user_input(msg: Any) -> None:
        if msg.action == "user_input":
            lifeos.notify_lifeos_delivered(True)

    agent_ws.send_message.side_effect = _deliver_on_user_input
    assert await lifeos.fire_daily_brief() is True

    assert agent_ws.send_message.await_count == 2  # persona 注入 + 任务提示词
    inject_msg = agent_ws.send_message.await_args_list[0].args[0]
    assert inject_msg.action == "inject"
    assert inject_msg.payload.text == "__SET_PERSONA__"
    assert inject_msg.payload.raw_signal == "PROMPT[kaltsit]"

    brief_msg = agent_ws.send_message.await_args_list[1].args[0]
    assert brief_msg.session_id == lifeos.LIFEOS_SESSION_ID
    assert brief_msg.action == "user_input"
    assert "日结 SOP" in brief_msg.payload.text
    assert brief_msg.payload.work_dir == str(vault.resolve())

    # 第二次触发：persona 不重复注入
    assert await lifeos.fire_daily_brief() is True
    assert agent_ws.send_message.await_count == 3


async def test_daily_brief_delivery_failure_marks_pending(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """投递失败（如下行时 LLOneBot 掉线）→ 返回 False，等待补发，不误标 last_fired。"""
    agent_ws = _setup(vault)

    async def _fail_delivery(msg: Any) -> None:
        if msg.action == "user_input":
            lifeos.notify_lifeos_delivered(False)

    agent_ws.send_message.side_effect = _fail_delivery
    assert await lifeos.fire_daily_brief() is False


async def test_daily_brief_timeout_marks_pending(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent 一直无回执 → 超时返回 False，不误标 last_fired。"""
    _setup(vault)
    monkeypatch.setattr(lifeos, "_BRIEF_TIMEOUT", 0.05)
    assert await lifeos.fire_daily_brief() is False


async def test_daily_brief_retry_gives_up(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_ws 断连：重试 1 次后放弃，返回 False 等待补发。"""
    agent_ws = _setup(vault, connected=False)
    monkeypatch.setattr(lifeos, "_RETRY_DELAY", 0)
    assert await lifeos.fire_daily_brief() is False
    assert agent_ws.send_message.await_count == 0


# ------------------------------------------------------------------ #
# 提醒推送与补发逻辑
# ------------------------------------------------------------------ #


async def test_fire_reminder(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """22:30 提醒：无日记 → 推送；已记录 → 不推送但任务算完成。"""
    _setup(vault)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(lifeos, "_push_text", push)

    day = date(2026, 7, 20)
    assert await lifeos.fire_reminder(today=day) is True
    assert push.await_count == 1
    assert "/记" in push.await_args.args[0]

    push.reset_mock()
    lifeos.append_diary_section(vault, day, "推进", "- [拉丁语] ch1")
    assert await lifeos.fire_reminder(today=day) is True
    assert push.await_count == 0


async def test_run_catchup(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """补发：last_fired 缺失 → 补发最近一次并写回 state；当期已发 → 不补。"""
    _setup(vault)
    monkeypatch.setattr(lifeos, "fire_daily_brief", AsyncMock(return_value=True))
    monkeypatch.setattr(lifeos, "fire_reminder", AsyncMock(return_value=True))
    monkeypatch.setattr(lifeos, "fire_weekly_reminder", AsyncMock(return_value=True))

    now = datetime(2026, 7, 19, 12, 0)  # 周日中午，三个 job 的最近应发时刻都已过
    fired = await lifeos.run_catchup(now)
    assert fired == ["daily_brief", "reminder", "weekly_reminder"]

    state = lifeos.load_state(vault / "lifeos_state.json")
    assert state == dict.fromkeys(fired, "2026-07-19")

    # 同一时间再跑：当期已发，不再补
    assert await lifeos.run_catchup(now) == []


# ------------------------------------------------------------------ #
# persona 跳过与周结 .qq.txt
# ------------------------------------------------------------------ #


def test_lifeos_session_routing(vault: Path) -> None:
    """LifeOS 会话命中改路由判定；普通会话不命中。"""
    _setup(vault)
    assert lifeos.is_lifeos_session(lifeos.LIFEOS_SESSION_ID) is True
    assert lifeos.is_lifeos_session("abcd1234efgh") is False
    assert lifeos.get_target_qq() == "3058442393"


def test_consume_fresh_qq_txt(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/周结 后：新鲜的 .qq.txt 被消费一次；无等待标记时不消费。"""
    _setup(vault)
    weekly_txt = vault / "30_日志" / "周结" / "2026-W29.qq.txt"
    weekly_txt.write_text("本周汇报精简版\n1. 待决策一\n", encoding="utf-8")

    # 无 /周结 等待标记 → 不消费
    assert lifeos.consume_fresh_qq_txt() is None

    monkeypatch.setattr(lifeos, "_weekly_pending_since", time.time() - 10)
    assert lifeos.consume_fresh_qq_txt() == "本周汇报精简版\n1. 待决策一"
    # 只消费一次
    assert lifeos.consume_fresh_qq_txt() is None


# ------------------------------------------------------------------ #
# 自定义提醒（/提醒）
# ------------------------------------------------------------------ #


def test_parse_reminder_single(vault: Path) -> None:
    """解析：单次提醒，未来时间当天触发。"""
    _setup(vault)
    daily, time_str, content, next_fire = lifeos.parse_reminder_args(
        "20:30 收快递", datetime(2026, 7, 19, 10, 0)
    )
    assert (daily, time_str, content, next_fire) == (False, "20:30", "收快递", "2026-07-19")


def test_parse_reminder_rolls_to_tomorrow(vault: Path) -> None:
    """解析：当天时间已过 → 顺延到明天同时间。"""
    _setup(vault)
    _, _, _, next_fire = lifeos.parse_reminder_args("20:30 收快递", datetime(2026, 7, 19, 21, 0))
    assert next_fire == "2026-07-20"


def test_parse_reminder_daily(vault: Path) -> None:
    """解析："每天"作为可选第一参数。"""
    _setup(vault)
    daily, time_str, content, next_fire = lifeos.parse_reminder_args(
        "每天 07:50 吃药", datetime(2026, 7, 19, 6, 0)
    )
    assert (daily, time_str, content, next_fire) == (True, "07:50", "吃药", "2026-07-19")


@pytest.mark.parametrize("raw", ["", "收快递", "abc 吃药", "20:30", "25:00 吃药", "每天"])
def test_parse_reminder_errors(vault: Path, raw: str) -> None:
    """解析错误（无时间/格式不对/无内容）→ ValueError 带用法说明。"""
    _setup(vault)
    with pytest.raises(ValueError, match="用法"):
        lifeos.parse_reminder_args(raw, datetime(2026, 7, 19, 10, 0))


def test_reminder_persistence_roundtrip(vault: Path, tmp_path: Path) -> None:
    """持久化：id 自增、读写往返、按编号删除、原子写不留 .tmp。"""
    _setup(vault)
    path = tmp_path / "reminders.json"
    r1 = lifeos.add_reminder(path, "20:30", "收快递", False, "2026-07-19")
    r2 = lifeos.add_reminder(path, "07:50", "吃药", True, "2026-07-20")
    assert (r1.id, r2.id) == (1, 2)

    items = lifeos.load_reminders(path)
    assert [r.id for r in items] == [1, 2]
    assert items[1].daily is True
    assert items[0].to_dict() == {
        "id": 1,
        "time": "20:30",
        "content": "收快递",
        "daily": False,
        "next_fire": "2026-07-19",
    }

    removed = lifeos.delete_reminder(path, 1)
    assert removed is not None and removed.content == "收快递"
    assert [r.id for r in lifeos.load_reminders(path)] == [2]
    assert lifeos.delete_reminder(path, 99) is None
    assert not (tmp_path / "reminders.json.tmp").exists()

    # 损坏文件容错
    path.write_text("not json", encoding="utf-8")
    assert lifeos.load_reminders(path) == []


async def test_fire_due_reminders(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """到期扫描：单次推送后删除，每天条目 next_fire 推一天，未到期不动。"""
    _setup(vault)
    path = tmp_path / "reminders.json"
    lifeos.add_reminder(path, "20:30", "收快递", False, "2026-07-19")
    lifeos.add_reminder(path, "07:50", "吃药", True, "2026-07-19")
    lifeos.add_reminder(path, "23:00", "未到期", False, "2026-07-20")

    push = AsyncMock(return_value=True)
    monkeypatch.setattr(lifeos, "_push_text", push)
    fired = await lifeos.fire_due_reminders(now=datetime(2026, 7, 19, 21, 0), path=path)

    assert fired == [1, 2]
    assert push.await_count == 2
    assert push.await_args_list[0].args[0] == "⏰ 收快递"

    items = lifeos.load_reminders(path)
    assert [r.id for r in items] == [2, 3]  # 单次已删，未到期保留
    assert items[0].next_fire == "2026-07-20"  # 每天条目推到明天


async def test_fire_due_reminders_push_failure_keeps(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """推送失败（LLOneBot 掉线）：条目保留不动，文件不写回，下 tick 重试。"""
    _setup(vault)
    path = tmp_path / "reminders.json"
    lifeos.add_reminder(path, "20:30", "收快递", False, "2026-07-19")
    before = path.read_text(encoding="utf-8")

    monkeypatch.setattr(lifeos, "_push_text", AsyncMock(return_value=False))
    fired = await lifeos.fire_due_reminders(now=datetime(2026, 7, 19, 21, 0), path=path)

    assert fired == []
    assert path.read_text(encoding="utf-8") == before


def test_due_reminders_catchup_missed_day(vault: Path) -> None:
    """关机补推：next_fire 是过去日期的条目开机即到期。"""
    _setup(vault)
    items = [
        lifeos.Reminder(1, "08:00", "昨天的提醒", False, "2026-07-18"),
        lifeos.Reminder(2, "23:59", "今天但时刻未到", False, "2026-07-19"),
    ]
    due = lifeos.due_reminders(items, datetime(2026, 7, 19, 9, 0))
    assert [r.id for r in due] == [1]


def test_render_reminders(vault: Path) -> None:
    """/提醒列表 渲染：编号、时间、内容、单次/每天。"""
    _setup(vault)
    assert lifeos.render_reminders([]) == "当前没有自定义提醒。"
    items = [
        lifeos.Reminder(1, "20:30", "收快递", False, "2026-07-19"),
        lifeos.Reminder(2, "07:50", "吃药", True, "2026-07-20"),
    ]
    text = lifeos.render_reminders(items)
    assert "1. 20:30 收快递（2026-07-19 单次）" in text
    assert "2. 07:50 吃药（每天）" in text


# ------------------------------------------------------------------ #
# 图片灵感记录
# ------------------------------------------------------------------ #


def _image_seg(**data: str) -> MessageSegment:
    return MessageSegment(type="image", data=data)


def test_guess_ext(vault: Path) -> None:
    """后缀推断：剥查询串、小写化、取不到默认 .jpg。"""
    _setup(vault)
    assert lifeos._guess_ext("http://x.com/a/photo.PNG?size=large") == ".png"
    assert lifeos._guess_ext("/tmp/noext") == ".jpg"
    assert lifeos._guess_ext("img.webp") == ".webp"


async def test_save_image_local_file(vault: Path, tmp_path: Path) -> None:
    """本地 file 路径直接复制到 assets，返回 vault 相对路径。"""
    _setup(vault)
    src = tmp_path / "pic.png"
    src.write_bytes(b"\x89PNG-fake")
    seg = _image_seg(file=str(src), url="")
    rel = await lifeos._save_image(seg, datetime(2026, 7, 19, 12, 0, 0), 1)
    assert rel == "40_收集箱/灵感草稿/assets/2026-07-19_12-00-00_1.png"
    dest = vault / "40_收集箱" / "灵感草稿" / "assets" / "2026-07-19_12-00-00_1.png"
    assert dest.read_bytes() == b"\x89PNG-fake"


async def test_capture_images_partial_failure(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端保存逻辑：本地下载混合，单张失败不影响其他，返回失败张数。"""
    _setup(vault)
    src = tmp_path / "a.jpg"
    src.write_bytes(b"jpg-fake")

    def fake_download(url: str, dest: Path) -> None:
        dest.write_bytes(b"downloaded")

    monkeypatch.setattr(lifeos, "_download_image", fake_download)
    segs = [
        _image_seg(file=str(src), url=""),  # 本地复制
        _image_seg(url="http://example.com/b.png", file="b.png"),  # 走下载（mock）
        _image_seg(file="", url=""),  # 无来源 → 失败
    ]
    saved, failed = await lifeos.capture_images(segs, datetime(2026, 7, 19, 12, 0, 0))
    assert failed == 1
    assert len(saved) == 2
    assert saved[1].endswith("_2.png")
    assets = vault / "40_收集箱" / "灵感草稿" / "assets"
    assert (assets / "2026-07-19_12-00-00_2.png").read_bytes() == b"downloaded"


def test_record_image_inspiration_format(vault: Path) -> None:
    """Inbox 追加格式：- YYYY-MM-DD HH:mm | [图片] <链接...> <附注>。"""
    _setup(vault)
    lifeos.record_image_inspiration(
        vault,
        ["40_收集箱/灵感草稿/assets/a.jpg", "40_收集箱/灵感草稿/assets/b.jpg"],
        "杯垫散热想法",
        datetime(2026, 7, 19, 12, 5),
    )
    text = (vault / "40_收集箱" / "Inbox.md").read_text(encoding="utf-8")
    assert text.endswith(
        "- 2026-07-19 12:05 | [图片] "
        "40_收集箱/灵感草稿/assets/a.jpg 40_收集箱/灵感草稿/assets/b.jpg 杯垫散热想法\n"
    )


def _private_event(user_id: int, message: Message) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=0,
        self_id=1,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=1,
        user_id=user_id,
        message=message,
        raw_message="",
        font=0,
        sender={"user_id": user_id, "nickname": "t"},
    )


async def test_image_rule(vault: Path) -> None:
    """图片 rule：私聊+superuser+含 image 才命中；其余放行到现有流程。"""
    _setup(vault)
    image_msg = Message([_image_seg(file="http://x/a.png", url="http://x/a.png")])
    assert await lifeos._has_superuser_image(_private_event(3058442393, image_msg)) is True
    # 无图片 → 不拦普通文字
    assert await lifeos._has_superuser_image(_private_event(3058442393, Message("你好"))) is False
    # 非 superuser → 不拦
    assert await lifeos._has_superuser_image(_private_event(12345, image_msg)) is False
    # 群聊 → 不拦
    group_event = GroupMessageEvent(
        time=0,
        self_id=1,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=1,
        user_id=3058442393,
        group_id=999,
        message=image_msg,
        raw_message="",
        font=0,
        sender={"user_id": 3058442393, "nickname": "t"},
    )
    assert await lifeos._has_superuser_image(group_event) is False
