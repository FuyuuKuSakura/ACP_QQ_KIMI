from __future__ import annotations

from unittest.mock import patch

import pytest

from acp_qq_bridge.middleware.persona import (
    PersonaSkill,
    PersonaTemplate,
    load_personas_from_dir,
)


@pytest.fixture
def skill() -> PersonaSkill:
    """Return a PersonaSkill using built-in personas."""
    return PersonaSkill()


def test_transform_assistant(skill: PersonaSkill) -> None:
    """assistant 人设基本不变（无 prefix/suffix，关闭随机注入）。"""
    text = "这是一段普通回复"
    with patch("acp_qq_bridge.middleware.persona.random.random", return_value=1.0):
        result = skill.transform(text, persona_id="assistant")
    assert result == text


def test_transform_cute(skill: PersonaSkill) -> None:
    """cute 人设应添加颜文字和萌系前缀/后缀。"""
    text = "你好呀"
    with patch("acp_qq_bridge.middleware.persona.random.random", return_value=1.0):
        result = skill.transform(text, persona_id="cute")
    assert "好哒~" in result
    assert text in result


def test_transform_sarcastic(skill: PersonaSkill) -> None:
    """sarcastic 人设应有毒舌语气（随机注入 tone_keywords）。"""
    text = "代码写完了"
    with (
        patch("acp_qq_bridge.middleware.persona.random.random", return_value=0.0),
        patch(
            "acp_qq_bridge.middleware.persona.random.choice",
            side_effect=lambda seq: seq[0],
        ),
    ):
        result = skill.transform(text, persona_id="sarcastic")
    assert text in result
    # 第一个 tone_keyword 是 "啧啧"
    assert "啧啧" in result


def test_transform_geek(skill: PersonaSkill) -> None:
    """geek 人设应有 >> 前缀。"""
    text = "init system"
    with patch("acp_qq_bridge.middleware.persona.random.random", return_value=1.0):
        result = skill.transform(text, persona_id="geek")
    assert result.startswith(">>")
    assert text in result


def test_transform_long_text(skill: PersonaSkill) -> None:
    """超长文本截断。"""
    text = "x" * 5000
    result = skill.transform(text, persona_id="assistant")
    assert len(result) <= 4096
    assert result.endswith("...")


def test_transform_acp_tag_preserved(skill: PersonaSkill) -> None:
    """<acp:cmd>...</acp:cmd> 标签不被破坏。"""
    text = "<acp:cmd>run tests</acp:cmd>"
    with patch("acp_qq_bridge.middleware.persona.random.random", return_value=1.0):
        result = skill.transform(text, persona_id="assistant")
    assert "<acp:cmd>" in result
    assert "</acp:cmd>" in result
    assert "run tests" in result


def test_get_available_personas(skill: PersonaSkill) -> None:
    """返回内置人设列表。"""
    personas = skill.get_available_personas()
    assert set(personas) == {"assistant", "cute", "sarcastic", "geek"}


def test_load_personas_from_dir_missing() -> None:
    """目录不存在回退到内置。"""
    personas = load_personas_from_dir("/nonexistent/directory/personas")
    assert set(personas.keys()) == {"assistant", "cute", "sarcastic", "geek"}
    assert isinstance(personas["geek"], PersonaTemplate)
