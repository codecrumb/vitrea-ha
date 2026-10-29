from __future__ import annotations
from typing import Any
from homeassistant.components.cover import ATTR_POSITION, CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .hub import VitreaHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub: VitreaHub = hass.data[DOMAIN][entry.entry_id]

    def _add() -> None:
        async_add_entities(VitreaCover(hub, k) for k in hub.keys if k["ha_type"] == "cover")

    hub.add_metadata_listener(_add)


class VitreaCover(CoverEntity):
    """Continuous absolute-position blind (confirmed live 2026-10-25
    against one blind key): `ratio` is an absolute target position
    (0=closed, 100=open) the motor drives toward; the status byte
    doesn't encode direction. stop_cover sends KEY_OFF with ratio=255
    (a sentinel outside the normal 0-100 range), taken from the
    official app's own decompiled CustomBlinds.doSendBlindStop and
    confirmed live 2026-10-29.
    """

    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )

    def __init__(self, hub: VitreaHub, key: dict) -> None:
        self._hub = hub
        self._node = key["node"]
        self._key = key["key"]
        self._attr_name = key["name"]
        self._attr_unique_id = f"vitrea_{self._node}_{self._key}"
        self._attr_device_info = hub.device_info(self._node, key.get("room"))
        self._position: int | None = None
        hub.register_entity(self._node, self._key, self)

    async def async_added_to_hass(self) -> None:
        ks = await self._hub.async_get_key_status(self._node, self._key)
        if ks is not None:
            self.on_push_state(ks.is_on, ks.ratio)

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def is_closed(self) -> bool | None:
        if self._position is None:
            return None
        return self._position <= 0

    @property
    def current_cover_position(self) -> int | None:
        return self._position

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._hub.async_set_cover_position(self._node, self._key, 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._hub.async_set_cover_position(self._node, self._key, 0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = kwargs[ATTR_POSITION]
        await self._hub.async_set_cover_position(self._node, self._key, position)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._hub.async_stop_cover(self._node, self._key)

    def on_push_state(self, is_on: bool, ratio: int) -> None:
        # While the motor is traveling, is_on=True and ratio reads back
        # as 0 (transient, not a real position) — ignore until it
        # reports arrival (is_on=False) with the real resting ratio.
        if is_on:
            return
        self._position = ratio
        self.async_write_ha_state()
