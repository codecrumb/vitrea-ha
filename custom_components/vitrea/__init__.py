from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .hub import VitreaHub

PLATFORMS = [Platform.LIGHT, Platform.SWITCH, Platform.COVER, Platform.FAN, Platform.SCENE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = VitreaHub(hass, entry.data, entry.options)
    try:
        await hub.async_setup()
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot reach Vitrea V-Box: {err}") from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Options (e.g. the metadata-cache toggle) only take effect on a fresh
    # setup, so saving the options form reloads the entry for you.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub: VitreaHub = hass.data[DOMAIN].pop(entry.entry_id)
    await hub.async_unload()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
