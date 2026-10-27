from __future__ import annotations
from homeassistant.components.scene import Scene
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
        async_add_entities(VitreaScene(hub, s) for s in hub.scenarios)

    hub.add_metadata_listener(_add)


class VitreaScene(Scene):
    def __init__(self, hub: VitreaHub, scenario: dict) -> None:
        self._hub = hub
        self._scenario_id = scenario["id"]
        # "Scene: " prefix avoids name collisions with light/switch/cover
        # entities — confirmed live 2026-10-25: a scenario named the same
        # as a physical key collided with that key's entity, and the user
        # kept landing on the (non-functional feeling, "Activate"-only)
        # scene by mistake.
        self._attr_name = f"Scene: {scenario['name']}"
        self._attr_unique_id = f"vitrea_scene_{self._scenario_id}"

    async def async_activate(self, **kwargs) -> None:
        await self._hub.async_trigger_scenario(self._scenario_id)
