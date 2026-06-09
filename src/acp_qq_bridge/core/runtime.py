"""ACP-QQ Bridge session runtime state manager.

This module provides the :class:`SessionManager` which is the central authority
for tracking ACP sessions, their QQ bindings, status, context history and TTL
lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


class SessionNotFoundError(Exception):
    """Raised when an operation targets a non-existent session."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session not found: {session_id}")
        self.session_id = session_id


class SessionAlreadyBoundError(Exception):
    """Raised when a QQ ID is already bound to another session."""

    def __init__(self, qq_id: str, existing_session_id: str) -> None:
        super().__init__(
            f"QQ ID '{qq_id}' is already bound to session '{existing_session_id}'"
        )
        self.qq_id = qq_id
        self.existing_session_id = existing_session_id


@dataclass
class SessionMeta:
    """Metadata representing an active ACP session.

    Attributes:
        session_id: Unique ACP session identifier (12-char hex).
        agent_source: Origin of the agent request (``"vscode-extension"`` or ``"cli"``).
        status: Current lifecycle status.
        created_at: Unix timestamp when the session was created.
        last_active: Unix timestamp of the most recent activity.
        qq_id: QQ user ID or group ID, if bound.
        qq_type: ``"private"`` or ``"group"``.
        context: Recent message context, up to ``max_context`` entries.
    """

    session_id: str
    agent_source: Literal["vscode-extension", "cli"]
    status: Literal["thinking", "executing", "idle", "interrupted"] = "idle"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    qq_id: str | None = None
    qq_type: Literal["private", "group"] | None = None
    context: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """Thread-safe (asyncio-safe) session registry with TTL management.

    :class:`SessionManager` maintains two indexes:

    - ``session_map``: ``session_id`` → :class:`SessionMeta`
    - ``qq_binding``: ``qq_id`` → ``session_id``

    All mutating methods are protected by an :class:`asyncio.Lock` so they are
    safe to call from multiple concurrent tasks.

    Args:
        ttl: Time-to-live in seconds. Sessions that have been inactive longer
            than this duration are eligible for cleanup.
        max_context: Maximum number of context messages retained per session.
    """

    def __init__(self, ttl: int = 3600, max_context: int = 20) -> None:
        self.ttl = ttl
        self.max_context = max_context
        self._session_map: dict[str, SessionMeta] = {}
        self._qq_binding: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ #
    # CRUD operations
    # ------------------------------------------------------------------ #

    async def create_session(
        self,
        agent_source: Literal["vscode-extension", "cli"],
    ) -> SessionMeta:
        """Create a new idle session.

        Args:
            agent_source: The source channel for the new session.

        Returns:
            The freshly created :class:`SessionMeta`.
        """
        session_id = uuid.uuid4().hex[:12]
        meta = SessionMeta(
            session_id=session_id,
            agent_source=agent_source,
        )
        async with self._lock:
            self._session_map[session_id] = meta
        logger.info("Created session %s (source=%s)", session_id, agent_source)
        return meta

    async def bind_qq(
        self,
        session_id: str,
        qq_id: str,
        qq_type: Literal["private", "group"],
    ) -> SessionMeta:
        """Bind a QQ context to an existing session.

        Args:
            session_id: The ACP session to bind.
            qq_id: QQ user ID or group ID.
            qq_type: ``"private"`` or ``"group"``.

        Returns:
            The updated :class:`SessionMeta`.

        Raises:
            SessionNotFoundError: If the session does not exist.
            SessionAlreadyBoundError: If *qq_id* is already bound to a different
                session.
        """
        async with self._lock:
            meta = self._session_map.get(session_id)
            if meta is None:
                raise SessionNotFoundError(session_id)

            existing = self._qq_binding.get(qq_id)
            if existing is not None and existing != session_id:
                raise SessionAlreadyBoundError(qq_id, existing)

            # Unbind previous QQ ID from this session if any
            if meta.qq_id is not None and meta.qq_id != qq_id:
                self._qq_binding.pop(meta.qq_id, None)

            meta.qq_id = qq_id
            meta.qq_type = qq_type
            meta.last_active = time.time()
            self._qq_binding[qq_id] = session_id

        logger.info(
            "Bound QQ %s (%s) to session %s",
            qq_id,
            qq_type,
            session_id,
        )
        return meta

    async def unbind_qq(self, qq_id: str) -> None:
        """Remove the QQ binding for *qq_id*.

        If *qq_id* is not currently bound this is a no-op.

        Args:
            qq_id: The QQ identifier to unbind.
        """
        async with self._lock:
            session_id = self._qq_binding.pop(qq_id, None)
            if session_id is not None:
                meta = self._session_map.get(session_id)
                if meta is not None:
                    meta.qq_id = None
                    meta.qq_type = None
        logger.debug("Unbound QQ %s", qq_id)

    async def get_by_qq(self, qq_id: str) -> SessionMeta | None:
        """Look up a session by its bound QQ ID.

        Args:
            qq_id: The QQ identifier to look up.

        Returns:
            The associated :class:`SessionMeta` or ``None``.
        """
        async with self._lock:
            session_id = self._qq_binding.get(qq_id)
            if session_id is None:
                return None
            return self._session_map.get(session_id)

    async def get_by_session(self, session_id: str) -> SessionMeta | None:
        """Look up a session by its ACP session ID.

        Args:
            session_id: The ACP session identifier.

        Returns:
            The :class:`SessionMeta` or ``None``.
        """
        async with self._lock:
            return self._session_map.get(session_id)

    async def update_status(
        self,
        session_id: str,
        status: Literal["thinking", "executing", "idle", "interrupted"],
    ) -> SessionMeta:
        """Update the lifecycle status of a session.

        Args:
            session_id: The target session.
            status: The new status value.

        Returns:
            The updated :class:`SessionMeta`.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        async with self._lock:
            meta = self._session_map.get(session_id)
            if meta is None:
                raise SessionNotFoundError(session_id)
            meta.status = status
            meta.last_active = time.time()
        logger.debug("Session %s status -> %s", session_id, status)
        return meta

    async def update_last_active(self, session_id: str) -> None:
        """Refresh the ``last_active`` timestamp of a session.

        Args:
            session_id: The target session.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        async with self._lock:
            meta = self._session_map.get(session_id)
            if meta is None:
                raise SessionNotFoundError(session_id)
            meta.last_active = time.time()

    async def append_context(self, session_id: str, message: dict[str, Any]) -> None:
        """Append a message to the session context, trimming if necessary.

        Args:
            session_id: The target session.
            message: A JSON-serialisable message dictionary.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        async with self._lock:
            meta = self._session_map.get(session_id)
            if meta is None:
                raise SessionNotFoundError(session_id)
            meta.context.append(message)
            if len(meta.context) > self.max_context:
                meta.context = meta.context[-self.max_context :]
            meta.last_active = time.time()

    async def get_context(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve the current message context for a session.

        Args:
            session_id: The target session.

        Returns:
            A shallow copy of the context list.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        async with self._lock:
            meta = self._session_map.get(session_id)
            if meta is None:
                raise SessionNotFoundError(session_id)
            return list(meta.context)

    async def interrupt_session(self, session_id: str) -> SessionMeta:
        """Mark a session as interrupted.

        Args:
            session_id: The target session.

        Returns:
            The updated :class:`SessionMeta`.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        return await self.update_status(session_id, "interrupted")

    async def close_session(self, session_id: str) -> None:
        """Close a session and remove it from the registry.

        Any associated QQ binding is also removed.

        Args:
            session_id: The session to close.

        Raises:
            SessionNotFoundError: If the session does not exist.
        """
        async with self._lock:
            meta = self._session_map.pop(session_id, None)
            if meta is None:
                raise SessionNotFoundError(session_id)
            if meta.qq_id is not None:
                self._qq_binding.pop(meta.qq_id, None)
        logger.info("Closed session %s", session_id)

    async def list_sessions(self) -> list[SessionMeta]:
        """Return a snapshot of all active sessions.

        Returns:
            A list of :class:`SessionMeta` objects (shallow copy).
        """
        async with self._lock:
            return list(self._session_map.values())

    async def cleanup_expired(self) -> list[str]:
        """Remove sessions whose ``last_active`` exceeds the TTL.

        Returns:
            A list of the *session_id* values that were removed.
        """
        now = time.time()
        expired: list[str] = []
        async with self._lock:
            for session_id, meta in list(self._session_map.items()):
                if now - meta.last_active > self.ttl:
                    expired.append(session_id)
                    self._session_map.pop(session_id, None)
                    if meta.qq_id is not None:
                        self._qq_binding.pop(meta.qq_id, None)
        if expired:
            logger.info("Cleaned up %d expired session(s): %s", len(expired), expired)
        return expired

    # ------------------------------------------------------------------ #
    # Background TTL cleanup
    # ------------------------------------------------------------------ #

    def start_cleanup_task(self, interval: int = 60) -> None:
        """Start a background :class:`asyncio.Task` that runs :meth:`cleanup_expired`.

        The task loops forever (or until :meth:`stop_cleanup_task` is called)
        sleeping *interval* seconds between invocations.

        Args:
            interval: Seconds between cleanup sweeps.

        Raises:
            RuntimeError: If the cleanup task is already running.
        """
        if self._cleanup_task is not None and not self._cleanup_task.done():
            raise RuntimeError("Cleanup task is already running")

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval)
                    await self.cleanup_expired()
                except asyncio.CancelledError:
                    logger.debug("Session cleanup task cancelled")
                    raise
                except Exception:
                    logger.exception("Unexpected error in session cleanup task")

        self._cleanup_task = asyncio.create_task(_loop())
        logger.info(
            "Started session cleanup task (interval=%ds, ttl=%ds)",
            interval,
            self.ttl,
        )

    def stop_cleanup_task(self) -> None:
        """Cancel the running background cleanup task, if any."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("Stopped session cleanup task")
        self._cleanup_task = None
