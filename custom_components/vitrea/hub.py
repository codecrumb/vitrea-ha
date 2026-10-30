from __future__ import annotations
import logging
import re
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from ._node_types import node_category
from ._mobile_client import BOILER_ON_TIMER, KEY_OFF, KEY_ON, MobileVBox, MobileVBoxConnectionError
from .const import (
    DOMAIN,
    CONF_MOBILE_PASSWORD,
    CONF_MOBILE_PORT,
    CONF_MOBILE_USER,
    CONF_DISABLE_METADATA_CACHE,
    CONF_EXCLUDED_KEYS,
    CONF_USE_ROOMS_AS_AREAS,
    DEFAULT_DISABLE_METADATA_CACHE,
    DEFAULT_EXCLUDED_KEYS,
    DEFAULT_USE_ROOMS_AS_AREAS,
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
# Measured live 2026-10-30 (HA disabled, probe session alone, clean network):
# the box drops or mutes a mobile session at random, often within ~10 s of
# login if the client stays quiet, and even a session that is pinged every
# 4 s dies after 30-100 s. Sessions that send real requests right after
# login live far longer, so a new session is verified with an immediate
# request and discarded if it is dead on arrival; pings only detect deaths.
KEEPALIVE_CHECK = timedelta(seconds=3)
KEEPALIVE_IDLE = 5.0
KEEPALIVE_TIMEOUT = 3.0
COMMAND_TIMEOUT = 3.0
LOGIN_VERIFY_TIMEOUT = 3.0
RECONNECT_DELAYS = (0.3, 1.0, 2.0, 4.0)
# When the box mutes sessions, a command used to queue behind the reconnect
# lock (up to ~40 s) and then replay late, in order — lights toggling a
# minute after the tap. Now a command lives COMMAND_MAX_AGE seconds at most,
# gets one recovery attempt, is dropped if a newer command for the same key
# was issued meanwhile, and otherwise fails with an error the user sees.
COMMAND_MAX_AGE = 10.0
# Never log in more often than this, whoever asks (command or background).
LOGIN_MIN_GAP = 5.0
# If the last few sessions died young the box is struggling: stop hammering
# it with fresh logins. The wait before a *background* reconnect grows with
# the number of young deaths in a row (a user command may still force one
# login, subject to LOGIN_MIN_GAP). A session that lives LONG_SESSION
# seconds clears the count.
YOUNG_SESSION = 30.0
LONG_SESSION = 60.0
BACKOFF_STEPS = (0.0, 0.0, 5.0, 15.0, 30.0, 60.0)
STATUS_CACHE_TTL = 5.0
METADATA_CACHE_VERSION = 2
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


class _Superseded(Exception):
    """A newer command for the same key replaced this one before it was sent."""


class VitreaHub:
    """Owns one mobile-protocol metadata snapshot and one persistent
    mobile-protocol connection (control + live push) for a single V-Box.
    """

    def __init__(self, hass: "HomeAssistant", data: dict, options: dict | None = None,
                 entry_id: str | None = None) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self._data = data
        self._use_areas = (options or {}).get(
            CONF_USE_ROOMS_AS_AREAS, DEFAULT_USE_ROOMS_AS_AREAS
        )
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
        self._session_started = time.monotonic()
        self._verify_cancel: dict[tuple[int, int], object] = {}
        self._metadata_listeners: list = []
        # Latest-wins bookkeeping: slot -> sequence number of the newest
        # command issued for it (see _run_command).
        self._cmd_seq: dict[tuple, int] = {}
        self._young_deaths = 0
        self._last_login = float("-inf")
        self._bg_reconnecting = False
        self.controller_version: str | None = None
        # Scenario enable flags (op 66), refreshed by the slow poll.
        self.scenario_enabled: dict[int, bool] = {}
        self._scenario_entities: dict[int, object] = {}

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
        try:
            self._async_apply_rooms_as_areas()
        except Exception:
            _LOGGER.exception("Vitrea: assigning rooms as areas failed")
        self.metadata_ready = True
        for callback in self._metadata_listeners:
            try:
                callback()
            except Exception:
                _LOGGER.exception("Vitrea metadata listener failed")
        self._metadata_listeners.clear()

    def _async_apply_rooms_as_areas(self) -> None:
        """With the "rooms as areas" option on, put each node's device in the
        area named after its V-Box room, creating the area only if no area
        of that name exists (HA matches names case-insensitively). Works on
        devices that already exist, but never overrides an area the user
        has set.
        """
        if not (self._use_areas and self.entry_id):
            return
        from homeassistant.helpers import area_registry as ar, device_registry as dr

        areas, devices = ar.async_get(self.hass), dr.async_get(self.hass)
        rooms: dict[int, str] = {}
        for k in self.keys:
            if k.get("room"):
                rooms.setdefault(k["node"], k["room"])
        for node, room in rooms.items():
            dev = devices.async_get_or_create(
                config_entry_id=self.entry_id, **self.device_info(node, None)
            )
            if dev.area_id:
                continue
            area = areas.async_get_area_by_name(room) or areas.async_create(room)
            devices.async_update_device(dev.id, area_id=area.id)

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
        moved = {eid for eid, _ in moves}
        # Never delete anything: an entity outside this migration that already
        # owns a target ID makes that one move impossible, so skip it.
        owners = {e.unique_id: e.entity_id for e in reg.entities.values() if e.platform == DOMAIN}
        blocked = [(eid, new) for eid, new in moves if owners.get(new) not in (None, eid) and owners[new] not in moved]
        for eid, new in blocked:
            _LOGGER.warning("Vitrea: not migrating %s, unique id %s is taken by %s", eid, new, owners[new])
        moves = [m for m in moves if m not in blocked]
        if not moves:
            return
        for eid, _ in moves:
            reg.async_update_entity(eid, new_unique_id=f"vitrea_migrating_{eid}")
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

                # Boiler keys are timed switches (CustomBoilers in the app):
                # "on" must carry timer 0xFFFF. Category 15 is the admin
                # enum's Boiler; type 21 is the app's NODE_KEY_BOILER.
                boiler = cat_id == 15 or kp.get("type") == 21
                if boiler:
                    ha_type = "switch"

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
                    "boiler": boiler,
                    # Recorded, not used for classification yet: the
                    # app's ENodeKeyType (2 toggle, 3 blind, 13 toggle
                    # sense, 20 dimmer, 21 boiler, 23 dual pole) should
                    # match this byte, but that is unverified live.
                    "key_type": kp.get("type"),
                    "category": cat_id,
                    "room": room_names.get(kp["room_no"] or info["room_no"], ""),
                })

        counts: dict[tuple, int] = {}
        for k in keys:
            counts[(k["key_type"], k["category"], k["ha_type"])] = counts.get((k["key_type"], k["category"], k["ha_type"]), 0) + 1
        _LOGGER.info(
            "Vitrea key (type, category, platform) -> count: %s",
            ", ".join(f"{t}/{c}/{p}={n}" for (t, c, p), n in sorted(counts.items(), key=str)),
        )

        scenarios: list[dict] = []
        try:
            numbers = list(paced("get_scenario_numbers"))
        except Exception as err:
            _LOGGER.warning("Vitrea: scenario list unavailable: %s", err)
            numbers = []
        # Opcode 66 is the list behind the app's Enable/Disable screen. The
        # regular list can omit scenarios (seen live: 37/38/39/42), so take
        # the union. attempts=1: a NACK here must not trigger re-logins.
        try:
            statuses = paced("get_scenario_statuses", attempts=1)
        except Exception as err:
            _LOGGER.info("Vitrea: scenario status list unavailable: %s", err)
            statuses = {}
        for sno in sorted(set(numbers) | set(statuses)):
            try:
                scenarios.append({
                    "id": sno,
                    "name": paced("get_scenario_name", sno),
                    "listed": sno in numbers,
                    "has_status": sno in statuses,
                })
            except Exception:
                _LOGGER.warning("Vitrea: scenario %s name unavailable", sno)
        extra = [s["id"] for s in scenarios if not s["listed"]]
        if extra:
            _LOGGER.info("Vitrea: scenarios %s are not in the regular list but exist (op 66)", extra)
        self.scenario_enabled.update(statuses)
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
            self._last_login = time.monotonic()
            try:
                m.connect()
                m.login()
                # Dead-on-arrival check: a session that answers nothing right
                # after login is useless, so fail the connect and let the
                # caller retry rather than trusting it. The official app
                # follows login with version -> super-user -> timestamp
                # before anything else; do the same.
                m.handshake(timeout=LOGIN_VERIFY_TIMEOUT)
            except Exception:
                # Don't leak a half-open session: the box allows one per
                # account and may keep treating the account as logged in.
                try:
                    m.close()
                except Exception:
                    pass
                raise
            self._mobile = m
            self._epoch += 1
            self._session_started = time.monotonic()
            self.controller_version = getattr(m, "controller_version", None) or self.controller_version
            self.available = True

    def _on_push(self, push) -> None:
        # Called from the mobile client's own reader thread — hop back
        # onto the HA event loop before touching any entity.
        self.hass.loop.call_soon_threadsafe(self._dispatch_push, push)

    def _dispatch_push(self, push) -> None:
        self._apply_state(self._entities.get((push.node, push.key)), push)

    @staticmethod
    def _apply_state(entity, ks) -> None:
        # An entity is registered in __init__ but only gets .hass once HA
        # adds it; writing state before then raises ("Attribute hass is
        # None"). async_added_to_hass reads its own initial state.
        if entity is None or getattr(entity, "hass", None) is None:
            return
        try:
            entity.on_push_state(ks.is_on, ks.ratio)
        except Exception:
            _LOGGER.exception("Vitrea: applying state to %s failed", getattr(entity, "entity_id", entity))

    async def _async_watchdog_tick(self, _now) -> None:
        alive = self._mobile is not None and self._mobile._sock is not None
        if alive or self._bg_reconnecting:
            return
        _LOGGER.warning("Vitrea mobile connection down, reconnecting")
        self.available = False
        await self._async_background_reconnect(self._epoch)

    async def _async_background_reconnect(self, epoch: int) -> bool:
        """Replace a dead session when no user is waiting on it.

        Single flight (the 3 s ticks must not stack up threads while a
        backoff is running), and it waits out the young-death backoff.
        """
        if self._bg_reconnecting:
            return False
        self._bg_reconnecting = True
        try:
            await self.hass.async_add_executor_job(self._background_reconnect, epoch)
        except Exception as err:
            self.available = False
            _LOGGER.warning("Vitrea reconnect failed: %s", err)
            return False
        finally:
            self._bg_reconnecting = False
        await self._async_refresh_all_states()
        return True

    def _background_reconnect(self, seen_epoch: int) -> None:
        self._backoff_wait(seen_epoch)
        self._reconnect_with_backoff(seen_epoch)

    def _backoff_wait(self, seen_epoch: int | None) -> None:
        """Sleep before a background login when recent sessions died young.

        Polls the epoch so that a user command which forced its own
        reconnect meanwhile ends the wait early.
        """
        wait = BACKOFF_STEPS[min(self._young_deaths, len(BACKOFF_STEPS) - 1)]
        if not wait:
            return
        _LOGGER.warning(
            "Vitrea: %d sessions in a row died young, waiting %.0fs before the next login",
            self._young_deaths, wait,
        )
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if seen_epoch is not None and self._epoch != seen_epoch:
                return
            time.sleep(0.25)

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
        await self.async_refresh_scenario_enabled()
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
                    self._apply_state(entity, snap[key])

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
        if self._mobile is None:
            return
        age = time.monotonic() - self._session_started
        if self._young_deaths and age >= LONG_SESSION:
            self._young_deaths = 0
        if not self.available or self._bg_reconnecting:
            return
        if time.monotonic() - self._last_activity < KEEPALIVE_IDLE:
            return
        epoch = self._epoch
        try:
            await self.hass.async_add_executor_job(self._mobile.get_last_change_timestamp, KEEPALIVE_TIMEOUT)
            self._last_activity = time.monotonic()
        except Exception as err:
            _LOGGER.warning(
                "Vitrea session unresponsive after %.0fs (%s), reconnecting",
                age, err,
            )
            await self._async_background_reconnect(epoch)

    def _reconnect_with_backoff(self, seen_epoch: int | None = None, deadline: float | None = None) -> None:
        # The box can hold the old session slot for a few seconds after a
        # timeout, so an immediate re-login times out too (seen live as
        # "timeout waiting for response to opcode 1"). Back off, and never
        # log in more often than LOGIN_MIN_GAP.
        #
        # seen_epoch is the session epoch the caller saw fail. If it has
        # moved on by the time we get the lock, another thread already
        # replaced the session — logging in again would kill that fresh
        # session (seen live 2026-10-29 as a failed turn_off right after
        # a keepalive reconnect).
        #
        # deadline (a monotonic time) is set for a user command: it must
        # not sit behind the lock or the retry schedule past its lifetime,
        # so it raises TimeoutError instead of waiting.
        if not self._connect_lock.acquire(timeout=-1 if deadline is None else max(0.0, deadline - time.monotonic())):
            raise TimeoutError("a reconnect is already in progress")
        try:
            if seen_epoch is not None and self._epoch != seen_epoch:
                # Another thread already replaced the session. The caller
                # may have marked us unavailable before that happened;
                # undo it, or keepalive/poll stay disabled and the next
                # refresh writes every entity as unavailable (seen live
                # 2026-10-29: 224 entities at once).
                self.available = True
                return
            if time.monotonic() - self._session_started < YOUNG_SESSION:
                self._young_deaths += 1
            for attempt, delay in enumerate(RECONNECT_DELAYS):
                wait = max(delay, LOGIN_MIN_GAP - (time.monotonic() - self._last_login))
                if deadline is not None and time.monotonic() + wait > deadline:
                    raise TimeoutError("the V-Box is not accepting logins right now")
                time.sleep(wait)
                try:
                    self._connect_mobile()
                    return
                except Exception:
                    if attempt == len(RECONNECT_DELAYS) - 1:
                        self._young_deaths += 1
                        raise
        finally:
            self._connect_lock.release()

    def _call(self, fn):
        """Run a read. Never reconnects: a dead session is replaced by the
        keepalive/watchdog, and the state refresh that follows covers
        whatever this read missed.
        """
        self._last_activity = time.monotonic()
        return fn()

    def _run_command(self, slot: tuple, seq: int, issued: float, send):
        """Send one command, with a single recovery attempt.

        Fail fast: the command is dropped (_Superseded) if a newer command
        for the same slot exists, and gives up (TimeoutError) once it is
        COMMAND_MAX_AGE old, so a muted box can never replay it late.
        A NACK means the box answered, so only a dead/mute session
        (timeout, closed socket) justifies the reconnect.
        """
        deadline = issued + COMMAND_MAX_AGE

        def check() -> None:
            if self._cmd_seq.get(slot) != seq:
                raise _Superseded
            if time.monotonic() > deadline:
                raise TimeoutError("the command expired before the V-Box answered")

        self._last_activity = time.monotonic()
        epoch = self._epoch
        check()
        try:
            return send(COMMAND_TIMEOUT)
        except (MobileVBoxConnectionError, OSError):
            pass
        check()
        self._reconnect_with_backoff(epoch, deadline)
        check()
        return send(max(0.5, min(COMMAND_TIMEOUT, deadline - time.monotonic())))

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

    async def _async_command(self, slot: tuple, send, verify: tuple[int, int] | None = None) -> None:
        """Run `send(timeout)` on the executor as the newest command for
        `slot` (see _run_command). A superseded command is silently
        dropped; any other failure reaches the user as a HomeAssistantError.
        """
        seq = self._cmd_seq.get(slot, 0) + 1
        self._cmd_seq[slot] = seq
        issued = time.monotonic()
        try:
            await self.hass.async_add_executor_job(self._run_command, slot, seq, issued, send)
        except _Superseded:
            return
        except Exception as err:
            raise HomeAssistantError(f"Vitrea V-Box command failed: {err}") from err
        if verify is not None:
            self._schedule_verify(*verify)

    async def async_key_on(self, node: int, key: int, ratio: int = 0, timer: int = 0) -> None:
        await self._async_command(
            (node, self._mk(node, key)),
            lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_ON, ratio=ratio, timer=timer, timeout=t),
            verify=(node, key),
        )

    async def async_key_off(self, node: int, key: int, timer: int = 0) -> None:
        await self._async_command(
            (node, self._mk(node, key)),
            lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_OFF, timer=timer, timeout=t),
            verify=(node, key),
        )

    async def async_boiler_on(self, node: int, key: int) -> None:
        # CustomBoilers.changeState: on = status ON with timer 0xFFFF
        # ("until switched off"); the timer is seconds for a boiler key.
        # Unverified — no boiler circuits in the reference install.
        await self.async_key_on(node, key, timer=BOILER_ON_TIMER)

    async def async_set_cover_position(self, node: int, key: int, ratio: int) -> None:
        # Confirmed live 2026-10-25 (docs/protocol-mobile.md "Cover model
        # resolved"): any set_key_status call just sets a new absolute
        # target position via ratio; the status byte doesn't encode
        # direction. KEY_ON is used here only because set_key_status
        # needs *some* valid status byte.
        await self._async_command(
            (node, self._mk(node, key)),
            lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_ON, ratio=ratio, timeout=t),
            verify=(node, key),
        )

    async def async_stop_cover(self, node: int, key: int) -> None:
        # From the official app's own decompiled source (CustomBlinds.
        # doSendBlindStop): mid-travel stop is KEY_OFF with ratio=255 —
        # a sentinel outside the normal 0-100 range, distinct from a
        # real "closed" target (ratio=0). Confirmed live 2026-10-29.
        await self._async_command(
            (node, self._mk(node, key)),
            lambda t: self._mobile.set_key_status(node, self._mk(node, key), KEY_OFF, ratio=255, timeout=t),
            verify=(node, key),
        )

    async def async_trigger_scenario(self, scenario_id: int) -> None:
        await self._async_command(
            ("scenario", scenario_id),
            lambda t: self._mobile.trigger_scenario(scenario_id, timeout=t),
        )

    def device_info(self, node: int, room: str | None = None):
        """Device registry entry for a V-Box node (a wall unit / module),
        parented to the V-Box itself. None when the hub has no config
        entry id (offline tests).
        """
        if self.entry_id is None:
            return None
        from homeassistant.helpers.device_registry import DeviceInfo

        info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_node_{node}")},
            name=f"V-Box node {node}",
            manufacturer="Vitrea",
            # No via_device: recent HA versions reject it when entities
            # create the device (seen live on 2026.10), which stopped every
            # entity from being added.
        )
        if self._use_areas and room:
            info["suggested_area"] = room
        return info

    def scenes_device_info(self):
        """One device for all scenes and scenario switches, so they are not
        left ungrouped. None when the hub has no config entry id."""
        if self.entry_id is None:
            return None
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}_scenes")},
            name="Vitrea Scenes",
            manufacturer="Vitrea",
        )

    def hub_device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo

        return DeviceInfo(
            identifiers={(DOMAIN, self.entry_id)},
            name="Vitrea V-Box",
            manufacturer="Vitrea",
            model="V-Box",
            sw_version=self.controller_version,
        )

    # ---------------------------------------------------------------
    # Scenario enable flags (opcode 66)
    # ---------------------------------------------------------------
    def register_scenario_entity(self, scenario_id: int, entity) -> None:
        self._scenario_entities[scenario_id] = entity

    async def async_refresh_scenario_enabled(self) -> None:
        """Read every scenario's enabled flag (one request) and push the
        values to the scenario switches that exist.
        """
        if not self._scenario_entities or not self.available or self._mobile is None:
            return
        try:
            statuses = await self.hass.async_add_executor_job(
                self._call, self._mobile.get_scenario_statuses
            )
        except Exception as err:
            _LOGGER.debug("Vitrea scenario status read failed: %s", err)
            return
        self.scenario_enabled.update(statuses)
        for sid, entity in self._scenario_entities.items():
            if sid in statuses and getattr(entity, "hass", None) is not None:
                entity.on_enabled(statuses[sid])

    async def async_set_scenario_enabled(self, scenario_id: int, enabled: bool) -> None:
        await self._async_command(
            ("scenario-enabled", scenario_id),
            lambda t: self._mobile.set_scenario_enabled(scenario_id, enabled, timeout=t),
        )
        self.scenario_enabled[scenario_id] = enabled

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
