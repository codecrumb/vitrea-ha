from __future__ import annotations
import ipaddress
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_ADMIN_PASSWORD,
    CONF_ADMIN_USER,
    CONF_MOBILE_PORT,
    CONF_MOBILE_USER,
    CONF_MOBILE_PASSWORD,
    CONF_DISABLE_METADATA_CACHE,
    CONF_EXCLUDED_KEYS,
    CONF_USE_ROOMS_AS_AREAS,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USER,
    DEFAULT_MOBILE_PORT,
    DEFAULT_MOBILE_USER,
    DEFAULT_DISABLE_METADATA_CACHE,
    DEFAULT_EXCLUDED_KEYS,
    DEFAULT_USE_ROOMS_AS_AREAS,
)
from ._mobile_client import MobileVBox, MobileVBoxAuthError
from ._simple_client import DEFAULT_PORT as SIMPLE_PORT, probe, scan

_LOGGER = logging.getLogger(__name__)


def _try_mobile_login(data: dict) -> None:
    """Log in once to validate credentials; always releases the session."""
    m = MobileVBox(
        host=data["host"],
        port=data.get(CONF_MOBILE_PORT) or DEFAULT_MOBILE_PORT,
        user=data.get(CONF_MOBILE_USER) or DEFAULT_MOBILE_USER,
        password=data[CONF_MOBILE_PASSWORD],
    )
    try:
        m.connect()
        m.login()
    finally:
        m.close()


async def _async_local_networks(hass) -> list[str]:
    """IPv4 subnets of this machine's enabled adapters, at most a /24 each
    (scanning anything bigger would take too long)."""
    from homeassistant.components import network

    nets: list[str] = []
    for adapter in await network.async_get_adapters(hass):
        if not adapter.get("enabled"):
            continue
        for addr in adapter.get("ipv4", []):
            ip = ipaddress.ip_address(addr["address"])
            if ip.is_loopback or ip.is_link_local:
                continue
            net = str(ipaddress.ip_network(f"{ip}/{max(addr['network_prefix'], 24)}", strict=False))
            if net not in nets:
                nets.append(net)
    return nets


async def _async_scan_for_vboxes(hass) -> list[str]:
    found: list[str] = []
    for net in await _async_local_networks(hass):
        for host in await scan(net, SIMPLE_PORT):
            if host not in found:
                found.append(host)
    return found


class VitreaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: list[str] = []
        self._pending: dict = {}

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
                # A password set in the options form would still win over
                # the corrected one, so drop it.
                options = {k: v for k, v in entry.options.items() if k != CONF_MOBILE_PASSWORD}
                return self.async_update_reload_and_abort(entry, data=data, options=options)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_MOBILE_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get("host") or "").strip()
            if not host:
                self._found = await _async_scan_for_vboxes(self.hass)
                if not self._found:
                    errors["base"] = "no_vbox_found"
                elif len(self._found) == 1:
                    host = self._found[0]
                else:
                    self._pending = user_input
                    return await self.async_step_pick()
            if host:
                result = await self._async_finish(host, user_input, errors)
                if result is not None:
                    return result

        defaults = user_input or {}
        schema = vol.Schema({
            vol.Optional("host", default=defaults.get("host", "")): str,
            vol.Optional(CONF_MOBILE_USER, default=defaults.get(CONF_MOBILE_USER, DEFAULT_MOBILE_USER)): str,
            vol.Optional(CONF_MOBILE_PASSWORD, default=defaults.get(CONF_MOBILE_PASSWORD, "")): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_pick(self, user_input=None) -> FlowResult:
        """Several V-Boxes answered the scan: choose one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_finish(user_input["host"], self._pending, errors)
            if result is not None:
                return result
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema({vol.Required("host"): vol.In(self._found)}),
            errors=errors,
        )

    async def _async_finish(self, host: str, user_input: dict, errors: dict) -> FlowResult | None:
        """Validate `host` (and the optional mobile login), then create the
        entry. Returns None and fills `errors` when something is wrong."""
        if not await probe(host, SIMPLE_PORT, timeout=3.0):
            errors["base"] = "cannot_connect"
            return None
        data = {
            "host": host,
            CONF_MOBILE_PORT: DEFAULT_MOBILE_PORT,
            CONF_MOBILE_USER: user_input.get(CONF_MOBILE_USER) or DEFAULT_MOBILE_USER,
            CONF_MOBILE_PASSWORD: user_input.get(CONF_MOBILE_PASSWORD) or "",
        }
        if data[CONF_MOBILE_PASSWORD]:
            try:
                await self.hass.async_add_executor_job(_try_mobile_login, data)
            except MobileVBoxAuthError:
                errors["base"] = "invalid_auth"
                return None
            except Exception:
                errors["base"] = "cannot_connect_mobile"
                return None
        await self.async_set_unique_id(host.lower())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Vitrea V-Box ({host})", data=data)


class VitreaOptionsFlow(config_entries.OptionsFlow):
    """Settings reachable from the integration's Configure button.

    The device list (and, with mobile credentials, the names/rooms/scenes)
    is cached because reading it takes minutes on a big V-Box. Flipping
    the rescan toggle on skips the cache every startup, including after a
    manual "Reload", so it doubles as the "rescan now" action: turn it
    on, hit Reload, turn it back off.

    `excluded_keys` lets an install drop a specific node/key (e.g. a
    confirmed duplicate/ghost circuit) without editing code — it's
    per-install state, not something that belongs hardcoded in hub.py.

    The admin fields are only needed if your V-Box's admin password is not
    the vendor default (admin): the integration tries the default by itself
    to read the exact device list once, and falls back if it is refused or
    the admin session is busy (for example with Control4).

    The mobile fields are a fallback source of names (and give the scenario
    enable switches); leave the password empty to skip them.
    """

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        data = self.config_entry.data
        if user_input is not None:
            password = user_input.get(CONF_MOBILE_PASSWORD) or ""
            if password:
                try:
                    await self.hass.async_add_executor_job(
                        _try_mobile_login,
                        {
                            "host": data["host"],
                            CONF_MOBILE_PORT: data.get(CONF_MOBILE_PORT),
                            CONF_MOBILE_USER: user_input.get(CONF_MOBILE_USER),
                            CONF_MOBILE_PASSWORD: password,
                        },
                    )
                except MobileVBoxAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:
                    errors["base"] = "cannot_connect_mobile"
            if not errors:
                return self.async_create_entry(data=user_input)
        cur_user = opts.get(CONF_MOBILE_USER) or data.get(CONF_MOBILE_USER) or DEFAULT_MOBILE_USER
        cur_pw = opts.get(CONF_MOBILE_PASSWORD) or data.get(CONF_MOBILE_PASSWORD) or ""
        schema = vol.Schema({
            vol.Optional(
                CONF_ADMIN_USER,
                default=opts.get(CONF_ADMIN_USER) or DEFAULT_ADMIN_USER,
            ): str,
            vol.Optional(
                CONF_ADMIN_PASSWORD,
                default=opts.get(CONF_ADMIN_PASSWORD) or DEFAULT_ADMIN_PASSWORD,
            ): str,
            vol.Optional(CONF_MOBILE_USER, default=cur_user): str,
            vol.Optional(CONF_MOBILE_PASSWORD, default=cur_pw): str,
            vol.Required(
                CONF_DISABLE_METADATA_CACHE,
                default=opts.get(CONF_DISABLE_METADATA_CACHE, DEFAULT_DISABLE_METADATA_CACHE),
            ): bool,
            vol.Required(
                CONF_USE_ROOMS_AS_AREAS,
                default=opts.get(CONF_USE_ROOMS_AS_AREAS, DEFAULT_USE_ROOMS_AS_AREAS),
            ): bool,
            vol.Optional(
                CONF_EXCLUDED_KEYS,
                default=opts.get(CONF_EXCLUDED_KEYS, DEFAULT_EXCLUDED_KEYS),
            ): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
