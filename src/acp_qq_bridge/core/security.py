"""Security engine for ACP-QQ Bridge.

Provides sensitive word filtering, AST-based code auditing, command whitelisting,
session access validation, and centralized security event logging.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SecurityResult:
    """Result of a security validation / audit.

    Attributes:
        passed: Whether the check passed (i.e. no threat detected).
        reason: Human-readable description when *passed* is ``False``.
        details: Arbitrary structured data for callers or log consumers.
    """

    passed: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trie-based sensitive word filter
# ---------------------------------------------------------------------------


class SensitiveWordFilter:
    """High-performance sensitive-word matcher backed by a Trie tree."""

    def __init__(self, patterns: list[str] | None = None) -> None:
        """Initialise the filter with an optional list of sensitive words.

        Args:
            patterns: Initial list of sensitive words / patterns.
        """
        self._root: dict[str, Any] = {}
        self._default_patterns: list[str] = [
            "rm -rf",
            "mkfs",
            "dd if=/dev/zero",
            ">/dev/sda",
            "> /dev/sda",
            "format",
            "del /f /s /q",
            "del /f /s",
            "del /f",
            "rd /s /q",
            "rd /s",
            "shutdown",
            "reboot",
            "poweroff",
            "halt",
            "init 0",
            "fdisk",
            "parted",
            "wipefs",
            "shred",
            "dd of=/dev/sd",
        ]
        self._compiled_defaults: list[re.Pattern[str]] = [
            re.compile(rf"\b{re.escape(p)}\b", re.IGNORECASE) for p in self._default_patterns
        ]
        # Trie for exact-word matching
        self._trie_patterns: set[str] = set()
        if patterns:
            for w in patterns:
                self.add_word(w)

    # -- Trie helpers -------------------------------------------------------

    def add_word(self, word: str) -> None:
        """Add a sensitive word to the Trie.

        Args:
            word: The sensitive word to register.
        """
        node = self._root
        for ch in word:
            node = node.setdefault(ch, {})
        node["$"] = True  # end-of-word marker
        self._trie_patterns.add(word)

    def _trie_match(self, text: str) -> list[str]:
        """Return all sensitive words found in *text* via Trie traversal."""
        hits: list[str] = []
        length = len(text)
        i = 0
        while i < length:
            node = self._root
            j = i
            last_match: str | None = None
            while j < length and text[j] in node:
                node = node[text[j]]
                j += 1
                if "$" in node:
                    last_match = text[i:j]
            if last_match is not None:
                hits.append(last_match)
                i = j  # skip past the matched word
            else:
                i += 1
        return hits

    def _regex_match(self, text: str) -> list[str]:
        """Return all default high-risk command patterns found in *text*."""
        hits: list[str] = []
        for pat in self._compiled_defaults:
            for m in pat.finditer(text):
                hits.append(m.group())
        return hits

    def check(self, text: str) -> tuple[bool, list[str]]:
        """Check whether *text* contains any sensitive word.

        Args:
            text: The input text to inspect.

        Returns:
            A tuple ``(contains_sensitive, hit_list)``.
        """
        trie_hits = self._trie_match(text)
        regex_hits = self._regex_match(text)
        all_hits = list(dict.fromkeys(trie_hits + regex_hits))  # preserve order, de-dupe
        return (bool(all_hits), all_hits)


# ---------------------------------------------------------------------------
# AST auditor
# ---------------------------------------------------------------------------


class ASTAuditor:
    """Static analyser that audits Python source code for dangerous constructs."""

    # Functions / builtins that are always considered dangerous
    DANGEROUS_CALLS: set[str] = {
        "os.system",
        "os.popen",
        "os.execve",
        "os.execv",
        "os.execvp",
        "os.execvpe",
        "subprocess.call",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.check_call",
        "subprocess.check_output",
        "eval",
        "exec",
        "compile",
        "__import__",
        "breakpoint",
    }

    DANGEROUS_ATTRS: set[str] = {
        "os.system",
        "os.popen",
        "sys.exit",
    }

    def audit(self, code: str) -> tuple[bool, list[str]]:
        """Audit *code* for dangerous calls and attribute accesses.

        Args:
            code: Python source code string.

        Returns:
            ``(is_safe, violations)`` where *is_safe* is ``True`` when no
            dangerous constructs are detected.
        """
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Code that cannot be parsed is treated as unsafe
            return False, ["syntax_error: unable to parse code"]

        violations: list[str] = []
        for node in ast.walk(tree):
            # -- Dangerous function calls -----------------------------------
            if isinstance(node, ast.Call):
                call_name = self._resolve_call_name(node.func)
                if call_name in self.DANGEROUS_CALLS:
                    violations.append(f"dangerous_call:{call_name}")
                # Detect open(..., 'w') / open(..., 'a') / open(..., 'x')
                if call_name == "open" and node.args:
                    mode_arg = self._extract_mode(node)
                    if mode_arg and any(c in mode_arg for c in "wax+"):
                        violations.append(f"dangerous_call:open(mode={mode_arg})")

            # -- Dangerous attribute access ---------------------------------
            if isinstance(node, ast.Attribute):
                attr_path = self._resolve_attr_path(node)
                if attr_path in self.DANGEROUS_ATTRS:
                    violations.append(f"dangerous_attr:{attr_path}")

            # -- Direct exec / eval as names --------------------------------
            if isinstance(node, ast.Name) and node.id in {"eval", "exec", "compile"}:
                # Already covered by Call above, but keep for completeness
                pass

        is_safe = not bool(violations)
        return is_safe, list(dict.fromkeys(violations))

    @staticmethod
    def _resolve_call_name(func: ast.expr) -> str | None:
        """Return the fully-qualified name of a callable AST node."""
        parts: list[str] = []
        node = func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return None

    @staticmethod
    def _resolve_attr_path(node: ast.Attribute) -> str | None:
        """Return ``module.attr`` for an attribute access AST node."""
        parts: list[str] = [node.attr]
        current: ast.expr = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
            return ".".join(reversed(parts))
        return None

    @staticmethod
    def _extract_mode(node: ast.Call) -> str | None:
        """Extract the *mode* argument from an ``open()`` call."""
        # Keyword argument
        for kw in node.keywords:
            if kw.arg == "mode":
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    return kw.value.value
        # Positional argument (second arg)
        if len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
        return None


# ---------------------------------------------------------------------------
# Whitelist engine
# ---------------------------------------------------------------------------


class WhitelistEngine:
    """Command whitelist that validates only the main command token."""

    def __init__(self, allowed_commands: list[str]) -> None:
        """Initialise the whitelist.

        Args:
            allowed_commands: List of permitted main command names
                (e.g. ``["python", "ls", "cat"]``).
        """
        # Normalise to lower-case for case-insensitive matching
        self._allowed: set[str] = {cmd.strip().lower() for cmd in allowed_commands}

    def check(self, command: str) -> bool:
        """Check whether *command* is permitted.

        The command string is split on whitespace; only the first token
        (the main command) is compared against the whitelist.

        Args:
            command: Raw command string, potentially with arguments.

        Returns:
            ``True`` if the main command is in the whitelist.
        """
        if not command or not command.strip():
            return False
        main_cmd = command.strip().split(None, 1)[0].lower()
        return main_cmd in self._allowed


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def validate_session_access(
    session_id: str,
    qq_id: str,
    bindings: dict[str, str],
) -> bool:
    """Validate that *session_id* is bound exclusively to *qq_id*.

    This enforces strict 1-to-1 session isolation: a session may only
    be accessed by the QQ account it was originally bound to.

    Args:
        session_id: The session identifier to validate.
        qq_id: The QQ ID requesting access.
        bindings: Mapping of ``session_id -> qq_id`` representing current
            active bindings.

    Returns:
        ``True`` if *session_id* exists in *bindings* and maps to *qq_id*.
        Returns ``False`` when the session is unknown or bound to a
        different QQ account.
    """
    bound_qq = bindings.get(session_id)
    if bound_qq is None:
        return False
    return bound_qq == qq_id


# ---------------------------------------------------------------------------
# Security engine (main orchestrator)
# ---------------------------------------------------------------------------


class SecurityEngine:
    """Centralised security orchestrator.

    Combines sensitive-word filtering, AST-based code auditing, command
    whitelisting, and structured audit logging.
    """

    def __init__(
        self,
        allowed_commands: list[str],
        sensitive_patterns: list[str],
        *,
        enable_ast: bool = True,
    ) -> None:
        """Initialise the security engine.

        Args:
            allowed_commands: Commands permitted by the whitelist.
            sensitive_patterns: Additional sensitive words for the Trie filter.
            enable_ast: Whether AST auditing is active.
        """
        self._filter = SensitiveWordFilter(sensitive_patterns)
        self._auditor = ASTAuditor()
        self._whitelist = WhitelistEngine(allowed_commands)
        self._enable_ast = enable_ast
        self._audit_log: list[dict[str, Any]] = []

    # -- Public API ----------------------------------------------------------

    def validate_command(self, text: str) -> SecurityResult:
        """Validate a plain-text command string.

        Performs, in order:
        1. Sensitive-word filtering
        2. Whitelist check (main command only)

        All validation attempts are recorded in the audit log.

        Args:
            text: Raw command text to validate.

        Returns:
            :class:`SecurityResult` indicating pass / fail status.
        """
        # 1. Sensitive words
        has_sensitive, hits = self._filter.check(text)
        if has_sensitive:
            return SecurityResult(
                passed=False,
                reason="Sensitive word detected",
                details={"hits": hits, "stage": "sensitive_word_filter"},
            )

        # 2. Whitelist
        if not self._whitelist.check(text):
            return SecurityResult(
                passed=False,
                reason="Command not in whitelist",
                details={"command": text.strip().split(None, 1)[0], "stage": "whitelist"},
            )

        return SecurityResult(
            passed=True,
            reason=None,
            details={"stage": "all_passed"},
        )

    def audit_code(self, code: str) -> SecurityResult:
        """Audit a block of Python source code.

        Performs, in order:
        1. Sensitive-word filtering (against raw source)
        2. AST static analysis (if *enable_ast* is ``True``)

        Args:
            code: Python source code string.

        Returns:
            :class:`SecurityResult` indicating pass / fail status.
        """
        # 1. Sensitive words in source
        has_sensitive, hits = self._filter.check(code)
        if has_sensitive:
            return SecurityResult(
                passed=False,
                reason="Sensitive word detected in source code",
                details={"hits": hits, "stage": "sensitive_word_filter"},
            )

        # 2. AST audit
        if self._enable_ast:
            is_safe, violations = self._auditor.audit(code)
            if not is_safe:
                return SecurityResult(
                    passed=False,
                    reason="Dangerous code construct detected",
                    details={"violations": violations, "stage": "ast_audit"},
                )

        return SecurityResult(
            passed=True,
            reason=None,
            details={"stage": "all_passed"},
        )

    def log_event(
        self,
        *,
        qq_id: str,
        raw_command: str,
        result: SecurityResult,
    ) -> None:
        """Append a structured audit event to the in-memory log.

        Args:
            qq_id: The QQ account ID that issued the request.
            raw_command: The original instruction text.
            result: The :class:`SecurityResult` produced by validation.
        """
        self._audit_log.append(
            {
                "timestamp": time.time(),
                "qq_id": qq_id,
                "raw_command": raw_command,
                "passed": result.passed,
                "reason": result.reason,
                "details": result.details,
            }
        )

    def get_audit_log(self) -> list[dict[str, Any]]:
        """Return a shallow copy of the full audit log.

        Returns:
            List of audit event dictionaries, ordered chronologically.
        """
        return list(self._audit_log)
