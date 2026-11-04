DOMAIN = "vitrea"

CONF_SIMPLE_PORT = "simple_port"
CONF_ADMIN_USER = "admin_user"
CONF_ADMIN_PASSWORD = "admin_password"
CONF_MOBILE_PORT = "mobile_port"
CONF_MOBILE_USER = "mobile_user"
CONF_MOBILE_PASSWORD = "mobile_password"

DEFAULT_SIMPLE_PORT = 11502
DEFAULT_ADMIN_PORT = 11501
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"
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

# Comma-separated node numbers to hide entirely (e.g. leftover example
# nodes some installs still have). Unlike excluded_keys this takes plain
# node numbers, so it works whatever naming source is in use.
CONF_EXCLUDED_NODES = "excluded_nodes"
DEFAULT_EXCLUDED_NODES = ""

# Comma-separated name patterns (* and ? wildcards, case-insensitive) of
# entities to hide, e.g. "N*-*, Spare*". Vendor default names (N35-3) are
# always hidden without any setting.
CONF_HIDDEN_NAMES = "hidden_names"
DEFAULT_HIDDEN_NAMES = ""

# On by default: keys the installer never named (empty, or the vendor default
# such as N35-3) control nothing, so they are left out.
CONF_HIDE_UNNAMED = "hide_unnamed"
DEFAULT_HIDE_UNNAMED = True

# Off by default: turning it on makes Home Assistant create an area per
# V-Box room the first time it sees each node (HA applies a device's
# suggested area only when the device is created).
CONF_USE_ROOMS_AS_AREAS = "use_rooms_as_areas"
DEFAULT_USE_ROOMS_AS_AREAS = False
