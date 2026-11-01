"""Tests for the AnyList push feed (WebSocket listener, design §8 phase 2)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType

from custom_components.anylist.realtime import (
    AnyListPushListener,
    ConnectionState,
    _ReconnectBackoff,
    parse_push_message,
    pause_coordinator_polling,
    resume_coordinator_polling,
)


class _Fake401(Exception):
    """A handshake rejection carrying an HTTP 401 status."""

    status = 401


class _FakeMessage:
    """A minimal WebSocket message (text or close)."""

    def __init__(self, type: WSMsgType, data: str | None = None) -> None:
        self.type = type
        self.data = data


class _FakeWebSocket:
    """A scripted WebSocket: the test feeds it messages from a queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_calls = 0

    async def receive(self) -> _FakeMessage:
        """Wait for the next scripted message."""
        return await self.queue.get()

    async def send_str(self, data: str) -> None:
        """Record a sent frame."""
        self.sent.append(data)

    async def close(self) -> None:
        """Mark the socket closed."""
        self.closed = True
        self.close_calls += 1

    def push_text(self, text: str) -> None:
        """Deliver a text frame to the listener."""
        self.queue.put_nowait(_FakeMessage(WSMsgType.TEXT, text))

    def push_close(self, code: int | None = None) -> None:
        """Deliver a close frame (optionally with a close code)."""
        self.close_code = code
        self.queue.put_nowait(_FakeMessage(WSMsgType.CLOSE))


class _FakeSession:
    """A fake aiohttp session: each connect returns a fresh socket."""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.connects: list[str] = []
        self.sockets: list[_FakeWebSocket] = []
        self._failures = list(failures or [])

    async def ws_connect(self, url: str, **kwargs) -> _FakeWebSocket:
        """Record the URL and return a fresh socket (or raise a failure)."""
        self.connects.append(url)
        if self._failures:
            raise self._failures.pop(0)
        ws = _FakeWebSocket()
        self.sockets.append(ws)
        return ws


class _FakeClient:
    """A fake AnyList client with token accessors the listener uses."""

    def __init__(self) -> None:
        self.access_token = "token-1"
        self.client_identifier = "client-1"
        self.refresh_calls = 0
        self.refresh_error: Exception | None = None

    def refresh_tokens(self) -> None:
        """Rotate the token (or raise the configured error).

        The call counter counts attempts, not successes, so a failing
        refresh is still observable.
        """
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        self.access_token = f"token-{self.refresh_calls + 1}"


class _FakeHass:
    """A minimal Home Assistant stand-in (executor only)."""

    async def async_add_executor_job(self, func, *args):
        """Run the function directly (no real executor in unit tests)."""
        return func(*args)


class _FakeCoordinator:
    """A duck-typed coordinator for the pause/resume helpers."""

    def __init__(self) -> None:
        self.update_interval: timedelta | None = timedelta(seconds=3600)
        self.unsub_calls = 0

    def _async_unsub_refresh(self) -> None:
        """Record a cancelled pending refresh."""
        self.unsub_calls += 1


async def _wait_until(condition, timeout: float = 2.0) -> None:
    """Poll a condition until true (or fail after the timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    assert condition(), "condition not met in time"


def _make_listener(session, client=None, **kwargs):
    """Build a listener with recording callbacks and tiny test timings."""
    client = client or _FakeClient()
    calls: dict[str, list] = {"sync": [], "connected": [], "disconnected": []}
    listener = AnyListPushListener(
        _FakeHass(),
        client,
        on_sync=lambda: calls["sync"].append(True),
        on_connected=lambda: calls["connected"].append(True),
        on_disconnected=lambda: calls["disconnected"].append(True),
        session=session,
        heartbeat_interval=kwargs.pop("heartbeat_interval", 10.0),
        liveness_timeout=kwargs.pop("liveness_timeout", 30.0),
        reconnect_initial_delay=kwargs.pop("reconnect_initial_delay", 0.01),
        reconnect_max_delay=kwargs.pop("reconnect_max_delay", 0.05),
        **kwargs,
    )
    return listener, calls, client


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


def test_parse_push_message_maps_all_refresh_events() -> None:
    """Every refresh-* message maps to its sync event."""
    expected = {
        "refresh-shopping-lists": "shopping_lists",
        "refresh-categorized-items": "categorized_items",
        "refresh-list-folders": "list_folders",
        "refresh-list-settings": "list_settings",
        "refresh-starter-lists": "starter_lists",
        "refresh-ordered-starter-list-ids": "starter_list_order",
        "refresh-starter-list-settings": "starter_list_settings",
        "refresh-mobile-app-settings": "mobile_app_settings",
        "refresh-user-categories": "user_categories",
        "refresh-user-recipe-data": "recipe_data",
        "refresh-meal-plan-calendar": "meal_plan_calendar",
        "refresh-account-info": "account_info",
        "refresh-subscription-info": "subscription_info",
    }
    for message, event in expected.items():
        assert parse_push_message(message) == event, message


def test_parse_push_message_heartbeat_account_deleted_unknown() -> None:
    """Heartbeat, account deletion and unknown messages classify correctly."""
    assert parse_push_message("--heartbeat--") == "heartbeat"
    assert parse_push_message("did-delete-account") == "account_deleted"
    assert parse_push_message("unknown-message") is None
    assert parse_push_message("") is None


def test_reconnect_backoff_sequence_and_reset() -> None:
    """Backoff starts immediate, doubles, caps, and resets."""
    backoff = _ReconnectBackoff(initial_delay=2.0, max_delay=120.0)
    assert backoff.next_delay() == 0.0
    assert backoff.next_delay() == 2.0
    assert backoff.next_delay() == 4.0
    assert backoff.next_delay() == 8.0
    assert backoff.next_delay() == 16.0
    assert backoff.next_delay() == 32.0
    assert backoff.next_delay() == 64.0
    assert backoff.next_delay() == 120.0  # capped
    assert backoff.next_delay() == 120.0  # stays capped
    backoff.reset()
    assert backoff.next_delay() == 0.0
    assert backoff.next_delay() == 2.0


# ---------------------------------------------------------------------------
# Pause / resume helpers
# ---------------------------------------------------------------------------


def test_pause_coordinator_polling_cancels_pending_refresh() -> None:
    """Pausing clears the interval and cancels the pending refresh."""
    coordinator = _FakeCoordinator()
    pause_coordinator_polling(coordinator)
    assert coordinator.update_interval is None
    assert coordinator.unsub_calls == 1


def test_pause_coordinator_polling_without_unsub_method() -> None:
    """Pausing still works if the coordinator has no cancel method."""
    coordinator = SimpleNamespace(update_interval=timedelta(seconds=3600))
    pause_coordinator_polling(coordinator)  # must not raise
    assert coordinator.update_interval is None


def test_resume_coordinator_polling_restores_interval() -> None:
    """Resuming restores the fallback interval."""
    coordinator = _FakeCoordinator()
    pause_coordinator_polling(coordinator)
    resume_coordinator_polling(coordinator, 3600)
    assert coordinator.update_interval == timedelta(seconds=3600)


# ---------------------------------------------------------------------------
# Listener behaviour (fake session, no network)
# ---------------------------------------------------------------------------


async def test_event_triggers_sync_and_heartbeat_is_ignored() -> None:
    """A refresh event triggers a sync; heartbeats and unknowns do not."""
    session = _FakeSession()
    listener, calls, _ = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        ws = session.sockets[0]
        ws.push_text("refresh-user-recipe-data")
        await _wait_until(lambda: len(calls["sync"]) == 1)
        ws.push_text("--heartbeat--")
        ws.push_text("unknown-message")
        await asyncio.sleep(0.05)
        assert len(calls["sync"]) == 1
        ws.push_text("refresh-shopping-lists")
        await _wait_until(lambda: len(calls["sync"]) == 2)
        assert listener.state == ConnectionState.CONNECTED
    finally:
        await listener.stop()
    assert listener.state == ConnectionState.CLOSED


async def test_listener_uses_client_credentials_in_url() -> None:
    """The listener URL carries the client id and access token."""
    session = _FakeSession()
    listener, _, _ = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        assert session.connects == [
            "wss://www.anylist.com/data/add-user-listener"
            "?client_id=client-1&access_token=token-1"
        ]
    finally:
        await listener.stop()


async def test_heartbeat_is_sent_on_schedule() -> None:
    """The listener sends --heartbeat-- frames while connected."""
    session = _FakeSession()
    listener, _, _ = _make_listener(
        session, heartbeat_interval=0.05, liveness_timeout=30.0
    )
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        ws = session.sockets[0]
        await _wait_until(lambda: len(ws.sent) >= 2, timeout=2.0)
        assert all(frame == "--heartbeat--" for frame in ws.sent)
    finally:
        await listener.stop()


async def test_disconnect_reconnects_and_syncs_once() -> None:
    """A dropped connection reconnects, pauses nothing twice, and syncs."""
    session = _FakeSession()
    listener, calls, _ = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        assert len(calls["connected"]) == 1
        session.sockets[0].push_close()
        await _wait_until(lambda: len(session.connects) == 2)
        await _wait_until(lambda: listener.connected)
        assert len(calls["connected"]) == 2
        assert len(calls["disconnected"]) == 1
        # Reconnect does one full sync (changes during the gap).
        await _wait_until(lambda: len(calls["sync"]) == 1)
        assert session.sockets[1] is not session.sockets[0]
    finally:
        await listener.stop()


async def test_first_connect_failure_does_not_resume_polling() -> None:
    """A failed first connect never pauses polling, so nothing resumes."""
    session = _FakeSession(failures=[RuntimeError("boom")])
    listener, calls, _ = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        assert len(session.connects) == 2
        assert len(calls["connected"]) == 1
        assert calls["disconnected"] == []
        assert calls["sync"] == []
    finally:
        await listener.stop()


async def test_handshake_401_refreshes_token_and_reconnects() -> None:
    """A 401 handshake refreshes the token, then reconnects with it."""
    session = _FakeSession(failures=[_Fake401()])
    listener, calls, client = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        assert len(session.connects) == 2
        assert client.refresh_calls == 1
        assert session.connects[1].endswith("access_token=token-2")
        assert len(calls["connected"]) == 1
    finally:
        await listener.stop()


async def test_close_4010_refreshes_token_and_reconnects() -> None:
    """A 4010 close refreshes the token, then reconnects with it."""
    session = _FakeSession()
    listener, calls, client = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        session.sockets[0].push_close(code=4010)
        await _wait_until(lambda: len(session.connects) == 2)
        assert client.refresh_calls == 1
        assert session.connects[1].endswith("access_token=token-2")
        await _wait_until(lambda: listener.connected)
    finally:
        await listener.stop()


async def test_failed_token_refresh_keeps_retrying() -> None:
    """A failing token refresh backs off and retries instead of dying."""
    session = _FakeSession(failures=[_Fake401()])
    client = _FakeClient()
    client.refresh_error = RuntimeError("refresh down")
    listener, _, _ = _make_listener(session, client=client)
    listener.start()
    try:
        await _wait_until(lambda: client.refresh_calls >= 2, timeout=3.0)
        assert listener.state in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        )
        assert len(session.connects) == 1  # never got past the refresh
    finally:
        await listener.stop()


async def test_liveness_timeout_drops_a_silent_connection() -> None:
    """No inbound frame for the liveness timeout means reconnect."""
    session = _FakeSession()
    listener, calls, _ = _make_listener(
        session, heartbeat_interval=0.05, liveness_timeout=0.12
    )
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        # The socket stays silent: no frames ever arrive.
        await _wait_until(lambda: len(session.connects) == 2, timeout=3.0)
        assert len(calls["disconnected"]) == 1
        await _wait_until(lambda: listener.connected)
    finally:
        await listener.stop()


async def test_account_deleted_stops_the_listener() -> None:
    """An account-deletion notice stops the feed for good."""
    session = _FakeSession()
    listener, calls, _ = _make_listener(session)
    listener.start()
    try:
        await _wait_until(lambda: listener.connected)
        session.sockets[0].push_text("did-delete-account")
        await _wait_until(lambda: listener.state == ConnectionState.CLOSED)
        assert len(session.connects) == 1  # no reconnect
        assert calls["sync"] == []
    finally:
        await listener.stop()


async def test_stop_is_idempotent_and_closes_the_socket() -> None:
    """Stopping twice is safe and closes the socket."""
    session = _FakeSession()
    listener, _, _ = _make_listener(session)
    listener.start()
    await _wait_until(lambda: listener.connected)
    await listener.stop()
    await listener.stop()
    assert listener.state == ConnectionState.CLOSED
    assert session.sockets[0].closed
    assert session.sockets[0].close_calls >= 1
