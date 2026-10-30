from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_MOBILE_PORT,
    CONF_MOBILE_USER,
    CONF_MOBILE_PASSWORD,
    CONF_DISABLE_METADATA_CACHE,
    CONF_EXCLUDED_KEYS,
    CONF_USE_ROOMS_AS_AREAS,
    DEFAULT_MOBILE_PORT,
    DEFAULT_MOBILE_USER,
    DEFAULT_DISABLE_METADATA_CACHE,
    DEFAULT_EXCLUDED_KEYS,
    DEFAULT_USE_ROOMS_AS_AREAS,
)
from ._mobile_client import MobileVBox, MobileVBoxAuthError


def _try_mobile_login(data: dict) -> None:
    """Log in once to validate credentials; always releases the session."""
    m = MobileVBox(
        host=data["host"],
        port=data[CONF_MOBILE_PORT],
        user=data[CONF_MOBILE_USER],
        password=data[CONF_MOBILE_PASSWORD],
    )
    try:
        m.connect()
        m.login()
    finally:
        m.close()


class VitreaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "VitreaOptionsFlow":
        return VitreaOptionsFlow()

    async def async_step_reauth(self, entry_data) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> FlowResult:
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            data = {**entry.data, CONF_MOBILE_PASSWORD: user_input[CONF_MOBILE_PASSWORD]}
            try:
                await self.hass.async_add_executor_job(_try_mobile_login, data)
            except MobileVBoxAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_MOBILE_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            user_input = {**user_input, "host": user_input["host"].strip()}
            try:
                await self.hass.async_add_executor_job(_try_mobile_login, user_input)
            except MobileVBoxAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input["host"].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Vitrea V-Box ({user_input['host']})",
                    data=user_input,
                )

        schema = vol.Schema({
            vol.Required("host"): str,
            vol.Required(CONF_MOBILE_PORT, default=DEFAULT_MOBILE_PORT): int,
            vol.Required(CONF_MOBILE_USER, default=DEFAULT_MOBILE_USER): str,
            vol.Required(CONF_MOBILE_PASSWORD): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class VitreaOptionsFlow(config_entries.OptionsFlow):
    """Settings reachable from the integration's Configure button.

    Metadata caching (names/types/rooms/scenarios) is on by default — the
    full scan takes minutes on a V-Box this size (see hub.py). Flipping
    the rescan toggle on skips the cache every startup, including after a
    manual "Reload", so it doubles as the "rescan now" action: turn it
    on, hit Reload, turn it back off.

    `excluded_keys` lets an install drop a specific node/key (e.g. a
    confirmed duplicate/ghost circuit) without editing code — it's
    per-install state, not something that belongs hardcoded in hub.py.
    """

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema({
            vol.Required(
                CONF_DISABLE_METADATA_CACHE,
                default=self.config_entry.options.get(
                    CONF_DISABLE_METADATA_CACHE, DEFAULT_DISABLE_METADATA_CACHE
                ),
            ): bool,
            vol.Required(
                CONF_USE_ROOMS_AS_AREAS,
                default=self.config_entry.options.get(
                    CONF_USE_ROOMS_AS_AREAS, DEFAULT_USE_ROOMS_AS_AREAS
                ),
            ): bool,
            vol.Optional(
                CONF_EXCLUDED_KEYS,
                default=self.config_entry.options.get(
                    CONF_EXCLUDED_KEYS, DEFAULT_EXCLUDED_KEYS
                ),
            ): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
