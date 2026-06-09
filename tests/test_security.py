from __future__ import annotations

import pytest

from acp_qq_bridge.core.security import (
    ASTAuditor,
    SecurityEngine,
    SensitiveWordFilter,
    WhitelistEngine,
    validate_session_access,
)


def test_sensitive_word_filter_hit() -> None:
    """检测 rm -rf /home 包含敏感词。"""
    f = SensitiveWordFilter()
    hit, hits = f.check("rm -rf /home")
    assert hit is True
    assert any("rm -rf" in h for h in hits)


def test_sensitive_word_filter_safe() -> None:
    """正常文本不触发。"""
    f = SensitiveWordFilter()
    hit, hits = f.check("Hello, how are you today?")
    assert hit is False
    assert hits == []


def test_ast_auditor_dangerous() -> None:
    """import os; os.system('ls') 应检测为危险。"""
    auditor = ASTAuditor()
    safe, violations = auditor.audit("import os; os.system('ls')")
    assert safe is False
    assert any("os.system" in v for v in violations)


def test_ast_auditor_safe() -> None:
    """print('hello') 应安全。"""
    auditor = ASTAuditor()
    safe, violations = auditor.audit("print('hello')")
    assert safe is True
    assert violations == []


def test_ast_auditor_eval_exec() -> None:
    """eval('1+1') 应检测为危险。"""
    auditor = ASTAuditor()
    safe, violations = auditor.audit("eval('1+1')")
    assert safe is False
    assert any("eval" in v for v in violations)


def test_whitelist_engine_allowed() -> None:
    """白名单命令通过。"""
    engine = WhitelistEngine(["python", "ls", "cat"])
    assert engine.check("python script.py") is True
    assert engine.check("ls -la") is True


def test_whitelist_engine_denied() -> None:
    """非白名单命令拒绝。"""
    engine = WhitelistEngine(["python", "ls"])
    assert engine.check("rm -rf /") is False
    assert engine.check("") is False


def test_security_engine_validate_command_safe() -> None:
    """综合：安全命令通过。"""
    engine = SecurityEngine(
        allowed_commands=["python", "ls"],
        sensitive_patterns=["badword"],
    )
    result = engine.validate_command("python script.py")
    assert result.passed is True
    assert result.details.get("stage") == "all_passed"


def test_security_engine_validate_command_dangerous() -> None:
    """综合：危险命令拦截。"""
    engine = SecurityEngine(
        allowed_commands=["python", "ls"],
        sensitive_patterns=[],
    )
    result = engine.validate_command("rm -rf /")
    assert result.passed is False
    assert "Sensitive" in result.reason


def test_security_engine_audit_code() -> None:
    """代码审计功能。"""
    engine = SecurityEngine(
        allowed_commands=["python"],
        sensitive_patterns=[],
    )
    result = engine.audit_code("import os; os.system('ls')")
    assert result.passed is False
    assert result.details.get("stage") == "ast_audit"
    assert any("os.system" in v for v in result.details.get("violations", []))


def test_validate_session_access() -> None:
    """正确绑定返回 True，错误绑定返回 False。"""
    bindings = {"session-1": "qq-123", "session-2": "qq-456"}
    assert validate_session_access("session-1", "qq-123", bindings) is True
    assert validate_session_access("session-1", "qq-999", bindings) is False
    assert validate_session_access("session-x", "qq-123", bindings) is False


def test_audit_log() -> None:
    """验证审计日志记录。"""
    engine = SecurityEngine(
        allowed_commands=["python"],
        sensitive_patterns=[],
    )
    result = engine.validate_command("python ok.py")
    engine.log_event(qq_id="qq-123", raw_command="python ok.py", result=result)

    log = engine.get_audit_log()
    assert len(log) == 1
    assert log[0]["qq_id"] == "qq-123"
    assert log[0]["passed"] is True
    assert log[0]["raw_command"] == "python ok.py"
