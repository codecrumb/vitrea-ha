DOMAIN = "vitrea"

CONF_MOBILE_PORT = "mobile_port"
CONF_MOBILE_USER = "mobile_user"
CONF_MOBILE_PASSWORD = "mobile_password"

DEFAULT_MOBILE_PORT = 11503
DEFAULT_MOBILE_USER = "vitrea2"

# Options (not config data): changing these reloads the entry.
CONF_DISABLE_METADATA_CACHE = "disable_metadata_cache"
DEFAULT_DISABLE_METADATA_CACHE = False

# Comma-separated "node:key" pairs (0-indexed, mobile slot numbering) to
# skip when building entities — e.g. a confirmed duplicate/ghost key on
# your own install. Per-install, so it belongs in options, not in code.
CONF_EXCLUDED_KEYS = "excluded_keys"
DEFAULT_EXCLUDED_KEYS = ""

# Off by default: turning it on makes Home Assistant create an area per
# V-Box room the first time it sees each node (HA applies a device's
# suggested area only when the device is created).
CONF_USE_ROOMS_AS_AREAS = "use_rooms_as_areas"
DEFAULT_USE_ROOMS_AS_AREAS = False
