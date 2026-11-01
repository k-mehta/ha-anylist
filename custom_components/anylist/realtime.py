"""AnyList push feed: a persistent WebSocket listener (design §8 phase 2).

The push feed is the primary sync mechanism. While it is connected, the
coordinator does not poll at all — no scheduled downloads, day or night.
Polling is only a safety net while the feed is down, at the configurable
fallback interval (see ``get_fallback_poll_interval``).

Protocol (mirrors anylist_rs 0.5.1 ``src/realtime/``, the crate AnyList's
own web client uses):

- ``wss://www.anylist.com/data/add-user-listener?client_id=..&access_token=..``
- the client sends ``--heartbeat--`` every 5 s; any inbound frame proves
  liveness (15 s without one means the connection is dead)
- the server pushes tiny text messages (``refresh-user-recipe-data`` and
  ~12 other ``refresh-*`` events): coarse notifications only, no payload
  saying what changed — the client re-fetches after one
- reconnect with two-phase backoff: the first 10 attempts retry fast
  (immediate, then 2 s, doubling, capped at 120 s) to recover from
  brief blips; after that the retry slows to once every 30 minutes,
  indefinitely, until it connects — the feed must always come back,
  but a prolonged outage must never look like hammering. A session
  that lived at least 15 s resets the backoff to the fast phase.
- a 401 handshake (or a 4010 close) refreshes the tokens once, then
  reconnects with the fresh token; if the refresh itself fails 3
  times in a row the login is dead, so the feed stops attempting and
  lets the fallback poll surface the auth error (the feed comes back
  on the next restart / config-entry reload once the login is fixed)
- on reconnect, changes made during the gap were not delivered, so the
  client does a full sync

The listener never logs the listener URL: it carries the access token.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import inspect
import logging
from typing import Any
from urllib.parse import quote

from aiohttp import WSMsgType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import AnyListClient, async_call_with_timeout

_LOGGER = logging.getLogger(__name__)

LISTENER_BASE_URL = "wss://www.anylist.com/data/add-user-listener"
HEARTBEAT_MESSAGE = "--heartbeat--"
ACCOUNT_DELETED_MESSAGE = "did-delete-account"

# Protocol timings, matching anylist_rs defaults (the official web client).
HEARTBEAT_INTERVAL = 5.0  # seconds between --heartbeat-- sends
LIVENESS_TIMEOUT = 15.0  # seconds without any inbound frame = dead
RECONNECT_INITIAL_DELAY = 2.0  # seconds before the first backoff retry
RECONNECT_MAX_DELAY = 120.0  # seconds, fast-phase backoff cap
CONNECT_TIMEOUT = 10.0  # seconds per handshake attempt
SEND_TIMEOUT = 5.0  # seconds per heartbeat write
TOKEN_REFRESH_TIMEOUT = 15.0  # seconds for a token refresh
CLOSE_CODE_UNAUTHORIZED = 4010  # server close code: refresh the token

# Two-phase reconnect cadence (confirmed Nov 2, 2026). The fast phase
# covers brief blips; the slow phase keeps a dead feed retrying forever
# at a rate no telemetry could read as hammering. The retry never stops
# — except for a dead login (see MAX_TOKEN_REFRESH_FAILURES), where
# retrying cannot help and only looks like abuse.
FAST_ATTEMPT_COUNT = 10  # fast-phase attempts before slowing down
SLOW_RECONNECT_DELAY = 1800.0  # seconds between retries once slow (30 min)
MAX_TOKEN_REFRESH_FAILURES = 3  # consecutive failed refreshes = dead login

# Messages that mean "something changed, re-fetch". The coordinator
# refresh re-downloads the whole snapshot, so every refresh-* event maps
# to the same action: one sync. (anylist_rs src/realtime/events.rs.)
SYNC_EVENTS: dict[str, str] = {
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


def parse_push_message(message: str) -> str | None:
    """Map a push-feed text message to what the listener should do with it.

    Returns the sync event name (for example ``"recipe_data"``) for the
    ``refresh-*`` messages, ``"heartbeat"`` for the heartbeat,
    ``"account_deleted"`` for the account-deletion notice, and ``None``
    for anything unknown (ignored).
    """
    if message == HEARTBEAT_MESSAGE:
        return "heartbeat"
    if message == ACCOUNT_DELETED_MESSAGE:
        return "account_deleted"
    return SYNC_EVENTS.get(message)


class _Unauthorized(Exception):
    """The listener handshake was rejected with HTTP 401."""


class _RefreshFailed(Exception):
    """A token refresh call failed (the login may be dead)."""


class _ConnectFailed(Exception):
    """A listener connection attempt failed for a non-auth reason."""


class _Closed(Exception):
    """The listener was stopped while waiting (internal control flow)."""


class _ReconnectBackoff:
    """Two-phase reconnect backoff.

    Fast phase (brief blips): the first ``fast_attempts`` delays follow
    the doubling sequence — immediate, then the initial delay, doubling,
    capped at the maximum. Slow phase (real outage): every delay after
    that is ``slow_delay``, so a prolonged outage probes the server only
    every 30 minutes instead of hammering it every 2 minutes — while
    still retrying forever, so the feed always comes back. ``reset()``
    (after a session that lived a while) starts the fast phase over.
    """

    def __init__(
        self,
        initial_delay: float = RECONNECT_INITIAL_DELAY,
        max_delay: float = RECONNECT_MAX_DELAY,
        fast_attempts: int = FAST_ATTEMPT_COUNT,
        slow_delay: float = SLOW_RECONNECT_DELAY,
    ) -> None:
        self._initial_delay = max(initial_delay, 0.001)
        self._max_delay = max(max_delay, 0.001)
        self._fast_attempts = max(fast_attempts, 1)
        self._slow_delay = max(slow_delay, 0.001)
        self._next = 0.0
        self._calls = 0

    def next_delay(self) -> float:
        """Return the delay before the next attempt and advance the backoff."""
        self._calls += 1
        if self._calls > self._fast_attempts:
            return self._slow_delay
        delay = self._next
        if delay == 0.0:
            self._next = min(self._initial_delay, self._max_delay)
        else:
            self._next = min(delay * 2, self._max_delay)
        return delay

    def reset(self) -> None:
        """Start over (call after a session that lived a while)."""
        self._next = 0.0
        self._calls = 0


class ConnectionState:
    """Listener connection states (plain strings, for diagnostics/tests)."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"


async def _call_callback(callback: Any) -> None:
    """Call a listener callback, awaiting it if it returns an awaitable.

    A failing callback is logged, never raised: the feed task must not
    die because a sync request failed (the next event or the fallback
    poll covers it).
    """
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception as err:
        _LOGGER.warning("AnyList push feed callback failed: %s", err)


def pause_coordinator_polling(coordinator: DataUpdateCoordinator) -> None:
    """Stop scheduled coordinator polls while the push feed is connected.

    Setting the interval to ``None`` makes the coordinator schedule
    nothing more — but a refresh may already be scheduled for the old
    interval, so that pending callback is cancelled too. Without the
    cancel, one last poll could fire up to a full fallback interval
    after the feed connects (for example in the middle of the night),
    which is exactly what the push feed exists to avoid.
    ``_async_unsub_refresh`` is Home Assistant-internal; if a future
    version removes it, the worst case is one trailing poll, so a
    missing method is only logged, never raised.
    """
    coordinator.update_interval = None
    unsub = getattr(coordinator, "_async_unsub_refresh", None)
    if unsub is not None:
        try:
            unsub()
        except Exception as err:  # never break the feed on this
            _LOGGER.debug("Could not cancel a pending AnyList poll: %s", err)


def resume_coordinator_polling(
    coordinator: DataUpdateCoordinator, interval_seconds: int
) -> None:
    """Restore scheduled coordinator polls at the fallback interval.

    Used when the push feed drops: polling is the safety net until the
    feed reconnects. Setting the interval alone schedules nothing — the
    next poll is only scheduled when a refresh finishes — so callers
    that want fresh data immediately should also request a refresh
    (the disconnect handler does).
    """
    coordinator.update_interval = timedelta(seconds=interval_seconds)


class AnyListPushListener:
    """Owns the AnyList WebSocket push feed for one config entry.

    One background task owns the connection outright: it connects, runs a
    session (inbound frames, heartbeat tick, liveness check), then backs
    off and reconnects until stopped — fast for the first 10 attempts,
    then once every 30 minutes, forever, so the feed always comes back
    without hammering a dead server. The only case where it stops
    retrying is a dead login (3 failed token refreshes in a row); then
    the fallback poll takes over until the next restart / reload.
    Callbacks are invoked on that task; keep them short — a slow
    callback delays heartbeats.

    ``on_sync`` fires for every sync-triggering push event and once on
    reconnect (changes during the gap were not delivered). ``on_connected``
    fires on every (re)connect, ``on_disconnected`` only when a live
    connection drops (never when the first connect fails — polling was
    never paused then, so there is nothing to resume).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: AnyListClient,
        *,
        on_sync: Any,
        on_connected: Any,
        on_disconnected: Any,
        session: Any | None = None,
        url: str | None = None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        liveness_timeout: float = LIVENESS_TIMEOUT,
        reconnect_initial_delay: float = RECONNECT_INITIAL_DELAY,
        reconnect_max_delay: float = RECONNECT_MAX_DELAY,
        fast_attempts: int = FAST_ATTEMPT_COUNT,
        slow_delay: float = SLOW_RECONNECT_DELAY,
        max_refresh_failures: int = MAX_TOKEN_REFRESH_FAILURES,
        connect_timeout: float = CONNECT_TIMEOUT,
    ) -> None:
        self._hass = hass
        self._client = client
        self._on_sync = on_sync
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._session = session
        self._url = url
        self._heartbeat_interval = heartbeat_interval
        self._liveness_timeout = liveness_timeout
        self._reconnect_initial_delay = reconnect_initial_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._fast_attempts = fast_attempts
        self._slow_delay = slow_delay
        self._max_refresh_failures = max_refresh_failures
        self._connect_timeout = connect_timeout
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._ws: Any | None = None
        # Consecutive token-refresh failures. A successful refresh resets
        # it (see _refresh_tokens); reaching the bound means the login is
        # dead and the feed stops attempting (see _run).
        self._refresh_failures = 0

    @property
    def state(self) -> str:
        """Return the current connection state."""
        return self._state

    @property
    def connected(self) -> bool:
        """Return True while the feed is live."""
        return self._state == ConnectionState.CONNECTED

    def _listener_url(self) -> str:
        """Build the listener URL from the client's credentials.

        Never log the result: it carries the access token.
        """
        return (
            f"{LISTENER_BASE_URL}"
            f"?client_id={quote(self._client.client_identifier)}"
            f"&access_token={quote(self._client.access_token)}"
        )

    def start(self) -> None:
        """Start the listener's background task (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._closed_event.clear()
        self._task = asyncio.create_task(self._run(), name="anylist push feed")

    async def stop(self) -> None:
        """Stop the listener for good and wait for its task to finish."""
        self._closed = True
        self._closed_event.set()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=SEND_TIMEOUT)
            except Exception as err:  # already gone; nothing to do
                _LOGGER.debug("Error closing AnyList push feed: %s", err)
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._state = ConnectionState.CLOSED

    async def _run(self) -> None:
        """Connect, run sessions, and reconnect until stopped."""
        backoff = _ReconnectBackoff(
            self._reconnect_initial_delay,
            self._reconnect_max_delay,
            self._fast_attempts,
            self._slow_delay,
        )
        needs_refresh = False
        first = True
        was_connected = False
        permanent_stop = False
        try:
            while not self._closed:
                self._state = ConnectionState.CONNECTING
                try:
                    ws = await self._open(needs_refresh)
                except _RefreshFailed:
                    # The refresh call itself failed. A few failures in a
                    # row mean the login is dead: refreshing cannot help,
                    # and hammering a dead login is exactly the pattern
                    # that looks like abuse. Stop attempting the feed and
                    # let the fallback poll surface the auth error — the
                    # feed comes back on the next restart / reload once
                    # the login is fixed. This is the only case where the
                    # feed stops retrying; every other failure keeps
                    # retrying (fast, then slow) forever.
                    self._refresh_failures += 1
                    if self._refresh_failures >= self._max_refresh_failures:
                        _LOGGER.warning(
                            "AnyList push feed token refresh failed %s times; "
                            "stopping the feed (login is dead)",
                            self._refresh_failures,
                        )
                        await _call_callback(self._on_disconnected)
                        permanent_stop = True
                        break
                    if await self._sleep_or_close(backoff.next_delay()):
                        break
                    continue
                except _Unauthorized:
                    # The token was rejected: back off and try again with
                    # a refreshed token (the next _open refreshes first).
                    needs_refresh = True
                    if await self._sleep_or_close(backoff.next_delay()):
                        break
                    continue
                except _ConnectFailed as err:
                    _LOGGER.debug("AnyList push feed connect failed: %s", err)
                    if await self._sleep_or_close(backoff.next_delay()):
                        break
                    continue

                needs_refresh = False
                session_start = asyncio.get_running_loop().time()
                self._ws = ws
                self._state = ConnectionState.CONNECTED
                await _call_callback(self._on_connected)
                if not first:
                    # Changes during the gap were not delivered: full sync.
                    await _call_callback(self._on_sync)
                first = False
                was_connected = True
                try:
                    reason = await self._run_session(ws)
                finally:
                    self._ws = None
                    try:
                        await asyncio.wait_for(ws.close(), timeout=SEND_TIMEOUT)
                    except Exception:
                        pass
                if reason == "stop":
                    # Account deleted: no point reconnecting.
                    permanent_stop = True
                    break
                if (
                    asyncio.get_running_loop().time() - session_start
                    >= self._liveness_timeout
                ):
                    # Only a session that lived for a while resets the
                    # backoff, so a flapping connection keeps backing off.
                    backoff.reset()
                needs_refresh = reason == "refresh_tokens"
                self._state = ConnectionState.RECONNECTING
                await _call_callback(self._on_disconnected)
                if await self._sleep_or_close(backoff.next_delay()):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # the task must never die on its own
            _LOGGER.warning("AnyList push feed stopped unexpectedly: %s", err)
        finally:
            if self._closed or permanent_stop:
                self._state = ConnectionState.CLOSED
            elif not was_connected:
                self._state = ConnectionState.DISCONNECTED
            else:
                self._state = ConnectionState.RECONNECTING

    async def _sleep_or_close(self, delay: float) -> bool:
        """Sleep for the backoff delay; return True if stopped meanwhile."""
        if delay <= 0:
            return self._closed
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return self._closed

    async def _open(self, refresh_first: bool) -> Any:
        """Open one listener connection (one handshake attempt).

        Raises ``_Unauthorized`` when the handshake is rejected with 401
        (after refreshing the tokens once, if asked), ``_RefreshFailed``
        when the token refresh call itself fails, and ``_ConnectFailed``
        for anything else.
        """
        if refresh_first:
            try:
                await self._refresh_tokens()
            except Exception as err:
                raise _RefreshFailed(f"token refresh failed: {err}") from err
        session = self._session or async_get_clientsession(self._hass)
        url = self._url or self._listener_url()
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(url, heartbeat=None),
                timeout=self._connect_timeout,
            )
        except Exception as err:
            if getattr(err, "status", None) == 401:
                raise _Unauthorized from err
            raise _ConnectFailed(str(err) or err.__class__.__name__) from err
        return ws

    async def _refresh_tokens(self) -> None:
        """Refresh the client's tokens (synchronous client, in the executor).

        A successful refresh breaks the streak of consecutive refresh
        failures, so a login that recovers does not trip the dead-login
        bound from old failures.
        """
        await async_call_with_timeout(
            self._hass,
            self._client.refresh_tokens,
            timeout=TOKEN_REFRESH_TIMEOUT,
        )
        self._refresh_failures = 0

    async def _send_heartbeat(self, ws: Any) -> None:
        """Send one heartbeat frame (raises if the socket is dead)."""
        await asyncio.wait_for(ws.send_str(HEARTBEAT_MESSAGE), timeout=SEND_TIMEOUT)

    async def _run_session(self, ws: Any) -> str:
        """Run one live session until it ends.

        Returns ``"reconnect"`` (dropped, try again), ``"refresh_tokens"``
        (closed with 4010: refresh, then reconnect), or ``"stop"``
        (account deleted: do not reconnect).
        """
        loop = asyncio.get_running_loop()
        last_seen = loop.time()
        next_heartbeat = last_seen + self._heartbeat_interval
        while True:
            timeout = max(0.0, next_heartbeat - loop.time())
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=timeout)
            except asyncio.TimeoutError:
                if loop.time() - last_seen > self._liveness_timeout:
                    _LOGGER.debug(
                        "AnyList push feed silent for over %s s; reconnecting",
                        self._liveness_timeout,
                    )
                    return "reconnect"
                await self._send_heartbeat(ws)
                next_heartbeat = loop.time() + self._heartbeat_interval
                continue
            last_seen = loop.time()
            if message.type == WSMsgType.TEXT:
                event = parse_push_message(message.data)
                if event is None or event == "heartbeat":
                    continue
                if event == "account_deleted":
                    _LOGGER.error(
                        "AnyList reported the account was deleted; "
                        "stopping the push feed"
                    )
                    return "stop"
                _LOGGER.debug("AnyList push event: %s", event)
                await _call_callback(self._on_sync)
                continue
            if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                return (
                    "refresh_tokens"
                    if ws.close_code == CLOSE_CODE_UNAUTHORIZED
                    else "reconnect"
                )
            # Binary and anything else: unexpected, ignore.
            _LOGGER.debug("Ignoring unexpected AnyList push frame: %s", message.type)
