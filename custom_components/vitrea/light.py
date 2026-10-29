from __future__ import annotations
import math
from typing import Any
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
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
        async_add_entities(VitreaLight(hub, k) for k in hub.keys if k["ha_type"] == "light")

    hub.add_metadata_listener(_add)


class VitreaLight(LightEntity):
    _attr_should_poll = False

    def __init__(self, hub: VitreaHub, key: dict) -> None:
        self._hub = hub
        self._node = key["node"]
        self._key = key["key"]
        self._dimmable = key.get("dimmable", False)
        self._attr_name = key["name"]
        self._attr_unique_id = f"vitrea_{self._node}_{self._key}"
        self._is_on = False
        self._ratio = 0
        if self._dimmable:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        hub.register_entity(self._node, self._key, self)

    async def async_added_to_hass(self) -> None:
        ks = await self._hub.async_get_key_status(self._node, self._key)
        if ks is not None:
            self._is_on = ks.is_on
            self._ratio = ks.ratio

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int | None:
        if not self._dimmable:
            return None
        return round(max(1, self._ratio) / 100 * 255)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._dimmable:
            if ATTR_BRIGHTNESS in kwargs:
                ratio = math.ceil(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            else:
                # No explicit brightness: 254 is the official app's own
                # "restore last/default level" sentinel for a bare
                # toggle-on (CustomDimmers.changeState, decompiled
                # com.vitrea.v7) — not plain 100%. Unverified against
                # real dimmer hardware; see README's dimmer note.
                ratio = 254
        else:
            ratio = 0
        await self._hub.async_key_on(self._node, self._key, ratio=ratio)
        # No optimistic state write here — on_push_state is the single
        # source of truth. Confirmed live 2026-10-25: writing optimistic
        # state let a late push from an earlier command silently
        # overwrite a correct state on rapid on/off toggling.

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_key_off(self._node, self._key)

    def on_push_state(self, is_on: bool, ratio: int) -> None:
        self._is_on = is_on
        self._ratio = ratio
        self.async_write_ha_state()
