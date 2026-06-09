"""Persona (人设) transformation layer for ACP-QQ Bridge.

Converts raw assistant output text into a specific conversational persona
while preserving the underlying ACP instruction structure.
"""

from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from acp_qq_bridge.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PersonaTemplate:
    """Configuration for a single persona (人设模板).

    Attributes:
        id: Unique machine-readable identifier (e.g. ``"cute"``).
        name: Human-readable display name (e.g. ``"萌系"``).
        prefix: Text inserted before the main content.
        suffix: Text appended after the main content.
        tone_keywords: List of tone / catch-phrase keywords that may be
            randomly injected into the text.
        emoji_set: Pool of emoji / emoticons to randomly insert.
    """

    id: str
    name: str
    prefix: str = ""
    suffix: str = ""
    tone_keywords: list[str] = field(default_factory=list)
    emoji_set: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in personas
# ---------------------------------------------------------------------------


def _build_in_personas() -> dict[str, PersonaTemplate]:
    """Return the default set of built-in personas."""
    return {
        "assistant": PersonaTemplate(
            id="assistant",
            name="标准助手",
            prefix="",
            suffix="",
            tone_keywords=["请查收", "如有疑问随时告诉我"],
            emoji_set=["📎", "✅"],
        ),
        "sarcastic": PersonaTemplate(
            id="sarcastic",
            name="毒舌",
            prefix="",
            suffix="",
            tone_keywords=["啧啧", "这代码写的", "不愧是你", "能跑就行对吧"],
            emoji_set=["🙄", "😏", "🤡"],
        ),
        "cute": PersonaTemplate(
            id="cute",
            name="萌系",
            prefix="好哒~",
            suffix="",
            tone_keywords=["喵", "呀", "呢", "哟"],
            emoji_set=["(≧▽≦)", "(｡♥‿♥｡)", "(◕‿◕✿)", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧"],
        ),
        "geek": PersonaTemplate(
            id="geek",
            name="极客",
            prefix=">>",
            suffix="",
            tone_keywords=["编译中...", "执行完毕", "0 errors, 0 warnings"],
            emoji_set=["💻", "⚡", "🤖", "🚀"],
        ),
    }


# ---------------------------------------------------------------------------
# Persona loading helpers
# ---------------------------------------------------------------------------


def _load_yaml_personas(directory: str | Path) -> dict[str, PersonaTemplate]:
    """Load persona definitions from YAML files in *directory*."""
    personas: dict[str, PersonaTemplate] = {}
    dir_path = Path(directory)
    if not dir_path.exists():
        return personas
    for file in dir_path.glob("*.yaml"):
        try:
            with file.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict):
                personas[file.stem] = PersonaTemplate(
                    id=file.stem,
                    name=data.get("name", file.stem),
                    prefix=data.get("prefix", ""),
                    suffix=data.get("suffix", ""),
                    tone_keywords=data.get("tone_keywords", []),
                    emoji_set=data.get("emoji_set", []),
                )
        except Exception:
            logger.exception("Failed to load persona YAML: %s", file)
    return personas


def load_personas_from_dir(directory: str | Path) -> dict[str, PersonaTemplate]:
    """Load personas from *directory*, falling back to built-ins if absent.

    Args:
        directory: Path to a directory containing ``*.yaml`` persona files.

    Returns:
        Mapping of persona ID -> :class:`PersonaTemplate`.
    """
    personas = _load_yaml_personas(directory)
    if not personas:
        logger.info("No external personas found in %s, using built-ins", directory)
        personas = _build_in_personas()
    return personas


# ---------------------------------------------------------------------------
# PersonaSkill engine
# ---------------------------------------------------------------------------

# Regex that isolates ACP instruction tags so they are never mutated.
_ACP_TAG_RE = re.compile(r"<acp:[^>]+>.*?</acp:[^>]+>", re.S)


class PersonaSkill:
    """Skill engine that transforms agent text into a target persona."""

    def __init__(
        self,
        personas: dict[str, PersonaTemplate] | None = None,
        default: str = "assistant",
    ) -> None:
        """Initialize the skill engine.

        Args:
            personas: Map of persona ID -> template.  If ``None``, built-ins
                are used.
            default: Default persona ID applied when none is specified.
        """
        self._personas = personas if personas is not None else _build_in_personas()
        self._default = default
        self._active = default

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def transform(self, text: str, persona_id: str | None = None) -> str:
        """Transform *text* according to the requested persona.

        ACP instruction tags (``<acp:...>...</acp:...>``) are preserved
        verbatim.  Tone keywords and emojis are injected randomly outside
        those tags.  Very long text is truncated with an ellipsis.

        Args:
            text: Raw agent output.
            persona_id: Specific persona to use.  Falls back to the active
                persona when omitted.

        Returns:
            Persona-adjusted text.
        """
        pid = persona_id or self._active
        persona = self._personas.get(pid)
        if persona is None:
            return text

        # 1. Isolate ACP tags
        tags: list[str] = []

        def _stash_tag(match: re.Match[str]) -> str:
            tags.append(match.group(0))
            return f"\x00TAG{len(tags) - 1}\x00"

        protected = _ACP_TAG_RE.sub(_stash_tag, text)

        # 2. Apply prefix / suffix
        if persona.prefix:
            protected = f"{persona.prefix}\n{protected}"
        if persona.suffix:
            protected = f"{protected}\n{persona.suffix}"

        # 3. Randomly inject tone keywords (outside tags)
        if persona.tone_keywords and random.random() < 0.4:
            keyword = random.choice(persona.tone_keywords)
            lines = protected.split("\n")
            if lines:
                insert_idx = random.randint(0, len(lines))
                lines.insert(insert_idx, keyword)
                protected = "\n".join(lines)

        # 4. Randomly inject emoji (outside tags)
        if persona.emoji_set and random.random() < 0.4:
            emoji = random.choice(persona.emoji_set)
            lines = protected.split("\n")
            if lines:
                insert_idx = random.randint(0, len(lines))
                lines.insert(insert_idx, emoji)
                protected = "\n".join(lines)

        # 5. Truncate if excessively long (preserving tags on restore)
        max_len = 4096
        if len(protected) > max_len:
            protected = protected[: max_len - 3].rstrip() + "..."

        # 6. Restore ACP tags
        for idx, tag in enumerate(tags):
            protected = protected.replace(f"\x00TAG{idx}\x00", tag)

        return protected

    def get_available_personas(self) -> list[str]:
        """Return the list of available persona IDs."""
        return list(self._personas.keys())

    def list_personas(self) -> list[str]:
        """Alias for :meth:`get_available_personas` (used by QQ bot)."""
        return self.get_available_personas()

    def set_active_persona(self, persona_id: str) -> None:
        """Switch the default (active) persona."""
        if persona_id not in self._personas:
            raise ValueError(f"Unknown persona: {persona_id}")
        self._active = persona_id
        logger.info("Active persona switched to: %s", persona_id)
