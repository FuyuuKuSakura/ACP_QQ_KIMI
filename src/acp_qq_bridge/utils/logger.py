"""结构化日志配置模块.

本模块基于 structlog 提供统一的 JSON 格式日志输出，支持通过环境变量
``LOG_LEVEL`` 动态调整日志级别（默认为 ``INFO``）。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog


def configure_logging() -> None:
    """配置 structlog 与标准库 logging 的处理器、格式化和日志级别.

    调用一次即可在全局范围内生效。配置完成后，所有通过 :func:`get_logger`
    获取的 logger 均会输出 JSON 格式的结构化日志。

    日志级别优先级：
        1. 环境变量 ``LOG_LEVEL`` 的值（如 ``DEBUG``、``INFO``、``WARNING``、
           ``ERROR``、``CRITICAL``）。
        2. 若未设置或值无效，则默认使用 ``INFO``。
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    # 用于标准库（foreign）日志的预处理链
    shared_processors: list[structlog.types.Processor] = [
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
    ]

    # 配置 structlog：处理器链负责收集上下文、添加级别与时间戳，
    # 最终通过 wrap_for_formatter 把事件字典交给 ProcessorFormatter 渲染。
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.ExtraAdder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # 统一的 ProcessorFormatter，负责把 structlog 事件和标准库日志都渲染为 JSON
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)

    # 强制重置根 logger，避免重复 handler 导致的多行输出
    logging.basicConfig(
        handlers=[handler],
        level=log_level,
        format="%(message)s",
        force=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """获取已配置好的结构化 logger 实例.

    若这是首次获取 logger，会自动调用 :func:`configure_logging` 完成
    全局初始化，确保后续日志输出格式正确。

    Args:
        name: Logger 名称，通常使用当前模块的 ``__name__``。

    Returns:
        一个绑定了指定名称的 structlog BoundLogger 实例。
    """
    if not structlog.is_configured():
        configure_logging()

    return structlog.get_logger(name)
