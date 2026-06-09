"""Agent WebSocket adapter for ACP-QQ Bridge.

Handles bi-directional communication with the ACP agent over WebSocket,
including automatic reconnection with exponential backoff, heartbeat pings,
idempotent message deduplication, and offline message buffering.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable, Literal

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from acp_qq_bridge.config import BridgeConfig
from acp_qq_bridge.core.protocol import (
    ACPProtocolError,
    DownstreamMessage,
    UpstreamMessage,
    parse_message,
    serialize_message,
)
from acp_qq_bridge.core.runtime import SessionManager
from acp_qq_bridge.utils.logger import get_logger

logger = get_logger(__name__)

ConnectionState = Literal["disconnected", "connecting", "connected", "reconnecting"]

# websockets >= 13 introduced ClientConnection and changed header kwarg name.
_WS_NEW_API: bool = hasattr(websockets, "ClientConnection")


def _ws_is_open(ws: Any) -> bool:
    """Check whether a WebSocket connection is open across websockets versions.

    Args:
        ws: A websockets client connection instance.

    Returns:
        ``True`` if the connection is currently open.
    """
    if hasattr(ws, "open"):
        return bool(ws.open)
    # websockets >= 13 uses a State enum
    return bool(ws.state == websockets.State.OPEN)


class AgentWebSocketAdapter:
    """WebSocket client adapter for the ACP agent.

    Manages the lifecycle of a WebSocket connection to the agent,
    including reconnection, heartbeat, message deduplication, and
    send buffering while disconnected.
    """

    def __init__(self, config: BridgeConfig, session_manager: SessionManager) -> None:
        """Initialize the adapter.

        Args:
            config: Bridge configuration containing agent WebSocket settings.
            session_manager: Session manager for runtime state tracking.
        """
        self._config = config
        self._session_manager = session_manager
        self._ws: Any | None = None
        self._state: ConnectionState = "disconnected"
        self._receive_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._downstream_handler: Callable[[DownstreamMessage], Awaitable[None]] | None = None
        self._disconnect_handler: Callable[[], Awaitable[None]] | None = None

        # Idempotency: trace_id -> timestamp (seconds)
        self._seen_trace_ids: dict[str, float] = {}
        self._dedup_lock = asyncio.Lock()
        self._dedup_ttl: float = 300.0  # 5 minutes

        # Offline send buffer
        self._send_buffer: deque[UpstreamMessage] = deque(maxlen=100)
        self._buffer_lock = asyncio.Lock()

        # Reconnection bookkeeping
        self._reconnect_attempts: int = 0
        self._reconnect_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        """Return whether the WebSocket is currently connected."""
        return self._ws is not None and _ws_is_open(self._ws)

    @property
    def connection_state(self) -> ConnectionState:
        """Return the current high-level connection state."""
        return self._state

    # ------------------------------------------------------------------ #
    # Handler registration
    # ------------------------------------------------------------------ #

    def register_downstream_handler(
        self,
        handler: Callable[[DownstreamMessage], Awaitable[None]],
    ) -> None:
        """Register a callback for incoming downstream messages.

        Args:
            handler: Async callable invoked for each :class:`DownstreamMessage`.
        """
        self._downstream_handler = handler

    def register_disconnect_handler(
        self,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        """Register a callback invoked when the connection drops.

        Args:
            handler: Async callable invoked on disconnect.
        """
        self._disconnect_handler = handler

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    async def connect(self) -> None:
        """Connect to the agent WebSocket and start background loops.

        This method blocks until the first successful connection is
        established.  If the connection drops, automatic reconnection
        with exponential backoff is performed in the background.
        """
        if self._state in ("connecting", "connected"):
            logger.warning("connect() called while already %s", self._state)
            return

        self._state = "connecting"
        url = self._config.agent.ws_url
        headers = {"Authorization": f"Bearer {self._config.agent.token}"}
        connect_kwargs: dict[str, Any] = (
            {"additional_headers": headers} if _WS_NEW_API else {"extra_headers": headers}
        )

        while True:
            try:
                logger.info("Connecting to agent at %s", url)
                self._ws = await websockets.connect(url, **connect_kwargs)
                self._state = "connected"
                self._reconnect_attempts = 0
                logger.info("Agent WebSocket connected")

                # Drain buffered messages
                await self._flush_send_buffer()

                # Start background tasks
                self._receive_task = asyncio.create_task(
                    self._receive_loop(),
                    name="agent-ws-receive",
                )
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name="agent-ws-heartbeat",
                )
                return
            except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
                logger.warning("Connection failed: %s", exc)
                self._state = "reconnecting"
                if self._disconnect_handler is not None:
                    try:
                        await self._disconnect_handler()
                    except Exception:
                        logger.exception("Disconnect handler failed")

                self._reconnect_attempts += 1
                delay = self._backoff_delay()
                logger.info("Reconnecting in %.1f seconds... (attempt %d)", delay, self._reconnect_attempts)
                await asyncio.sleep(delay)

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket and cancel background tasks."""
        logger.info("Disconnecting from agent WebSocket")
        self._state = "disconnected"

        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None

        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None

        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        self._reconnect_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.exception("Error closing WebSocket")
            self._ws = None

        logger.info("Agent WebSocket disconnected")

    # ------------------------------------------------------------------ #
    # Message sending
    # ------------------------------------------------------------------ #

    async def send_message(self, msg: UpstreamMessage) -> None:
        """Send an upstream message to the agent.

        If the connection is not available, the message is buffered for
        later delivery (up to a maximum of 100 messages).

        Args:
            msg: The upstream message to send.
        """
        if not self.is_connected or self._ws is None:
            async with self._buffer_lock:
                self._send_buffer.append(msg)
            logger.debug("Message buffered (offline): trace_id=%s", msg.trace_id)
            return

        try:
            payload = serialize_message(msg)
            await self._ws.send(payload)
            logger.debug("Message sent: trace_id=%s", msg.trace_id)
        except Exception:
            logger.exception("Send failed, buffering message")
            async with self._buffer_lock:
                self._send_buffer.append(msg)

    async def send_interrupt(self, session_id: str) -> None:
        """Send an interrupt action for the given session.

        Args:
            session_id: The session to interrupt.
        """
        from acp_qq_bridge.core.protocol import UpstreamPayload

        msg = UpstreamMessage(
            session_id=session_id,
            action="interrupt",
            payload=UpstreamPayload(text="", raw_signal="SIGINT"),
        )
        await self.send_message(msg)

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #

    async def _receive_loop(self) -> None:
        """Continuously receive messages and dispatch to the handler."""
        if self._ws is None:
            return

        try:
            async for raw in self._ws:
                try:
                    msg = parse_message(raw)
                except ACPProtocolError as exc:
                    logger.warning("Failed to parse message: %s", exc)
                    continue

                if not isinstance(msg, DownstreamMessage):
                    logger.debug("Ignoring non-downstream message type")
                    continue

                # Idempotency check
                if await self._is_duplicate(msg.trace_id):
                    logger.debug("Duplicate message dropped: trace_id=%s", msg.trace_id)
                    continue

                if self._downstream_handler is not None:
                    try:
                        await self._downstream_handler(msg)
                    except Exception:
                        logger.exception("Downstream handler error")
        except ConnectionClosed as exc:
            logger.warning(
                "WebSocket closed: code=%s reason=%s",
                exc.code,
                exc.reason,
            )
        except asyncio.CancelledError:
            logger.debug("Receive loop cancelled")
            raise
        except Exception:
            logger.exception("Unexpected error in receive loop")

        # Connection lost — trigger reconnect if not intentionally disconnecting
        if self._state != "disconnected":
            async with self._reconnect_lock:
                if self._state != "connecting" and (
                    self._reconnect_task is None or self._reconnect_task.done()
                ):
                    self._state = "reconnecting"
                    self._reconnect_task = asyncio.create_task(
                        self._reconnect(),
                        name="agent-ws-reconnect",
                    )

    async def _heartbeat_loop(self) -> None:
        """Send periodic WebSocket ping frames."""
        interval = self._config.agent.heartbeat_interval
        try:
            while True:
                await asyncio.sleep(interval)
                if self._ws is not None and _ws_is_open(self._ws):
                    try:
                        await self._ws.ping()
                        logger.debug("Heartbeat ping sent")
                    except Exception:
                        logger.warning("Heartbeat ping failed")
                        break
        except asyncio.CancelledError:
            logger.debug("Heartbeat loop cancelled")
            raise

    # ------------------------------------------------------------------ #
    # Reconnection & backoff
    # ------------------------------------------------------------------ #

    async def _reconnect(self) -> None:
        """Reconnect after a connection loss."""
        if self._state == "disconnected":
            return

        if self._disconnect_handler is not None:
            try:
                await self._disconnect_handler()
            except Exception:
                logger.exception("Disconnect handler failed during reconnect")

        url = self._config.agent.ws_url
        headers = {"Authorization": f"Bearer {self._config.agent.token}"}
        connect_kwargs: dict[str, Any] = (
            {"additional_headers": headers} if _WS_NEW_API else {"extra_headers": headers}
        )

        while True:
            self._reconnect_attempts += 1
            delay = self._backoff_delay()
            logger.info(
                "Reconnecting in %.1f seconds... (attempt %d)",
                delay,
                self._reconnect_attempts,
            )
            await asyncio.sleep(delay)

            try:
                self._state = "connecting"
                self._ws = await websockets.connect(url, **connect_kwargs)
                self._state = "connected"
                self._reconnect_attempts = 0
                logger.info("Reconnected to agent WebSocket")

                await self._flush_send_buffer()

                self._receive_task = asyncio.create_task(
                    self._receive_loop(),
                    name="agent-ws-receive",
                )
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name="agent-ws-heartbeat",
                )
                return
            except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
                logger.warning("Reconnection attempt failed: %s", exc)
                self._state = "reconnecting"

    def _backoff_delay(self) -> float:
        """Calculate the next exponential backoff delay in seconds.

        The delay doubles on each consecutive failure, capped at
        ``config.agent.reconnect_max_interval``.

        Returns:
            Delay in seconds (minimum 1.0).
        """
        max_interval: int = self._config.agent.reconnect_max_interval
        delay: float = min(
            2 ** max(0, self._reconnect_attempts - 1),
            max_interval,
        )
        return max(1.0, delay)

    async def _flush_send_buffer(self) -> None:
        """Send all buffered upstream messages."""
        async with self._buffer_lock:
            if not self._send_buffer:
                return
            to_flush = list(self._send_buffer)
            self._send_buffer.clear()

        for idx, msg in enumerate(to_flush):
            if self._ws is None or not _ws_is_open(self._ws):
                # Put remaining messages back if connection dropped mid-flush
                remaining = to_flush[idx:]
                async with self._buffer_lock:
                    # Respect maxlen by trimming from the front if needed
                    for m in remaining:
                        maxlen = self._send_buffer.maxlen
                        if maxlen is not None and len(self._send_buffer) >= maxlen:
                            self._send_buffer.popleft()
                        self._send_buffer.append(m)
                break
            try:
                await self._ws.send(serialize_message(msg))
                logger.debug("Flushed buffered message: trace_id=%s", msg.trace_id)
            except Exception:
                logger.exception("Flush failed for message")
                async with self._buffer_lock:
                    maxlen = self._send_buffer.maxlen
                    if maxlen is not None and len(self._send_buffer) >= maxlen:
                        self._send_buffer.popleft()
                    self._send_buffer.append(msg)

    # ------------------------------------------------------------------ #
    # Idempotency helpers
    # ------------------------------------------------------------------ #

    async def _is_duplicate(self, trace_id: str) -> bool:
        """Check whether *trace_id* was seen within the deduplication TTL.

        Args:
            trace_id: The trace identifier to check.

        Returns:
            ``True`` if the trace_id is a duplicate.
        """
        now = time.time()
        async with self._dedup_lock:
            # Periodic cleanup of expired entries when cache grows large
            if len(self._seen_trace_ids) > 10_000:
                cutoff = now - self._dedup_ttl
                self._seen_trace_ids = {
                    tid: ts
                    for tid, ts in self._seen_trace_ids.items()
                    if ts > cutoff
                }

            if trace_id in self._seen_trace_ids:
                return True

            self._seen_trace_ids[trace_id] = now
            return False
