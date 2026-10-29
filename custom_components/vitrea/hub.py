from __future__ import annotations
import logging
import re
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from ._node_types import node_category
from ._mobile_client import KEY_OFF, KEY_ON, MobileVBox
from .const import (
    DOMAIN,
    CONF_MOBILE_PASSWORD,
    CONF_MOBILE_PORT,
    CONF_MOBILE_USER,
    CONF_DISABLE_METADATA_CACHE,
    CONF_EXCLUDED_KEYS,
    DEFAULT_DISABLE_METADATA_CACHE,
    DEFAULT_EXCLUDED_KEYS,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# get_key_params "category" byte -> HA platform, for anything not caught
# by the dimmer/toggle/blind special cases below.
_KEY_CAT_HA: dict[int, str] = {
    2: "switch",   # Toggle
    3: "cover",    # Blind
    4: "switch",   # PushButton
    5: "light",    # Dimmer
    10: "light",   # DimmerMW
    11: "cover",   # BlindMW
    13: "switch",  # ToggleScene
    14: "switch",  # DualPole
    15: "switch",  # Boiler
    18: "fan",     # Fan
}
WATCHDOG_INTERVAL = timedelta(seconds=15)
# Safety-net resync: push is the primary source of truth, but a single
# missed push would otherwise leave a card wrong until it is reopened.
POLL_INTERVAL = timedelta(minutes=5)
# The box stops answering a session when it is asked for too much (seen
# live: sustained request bursts, e.g. a 40-node sync or ~100 per-entity
# status reads after a reconnect, then every request times out for a
# while). Keep background traffic minimal: push is the primary source of
# truth, the poll is only a slow safety net, node status is shared via a
# short cache, and an idle session gets a cheap ping so a dead one is
# replaced before a user command hits it.
# Measured live 2026-10-29: the box silently stops answering a session every
# few minutes (7 dead sessions caught in ~20 min), and a normal reply takes
# ~10 ms. Ping often enough that a dead session is replaced in the
# background before a user command hits it, and use a short timeout for
# pings and commands so a command that does hit a dead one stalls ~4 s
# (timeout + reconnect) instead of ~11 s (10 s timeout + reconnect).
KEEPALIVE_CHECK = timedelta(seconds=5)
KEEPALIVE_IDLE = 10.0
KEEPALIVE_TIMEOUT = 3.0
COMMAND_TIMEOUT = 3.0
STATUS_CACHE_TTL = 5.0
METADATA_CACHE_VERSION = 1
METADATA_PACE = 0.1
METADATA_RELOGIN_EVERY = 60
# Don't poll right after a command (the box serializes requests and was
# seen to time out under load), and re-read a key once its command burst
# settles so a fast double-click ends on the real physical state.
POLL_QUIET_AFTER_COMMAND = 10.0
VERIFY_DELAY = 1.5

def _parse_excluded_keys(raw: str) -> set[tuple[int, int]]:
    """Parse the `excluded_keys` option: "node:key,node:key", 0-indexed.

    Per-install exclusions (e.g. a key that turns out to cross-push the
    same physical circuit as another one on your setup) belong here, in
    config, not hardcoded in this file — every install's wiring quirks
    are its own.
    """
    out: set[tuple[int, int]] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            node_s, key_s = part.split(":", 1)
            out.add((int(node_s), int(key_s)))
        except ValueError:
            _LOGGER.warning("Vitrea: ignoring malformed excluded_keys entry %r", part)
    return out


class VitreaHub:
    """Owns one admin-protocol metadata snapshot and one persistent
    mobile-protocol connection (control + live push) for a single V-Box.
    """

    def __init__(self, hass: "HomeAssistant", data: dict, options: dict | None = None) -> None:
        self.hass = hass
        self._data = data
        self._disable_metadata_cache = (options or {}).get(
            CONF_DISABLE_METADATA_CACHE, DEFAULT_DISABLE_METADATA_CACHE
        )
        self._excluded_keys = _parse_excluded_keys(
            (options or {}).get(CONF_EXCLUDED_KEYS, DEFAULT_EXCLUDED_KEYS)
        )
        self.keys: list[dict] = []
        self.scenarios: list[dict] = []
        self.available = False
        self.metadata_ready = False
        self._mobile: MobileVBox | None = None
        # Keyed by (node, MOBILE key index) — what push events carry.
        self._entities: dict[tuple[int, int], object] = {}
        # (node, admin key index) -> mobile key index; filled once
        # metadata is ready. Entities and unique IDs keep admin indices.
        self._mobile_key: dict[tuple[int, int], int] = {}
        self._watchdog_unsub = None
        self._poll_unsub = None
        self._keepalive_unsub = None
        self._store = Store(hass, 1, "vitrea_metadata")
        self._last_activity = time.monotonic()
        self._status_cache: dict[int, tuple[float, dict]] = {}
        self._meta_count = 0
        self._last_command = 0.0
        # Two logins on one mobile account kill both sessions, so every
        # reconnect goes through this lock; _epoch counts successful
        # (re)connects so a thread that waited on the lock can see someone
        # else already replaced the session and skip a second login.
        self._connect_lock = threading.RLock()
        self._epoch = 0
        self._verify_cancel: dict[tuple[int, int], object] = {}
        self._metadata_listeners: list = []

    async def async_setup(self) -> None:
        # Mobile connect is fast (one login) — do that first so control/
        # push works immediately. The admin metadata scan below is NOT
        # awaited here: confirmed live 2026-10-25 that it takes minutes
        # (57 nodes x ~8 keys, hundreds of sequential admin-protocol
        # round trips) and blocking config-entry setup on it risks HA's
        # setup-timeout cancellation outright orphaning every entity.
        # It runs as a background task instead; platforms add their
        # entities once it completes via add_metadata_listener below.
        await self.hass.async_add_executor_job(self._connect_mobile)
        self._watchdog_unsub = async_track_time_interval(
            self.hass, self._async_watchdog_tick, WATCHDOG_INTERVAL
        )
        self._poll_unsub = async_track_time_interval(
            self.hass, self._async_poll_tick, POLL_INTERVAL
        )
        self._keepalive_unsub = async_track_time_interval(
            self.hass, self._async_keepalive_tick, KEEPALIVE_CHECK
        )
        self.hass.async_create_background_task(
            self._async_fetch_and_apply_metadata(), "vitrea_metadata_fetch"
        )

    async def async_unload(self) -> None:
        if self._watchdog_unsub is not None:
            self._watchdog_unsub()
            self._watchdog_unsub = None
        if self._poll_unsub is not None:
            self._poll_unsub()
            self._poll_unsub = None
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        for cancel in self._verify_cancel.values():
            cancel()
        self._verify_cancel.clear()
        if self._mobile is not None:
            await self.hass.async_add_executor_job(self._mobile.close)

    def _mk(self, node: int, key: int) -> int:
        return self._mobile_key.get((node, key), key)

    def register_entity(self, node: int, key: int, entity) -> None:
        self._entities[(node, self._mk(node, key))] = entity

    def add_metadata_listener(self, callback) -> None:
        """Call `callback()` once admin metadata (self.keys/scenarios) is
        ready. Each platform's async_setup_entry registers one of these
        instead of reading hub.keys synchronously — metadata may not be
        ready yet when platforms are set up (see async_setup above).
        Calls back immediately if metadata is already ready.
        """
        if self.metadata_ready:
            callback()
        else:
            self._metadata_listeners.append(callback)

    async def _async_fetch_and_apply_metadata(self) -> None:
        # The ~90s scan only needs to run when the controller's config
        # changed: opcode 3 returns the last-change timestamp, which is
        # the cache key. One request instead of ~330 on a normal restart.
        stamp = None
        try:
            stamp = (await self.hass.async_add_executor_job(self._mobile.get_last_change_timestamp)).hex()
        except Exception as err:
            _LOGGER.warning("Vitrea: could not read config timestamp: %s", err)
        cached = None
        if not self._disable_metadata_cache:
            try:
                cached = await self._store.async_load()
            except Exception:
                _LOGGER.exception("Vitrea: metadata cache unreadable")
        if (
            cached
            and cached.get("version") == METADATA_CACHE_VERSION
            and cached.get("keys")
            and (stamp is None or cached.get("stamp") == stamp)
        ):
            self.keys, self.scenarios = cached["keys"], cached["scenarios"]
            _LOGGER.info("Vitrea: using cached metadata (%d keys)", len(self.keys))
        else:
            try:
                self.keys, self.scenarios = await self.hass.async_add_executor_job(self._fetch_metadata)
                if self.keys and stamp is not None:
                    await self._store.async_save({
                        "version": METADATA_CACHE_VERSION, "stamp": stamp,
                        "keys": self.keys, "scenarios": self.scenarios,
                    })
            except Exception as err:
                _LOGGER.error("Vitrea metadata fetch failed: %s", err)
                self.keys, self.scenarios = [], []
        self._mobile_key = {(k["node"], k["key"]): k["mobile_key"] for k in self.keys}
        try:
            self._async_migrate_unique_ids()
        except Exception:
            _LOGGER.exception("Vitrea unique-id migration failed")
        self.metadata_ready = True
        for callback in self._metadata_listeners:
            try:
                callback()
            except Exception:
                _LOGGER.exception("Vitrea metadata listener failed")
        self._metadata_listeners.clear()

    def _async_migrate_unique_ids(self) -> None:
        """Move entities registered under admin key indices to mobile ones.

        Entities were originally created with unique_id vitrea_{node}_{admin
        key}. Keys after an MW alias now have a different (mobile) index, so
        without this HA would orphan them and create duplicates, losing
        renames/areas. Match by (node, name); two phases via temporary IDs
        so swapped indices on one node can't collide mid-update.
        """
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(self.hass)
        by_name = {(k["node"], k["name"].strip().lower()): k for k in self.keys}
        moves: list[tuple[str, str]] = []
        for e in list(reg.entities.values()):
            if e.platform != DOMAIN:
                continue
            m = re.fullmatch(r"vitrea_(\d+)_(\d+)", e.unique_id or "")
            if m is None:
                continue
            node = int(m.group(1))
            k = by_name.get((node, (e.original_name or "").strip().lower()))
            if k is None:
                continue
            new = f"vitrea_{node}_{k['key']}"
            if new != e.unique_id:
                moves.append((e.entity_id, new))
        if not moves:
            return
        for eid, _ in moves:
            reg.async_update_entity(eid, new_unique_id=f"vitrea_migrating_{eid}")
        targets = {new for _, new in moves}
        moved = {eid for eid, _ in moves}
        # A leftover legacy entity (e.g. a key later excluded via config)
        # that still owns an ID we are about to assign would block the move.
        for e in list(reg.entities.values()):
            if e.platform == DOMAIN and e.unique_id in targets and e.entity_id not in moved:
                _LOGGER.warning("Vitrea: removing stale entity %s blocking id %s", e.entity_id, e.unique_id)
                reg.async_remove(e.entity_id)
        for eid, new in moves:
            try:
                reg.async_update_entity(eid, new_unique_id=new)
                _LOGGER.info("Vitrea: migrated %s to unique_id %s", eid, new)
            except ValueError as err:
                _LOGGER.warning("Vitrea: could not migrate %s to %s: %s", eid, new, err)

    # ---------------------------------------------------------------
    # Metadata: names/types/rooms/scenarios, fetched over the same mobile
    # connection used for control (opcodes 20/21/30/31/40/60/61). The
    # admin protocol is no longer used at all.
    # ---------------------------------------------------------------
    def _paced(self, method: str, *args, attempts: int = 4):
        """Call a metadata request on the mobile client, paced and retried.

        Confirmed live 2026-10-26: after a few hundred requests the box
        stops answering on a session (every later request then burns the
        10s read timeout, even with spacing between requests), but a fresh
        login answers again. So: space requests, re-login on a timeout,
        and re-login proactively every METADATA_RELOGIN_EVERY requests.
        """
        for attempt in range(attempts):
            try:
                if self._meta_count and self._meta_count % METADATA_RELOGIN_EVERY == 0:
                    self._connect_mobile()
                self._meta_count += 1
                self._last_activity = time.monotonic()
                result = getattr(self._mobile, method)(*args)
                time.sleep(METADATA_PACE)
                return result
            except Exception:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.5 * (attempt + 1))
                try:
                    self._connect_mobile()
                except Exception:
                    pass

    def _fetch_metadata(self) -> tuple[list[dict], list[dict]]:
        paced = self._paced
        self._meta_count = 0
        room_names: dict[int, str] = {}
        try:
            for rno in paced("get_room_numbers"):
                try:
                    room_names[rno] = paced("get_room_name", rno)
                except Exception:
                    room_names[rno] = f"Room {rno}"
        except Exception:
            pass

        keys: list[dict] = []
        for nno in paced("get_node_numbers"):
            try:
                info = paced("get_node_params", nno)
            except Exception as err:
                _LOGGER.warning("Vitrea: node %s params unavailable: %s", nno, err)
                continue
            node_cat = node_category(info["node_type"])
            # The mobile key table lists only physical (class 255) keys, in
            # order, so position == mobile key index. MW aliases (admin
            # class 0) are simply absent — see _mobile_client.get_key_params.
            for ki in range(info["num_keys"]):
                try:
                    kp = paced("get_key_params", nno, ki)
                except Exception:
                    _LOGGER.warning("Vitrea: key params for N%s-K%s unavailable, skipping", nno, ki)
                    continue
                name = kp["name"] or f"N{nno}-K{ki}"
                lname = name.strip().lower()
                cat_id = kp["category"]
                if cat_id == 0:
                    continue  # NotExist — genuinely no key here
                if lname.endswith("mw") or " pair" in lname:
                    continue  # named MW/pair alias convention
                if (nno, ki) in self._excluded_keys:
                    continue

                dimmable = cat_id in {5, 10} or node_cat == "Dimmer"
                if "fan" in lname:
                    ha_type = "fan"
                elif cat_id in {3, 11}:
                    # Reliable signal on its own — every confirmed blind
                    # key reports cat_id 3/11 directly (node_cat=="Blind"
                    # misclassified reading lights that share a Blind-
                    # type node with real blind keys).
                    ha_type = "cover"
                elif dimmable or node_cat == "Toggle" or "light" in lname:
                    # "light" in the name: reading lights sit on Blind-type
                    # nodes (seen live: a reading light sharing a node with
                    # a blind) and would otherwise fall through to "switch".
                    ha_type = "light"
                else:
                    ha_type = _KEY_CAT_HA.get(cat_id, "switch")

                keys.append({
                    "node": nno,
                    # The mobile slot index is the only key index now, so
                    # "key" == "mobile_key". Unique IDs built from the old
                    # admin index are moved over by
                    # _async_migrate_unique_ids.
                    "key": ki,
                    "mobile_key": ki,
                    "name": name,
                    "ha_type": ha_type,
                    "dimmable": dimmable,
                    "room": room_names.get(kp["room_no"] or info["room_no"], ""),
                })

        scenarios: list[dict] = []
        try:
            for sno in paced("get_scenario_numbers"):
                try:
                    scenarios.append({"id": sno, "name": paced("get_scenario_name", sno)})
                except Exception:
                    _LOGGER.warning("Vitrea: scenario %s name unavailable", sno)
        except Exception as err:
            _LOGGER.warning("Vitrea: scenario list unavailable: %s", err)
        return keys, scenarios

    # ---------------------------------------------------------------
    # Mobile protocol: persistent connection, control + live push.
    # ---------------------------------------------------------------
    def _connect_mobile(self) -> None:
        with self._connect_lock:
            if self._mobile is not None:
                try:
                    self._mobile.close()
                except Exception:
                    pass
            d = self._data
            m = MobileVBox(
                host=d["host"],
                port=d[CONF_MOBILE_PORT],
                user=d[CONF_MOBILE_USER],
                password=d[CONF_MOBILE_PASSWORD],
            )
            m.add_push_callback(self._on_push)
            m.connect()
            m.login()
            self._mobile = m
            self._epoch += 1
            self.available = True

    def _on_push(self, push) -> None:
        # Called from the mobile client's own reader thread — hop back
        # onto the HA event loop before touching any entity.
        self.hass.loop.call_soon_threadsafe(self._dispatch_push, push)

    def _dispatch_push(self, push) -> None:
        entity = self._entities.get((push.node, push.key))
        if entity is not None:
            entity.on_push_state(push.is_on, push.ratio)

    async def _async_watchdog_tick(self, _now) -> None:
        alive = self._mobile is not None and self._mobile._sock is not None
        if alive:
            return
        _LOGGER.warning("Vitrea mobile connection down, reconnecting")
        self.available = False
        try:
            await self.hass.async_add_executor_job(self._reconnect_with_backoff, self._epoch)
            await self._async_refresh_all_states()
        except Exception as err:
            _LOGGER.warning("Vitrea reconnect failed: %s", err)

    async def _async_poll_tick(self, _now) -> None:
        """Resync entity state from the box, one bulk request per node.

        Corrects drift from missed pushes. on_push_state is only called
        for entities whose state actually differs, so this is silent in
        the normal case.
        """
        if not self.available or self._mobile is None or not self._entities:
            return
        if time.monotonic() - self._last_command < POLL_QUIET_AFTER_COMMAND:
            return
        # Covers report a transient "on" while moving; only lights/
        # switches/fans are corrected here, so only poll their nodes.
        nodes = sorted({n for (n, _), e in self._entities.items() if hasattr(e, "_is_on")})
        try:
            snapshot = await self.hass.async_add_executor_job(self._poll_nodes, nodes)
        except Exception as err:
            _LOGGER.debug("Vitrea poll failed: %s", err)
            return
        for (node, key), ks in snapshot.items():
            entity = self._entities.get((node, key))
            if entity is None:
                continue
            if getattr(entity, "_is_on", ks.is_on) != ks.is_on:
                _LOGGER.debug("Vitrea poll corrected N%s-K%s to %s", node, key, ks.is_on)
                entity.on_push_state(ks.is_on, ks.ratio)

    def _poll_nodes(self, nodes: list[int]) -> dict:
        out = {}
        for node in nodes:
            for key, ks in self._node_status(node, fresh=True).items():
                out[(node, key)] = ks
            time.sleep(0.25)
        return out

    async def _async_refresh_all_states(self) -> None:
        nodes = sorted({n for n, _ in self._entities})
        self._status_cache.clear()
        for node in nodes:
            try:
                snap = await self.hass.async_add_executor_job(self._node_status, node)
            except Exception:
                continue
            for (n, key), entity in list(self._entities.items()):
                if n == node and key in snap:
                    entity.on_push_state(snap[key].is_on, snap[key].ratio)

    def _node_status(self, node: int, fresh: bool = False) -> dict:
        """{mobile key: KeyStatusPush} for a node. One request per node,
        shared for STATUS_CACHE_TTL seconds so e.g. 100 entities loading
        at once (or after a reconnect) don't each cost a request.
        """
        now = time.monotonic()
        hit = self._status_cache.get(node)
        if hit is not None and not fresh and now - hit[0] < STATUS_CACHE_TTL:
            return hit[1]
        self._last_activity = now
        snap = {ks.key: ks for ks in self._mobile.get_all_key_status(node)}
        self._status_cache[node] = (time.monotonic(), snap)
        return snap

    async def _async_keepalive_tick(self, _now) -> None:
        if not self.available or self._mobile is None:
            return
        if time.monotonic() - self._last_activity < KEEPALIVE_IDLE:
            return
        epoch = self._epoch
        try:
            await self.hass.async_add_executor_job(self._mobile.get_last_change_timestamp, KEEPALIVE_TIMEOUT)
            self._last_activity = time.monotonic()
        except Exception as err:
            _LOGGER.warning("Vitrea session unresponsive (%s), reconnecting", err)
            self.available = False
            try:
                await self.hass.async_add_executor_job(self._reconnect_with_backoff, epoch)
                await self._async_refresh_all_states()
            except Exception as err2:
                _LOGGER.warning("Vitrea reconnect failed: %s", err2)

    def _reconnect_with_backoff(self, seen_epoch: int | None = None) -> None:
        # The box can hold the old session slot for a few seconds after a
        # timeout, so an immediate re-login times out too (seen live as
        # "timeout waiting for response to opcode 1"). Back off.
        #
        # seen_epoch is the session epoch the caller saw fail. If it has
        # moved on by the time we get the lock, another thread already
        # replaced the session — logging in again would kill that fresh
        # session (seen live 2026-10-29 as a failed turn_off right after
        # a keepalive reconnect).
        with self._connect_lock:
            if seen_epoch is not None and self._epoch != seen_epoch:
                return
            for attempt in range(3):
                time.sleep(1.0 + 2.0 * attempt)
                try:
                    self._connect_mobile()
                    return
                except Exception:
                    if attempt == 2:
                        raise

    def _call(self, fn):
        self._last_activity = time.monotonic()
        epoch = self._epoch
        try:
            return fn()
        except Exception:
            self._reconnect_with_backoff(epoch)
            return fn()

    def _call_cmd(self, send):
        """Run a command with a short first-try timeout (a dead session
        should fail fast), then retry once after a reconnect with the
        client's normal patient timeout — the box is often slow to answer
        right after a fresh login.
        """
        self._last_activity = time.monotonic()
        epoch = self._epoch
        try:
            return send(COMMAND_TIMEOUT)
        except Exception:
            self._reconnect_with_backoff(epoch)
            return send(None)

    # ---------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------
    def _schedule_verify(self, node: int, key: int) -> None:
        """Re-read this key's real state once its command burst settles."""
        self._last_command = time.monotonic()
        slot = (node, self._mk(node, key))
        cancel = self._verify_cancel.pop(slot, None)
        if cancel is not None:
            cancel()

        async def _verify(_now) -> None:
            self._verify_cancel.pop(slot, None)
            entity = self._entities.get(slot)
            if entity is None:
                return
            if hasattr(entity, "_position"):
                # Cover: a missed push would leave the position stale. A
                # moving blind reports a transient is_on (ignored by the
                # entity), so this only lands once it has stopped/arrived.
                ks = await self.async_get_key_status(*slot, mobile=True)
                if ks is not None and not ks.is_on and entity._position != ks.ratio:
                    entity.on_push_state(ks.is_on, ks.ratio)
                return
            if not hasattr(entity, "_is_on"):
                return
            ks = await self.async_get_key_status(*slot, mobile=True)
            if ks is not None and entity._is_on != ks.is_on:
                entity.on_push_state(ks.is_on, ks.ratio)

        self._verify_cancel[slot] = async_call_later(self.hass, VERIFY_DELAY, _verify)

    async def async_key_on(self, node: int, key: int, ratio: int = 0, timer: int = 0) -> None:
        await self.hass.async_add_executor_job(
            self._call_cmd, lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_ON, ratio=ratio, timer=timer, timeout=t)
        )
        self._schedule_verify(node, key)

    async def async_key_off(self, node: int, key: int, timer: int = 0) -> None:
        await self.hass.async_add_executor_job(
            self._call_cmd, lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_OFF, timer=timer, timeout=t)
        )
        self._schedule_verify(node, key)

    async def async_set_cover_position(self, node: int, key: int, ratio: int) -> None:
        # Confirmed live 2026-10-25 (docs/protocol-mobile.md "Cover model
        # resolved"): any set_key_status call just sets a new absolute
        # target position via ratio; the status byte doesn't encode
        # direction. KEY_ON is used here only because set_key_status
        # needs *some* valid status byte.
        await self.hass.async_add_executor_job(
            self._call_cmd,
            lambda t: self._mobile.set_key_status(
                node, self._mk(node, key), KEY_ON, ratio=ratio, timeout=t
            ),
        )
        self._schedule_verify(node, key)

    async def async_stop_cover(self, node: int, key: int) -> None:
        # From the official app's own decompiled source (CustomBlinds.
        # doSendBlindStop): mid-travel stop is KEY_OFF with ratio=255 —
        # a sentinel outside the normal 0-100 range, distinct from a
        # real "closed" target (ratio=0). Confirmed live 2026-10-29.
        await self.hass.async_add_executor_job(
            self._call_cmd,
            lambda t: self._mobile.set_key_status(
                node, self._mk(node, key), KEY_OFF, ratio=255, timeout=t
            ),
        )
        self._schedule_verify(node, key)

    async def async_trigger_scenario(self, scenario_id: int) -> None:
        await self.hass.async_add_executor_job(self._call, lambda: self._mobile.trigger_scenario(scenario_id))

    async def async_get_key_status(self, node: int, key: int, mobile: bool = False):
        """Fetch one key's current status — used by each entity's
        async_added_to_hass so it shows the real device state on load
        instead of defaulting to off. Returns None on failure (entity
        just keeps its default state and waits for the next push).
        """
        try:
            slot = key if mobile else self._mk(node, key)
            # A verify-after-command read must be fresh; initial entity
            # loads share one request per node.
            snap = await self.hass.async_add_executor_job(
                self._call, lambda: self._node_status(node, fresh=mobile)
            )
            return snap.get(slot)
        except Exception:
            return None
