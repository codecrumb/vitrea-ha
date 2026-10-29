from __future__ import annotations
from typing import Any
from homeassistant.components.fan import FanEntity, FanEntityFeature
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
        async_add_entities(VitreaFan(hub, k) for k in hub.keys if k["ha_type"] == "fan")

    hub.add_metadata_listener(_add)


class VitreaFan(FanEntity):
    """Plain on/off relay fan — these circuits have no speed control."""

    _attr_should_poll = False
    # HA requires these explicitly as of the 2024.8+ fan entity schema —
    # without them, fan.turn_on/fan.turn_off are rejected even though
    # async_turn_on/async_turn_off are implemented below. Confirmed live
    # 2026-10-25 ("Entity fan.<name> does not support action fan.turn_on"
    # before this was added).
    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(self, hub: VitreaHub, key: dict) -> None:
        self._hub = hub
        self._node = key["node"]
        self._key = key["key"]
        self._attr_name = key["name"]
        self._attr_unique_id = f"vitrea_{self._node}_{self._key}"
        self._attr_device_info = hub.device_info(self._node, key.get("room"))
        self._is_on = False
        hub.register_entity(self._node, self._key, self)

    async def async_added_to_hass(self) -> None:
        ks = await self._hub.async_get_key_status(self._node, self._key)
        if ks is not None:
            self._is_on = ks.is_on

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_key_on(self._node, self._key)
        # No optimistic state write — see light.py for why.

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_key_off(self._node, self._key)

    def on_push_state(self, is_on: bool, ratio: int) -> None:
        self._is_on = is_on
        self.async_write_ha_state()
