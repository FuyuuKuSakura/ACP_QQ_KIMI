"""ACP-QQ Bridge main entry point.

Parses CLI arguments, loads configuration, initialises all subsystems,
and starts the NoneBot2 + OneBot v11 event loop.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Any

import nonebot
from nonebot import get_driver

from acp_qq_bridge.adapters.agent_ws import AgentWebSocketAdapter
from acp_qq_bridge.adapters.qq_bot import init_qq_bot
from acp_qq_bridge.config import BridgeConfig, load_config
from acp_qq_bridge.core.runtime import SessionManager
from acp_qq_bridge.core.security import SecurityEngine
from acp_qq_bridge.middleware.persona import PersonaSkill, load_personas_from_dir
from acp_qq_bridge.utils.logger import configure_logging, get_logger

logger = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional argument list override for testing.

    Returns:
        Parsed namespace.
    """
    parser = argparse.ArgumentParser(
        prog="acp-qq-bridge",
        description="ACP-based QQ coding agent bridge",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to YAML configuration file",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Run the ACP-QQ Bridge.

    Execution flow:

    1. Parse CLI arguments.
    2. Load and validate configuration.
    3. Configure structured logging.
    4. Initialise security, persona, session manager, and WebSocket adapter.
    5. Connect to the ACP agent (via NoneBot startup hook).
    6. Start background cleanup (via NoneBot startup hook).
    7. Initialise QQ bot and run NoneBot2.
    8. Graceful shutdown on SIGINT/SIGTERM.
    """
    args = _parse_args()

    # 1. Load config
    cfg: BridgeConfig = load_config(args.config)

    # 2. Configure logging
    configure_logging()
    logger.info("ACP-QQ Bridge starting up", config_path=args.config)

    # 3. Initialise components
    session_manager = SessionManager(
        ttl=cfg.qq.session_ttl,
        max_context=20,
    )

    security = SecurityEngine(
        allowed_commands=cfg.security.allowed_commands,
        sensitive_patterns=cfg.security.sensitive_patterns,
        enable_ast=cfg.security.enable_ast_audit,
    )

    personas = load_personas_from_dir(cfg.persona.personas_dir)
    persona = PersonaSkill(
        personas=personas,
        default=cfg.persona.default_persona,
    )

    agent_ws = AgentWebSocketAdapter(
        config=cfg,
        session_manager=session_manager,
    )

    # 4. Set up NoneBot lifecycle hooks for async startup / shutdown
    nonebot.init()
    driver = get_driver()

    @driver.on_startup
    async def _on_startup() -> None:
        """Establish agent connection and start background tasks."""
        await agent_ws.connect()
        session_manager.start_cleanup_task(interval=60)
        logger.info("Bridge startup complete")

    @driver.on_shutdown
    async def _on_shutdown() -> None:
        """Gracefully tear down all subsystems."""
        logger.info("Bridge shutting down")

        try:
            await agent_ws.disconnect()
        except Exception:
            logger.exception("Error during agent disconnect")

        try:
            session_manager.stop_cleanup_task()
        except Exception:
            logger.exception("Error stopping cleanup task")

        try:
            for meta in await session_manager.list_sessions():
                try:
                    await session_manager.close_session(meta.session_id)
                except Exception:
                    logger.exception(
                        "Error closing session %s",
                        meta.session_id,
                    )
        except Exception:
            logger.exception("Error listing sessions during shutdown")

        logger.info("Bridge shutdown complete")

    # 5. Initialise QQ bot matchers and downstream handler
    init_qq_bot(
        config=cfg,
        session_manager=session_manager,
        security=security,
        persona=persona,
        agent_ws=agent_ws,
    )

    # 6. Register OS signal handlers for graceful shutdown
    def _handle_signal(signum: int, _frame: Any) -> None:
        """Raise SystemExit so that NoneBot/uvicorn runs lifespan shutdown hooks."""
        logger.info("Received signal %d, initiating graceful shutdown", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # 7. Start NoneBot2 (blocks until termination)
    nonebot.run()


if __name__ == "__main__":
    main()
