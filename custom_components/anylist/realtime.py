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
- reconnect with exponential backoff (first retry immediate, then 2 s,
  doubling, capped at 120 s); a session that lived at least 15 s resets
  the backoff
- a 401 handshake (or a 4010 close) refreshes the tokens once, then
  reconnects with the fresh token
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
RECONNECT_MAX_DELAY = 120.0  # seconds, backoff cap
CONNECT_TIMEOUT = 10.0  # seconds per handshake attempt
SEND_TIMEOUT = 5.0  # seconds per heartbeat write
TOKEN_REFRESH_TIMEOUT = 15.0  # seconds for a token refresh
CLOSE_CODE_UNAUTHORIZED = 4010  # server close code: refresh the token

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


class _ConnectFailed(Exception):
    """A listener connection attempt failed for a non-auth reason."""


class _Closed(Exception):
    """The listener was stopped while waiting (internal control flow)."""


class _ReconnectBackoff:
    """Exponential reconnect backoff, mirroring anylist_rs.

    The first retry after a session ends is immediate; each further retry
    waits twice as long as the previous one, starting at the initial delay
    and capped at the maximum. ``reset()`` (after a session that lived a
    while) starts over.
    """

    def __init__(
        self,
        initial_delay: float = RECONNECT_INITIAL_DELAY,
        max_delay: float = RECONNECT_MAX_DELAY,
    ) -> None:
        self._initial_delay = max(initial_delay, 0.001)
        self._max_delay = max(max_delay, 0.001)
        self._next = 0.0

    def next_delay(self) -> float:
        """Return the delay before the next attempt and advance the backoff."""
        delay = self._next
        if delay == 0.0:
            self._next = min(self._initial_delay, self._max_delay)
        else:
            self._next = min(delay * 2, self._max_delay)
        return delay

    def reset(self) -> None:
        """Start over (call after a session that lived a while)."""
        self._next = 0.0


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
    off and reconnects until stopped. Callbacks are invoked on that task;
    keep them short — a slow callback delays heartbeats.

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
        self._connect_timeout = connect_timeout
        self._state = ConnectionState.DISCONNECTED
        self._task: asyncio.Task | None = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._ws: Any | None = None

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
            self._reconnect_initial_delay, self._reconnect_max_delay
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
                except _Unauthorized:
                    # Token refresh failed or the fresh token was still
                    # rejected: back off and try again (refresh retried).
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
        (after refreshing the tokens once, if asked), ``_ConnectFailed``
        for anything else.
        """
        if refresh_first:
            try:
                await self._refresh_tokens()
            except Exception as err:
                # A failed refresh is a failed connection attempt, not a
                # reason for the task to die: back off and retry (the
                # fallback poll surfaces a permanently dead login).
                raise _ConnectFailed(f"token refresh failed: {err}") from err
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
        """Refresh the client's tokens (synchronous client, in the executor)."""
        await async_call_with_timeout(
            self._hass,
            self._client.refresh_tokens,
            timeout=TOKEN_REFRESH_TIMEOUT,
        )

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
