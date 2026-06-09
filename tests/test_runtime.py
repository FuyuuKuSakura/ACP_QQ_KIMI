from __future__ import annotations

import time

import pytest

from acp_qq_bridge.core.runtime import (
    SessionAlreadyBoundError,
    SessionManager,
    SessionNotFoundError,
)


@pytest.mark.asyncio
async def test_create_session() -> None:
    """创建会话，验证 session_id 生成、默认状态 idle。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")

    assert meta.session_id
    assert len(meta.session_id) == 12
    assert meta.status == "idle"
    assert meta.agent_source == "cli"


@pytest.mark.asyncio
async def test_bind_qq() -> None:
    """绑定 QQ，验证双向映射。"""
    mgr = SessionManager()
    meta = await mgr.create_session("vscode-extension")
    await mgr.bind_qq(meta.session_id, "12345", "private")

    by_qq = await mgr.get_by_qq("12345")
    by_session = await mgr.get_by_session(meta.session_id)

    assert by_qq is not None
    assert by_session is not None
    assert by_qq.session_id == meta.session_id
    assert by_session.qq_id == "12345"
    assert by_qq.qq_type == "private"


@pytest.mark.asyncio
async def test_bind_qq_already_bound() -> None:
    """重复绑定应抛 SessionAlreadyBoundError。"""
    mgr = SessionManager()
    meta1 = await mgr.create_session("cli")
    meta2 = await mgr.create_session("cli")
    await mgr.bind_qq(meta1.session_id, "99999", "group")

    with pytest.raises(SessionAlreadyBoundError) as exc_info:
        await mgr.bind_qq(meta2.session_id, "99999", "group")

    assert exc_info.value.qq_id == "99999"
    assert exc_info.value.existing_session_id == meta1.session_id


@pytest.mark.asyncio
async def test_get_by_qq() -> None:
    """通过 QQ ID 查找会话。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")
    await mgr.bind_qq(meta.session_id, "11111", "private")

    found = await mgr.get_by_qq("11111")
    assert found is not None
    assert found.session_id == meta.session_id

    not_found = await mgr.get_by_qq("00000")
    assert not_found is None


@pytest.mark.asyncio
async def test_get_by_session() -> None:
    """通过 session_id 查找。"""
    mgr = SessionManager()
    meta = await mgr.create_session("vscode-extension")

    found = await mgr.get_by_session(meta.session_id)
    assert found is not None
    assert found.agent_source == "vscode-extension"

    not_found = await mgr.get_by_session("nonexistent")
    assert not_found is None


@pytest.mark.asyncio
async def test_update_status() -> None:
    """更新状态 thinking -> executing -> idle。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")

    await mgr.update_status(meta.session_id, "thinking")
    assert (await mgr.get_by_session(meta.session_id)).status == "thinking"

    await mgr.update_status(meta.session_id, "executing")
    assert (await mgr.get_by_session(meta.session_id)).status == "executing"

    await mgr.update_status(meta.session_id, "idle")
    assert (await mgr.get_by_session(meta.session_id)).status == "idle"


@pytest.mark.asyncio
async def test_interrupt_session() -> None:
    """打断会话，状态变为 interrupted。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")
    await mgr.update_status(meta.session_id, "thinking")

    updated = await mgr.interrupt_session(meta.session_id)
    assert updated.status == "interrupted"
    assert (await mgr.get_by_session(meta.session_id)).status == "interrupted"


@pytest.mark.asyncio
async def test_close_session() -> None:
    """关闭会话，双向映射清除。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")
    await mgr.bind_qq(meta.session_id, "22222", "group")

    await mgr.close_session(meta.session_id)

    assert await mgr.get_by_session(meta.session_id) is None
    assert await mgr.get_by_qq("22222") is None


@pytest.mark.asyncio
async def test_session_not_found() -> None:
    """操作不存在的会话应抛 SessionNotFoundError。"""
    mgr = SessionManager()

    with pytest.raises(SessionNotFoundError):
        await mgr.update_status("no-such-id", "idle")
    with pytest.raises(SessionNotFoundError):
        await mgr.interrupt_session("no-such-id")
    with pytest.raises(SessionNotFoundError):
        await mgr.get_context("no-such-id")


@pytest.mark.asyncio
async def test_append_and_get_context() -> None:
    """上下文追加和获取，验证最多保留 max_context 条。"""
    mgr = SessionManager(max_context=3)
    meta = await mgr.create_session("cli")

    await mgr.append_context(meta.session_id, {"role": "user", "text": "1"})
    await mgr.append_context(meta.session_id, {"role": "user", "text": "2"})
    await mgr.append_context(meta.session_id, {"role": "user", "text": "3"})
    ctx = await mgr.get_context(meta.session_id)
    assert len(ctx) == 3

    await mgr.append_context(meta.session_id, {"role": "user", "text": "4"})
    ctx = await mgr.get_context(meta.session_id)
    assert len(ctx) == 3
    assert ctx[0]["text"] == "2"
    assert ctx[-1]["text"] == "4"


@pytest.mark.asyncio
async def test_cleanup_expired() -> None:
    """创建旧会话（修改 last_active），验证 cleanup 能清理。"""
    mgr = SessionManager(ttl=60)
    meta = await mgr.create_session("cli")
    # 将 last_active 回溯到超过 ttl 的时间
    meta.last_active = time.time() - 120

    expired = await mgr.cleanup_expired()
    assert meta.session_id in expired
    assert await mgr.get_by_session(meta.session_id) is None


@pytest.mark.asyncio
async def test_list_sessions() -> None:
    """列出多个会话。"""
    mgr = SessionManager()
    meta1 = await mgr.create_session("cli")
    meta2 = await mgr.create_session("vscode-extension")

    sessions = await mgr.list_sessions()
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert ids == {meta1.session_id, meta2.session_id}


@pytest.mark.asyncio
async def test_unbind_qq() -> None:
    """解绑后 get_by_qq 返回 None。"""
    mgr = SessionManager()
    meta = await mgr.create_session("cli")
    await mgr.bind_qq(meta.session_id, "33333", "private")

    await mgr.unbind_qq("33333")

    assert await mgr.get_by_qq("33333") is None
    by_session = await mgr.get_by_session(meta.session_id)
    assert by_session.qq_id is None
    assert by_session.qq_type is None
