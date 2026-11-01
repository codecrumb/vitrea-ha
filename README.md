# Vitrea V-Box for Home Assistant

> **If you can use [Vitrea's official Home Assistant integration](https://github.com/vitrea-sh/hass-vitrea), use that instead.** This project exists specifically for the case where you *can't* — see below.

A Home Assistant custom integration for the Vitrea V-Box smart-home
coordinator. It needs nothing but the V-Box's IP address (or not even
that: it can find the box on your network), and imports your lights,
switches and blinds automatically.

## Why this exists

Vitrea's own official integration talks to the V-Box over the *admin*
protocol — the same channel used by Control4 and the installer tools.
If your V-Box already has Control4 (or anything else) occupying that
admin session, the official integration doesn't work alongside it.

This integration uses two other interfaces of the V-Box, neither of
which touches the admin session:

- the **simple protocol** (port 11502, the box's interface for external
  systems): no login, fast, used for all control and live state;
- optionally the **mobile protocol** (port 11503, the phone app's
  interface, accounts `vitrea1`-`vitrea8`), used only to read names,
  rooms and scenes in a short login that is closed again.

In the author's setup (one install, with Control4 running on the admin
channel) these coexist; other installs are untested. Use this only if
that's your situation — otherwise the official integration is better
supported and more complete.

## What it supports

- **Lights** (on/off and dimmable — see note below)
- **Switches** (including boilers — unverified)
- **Fans** (on/off relays, no speed control)
- **Covers / blinds** (continuous absolute position, with mid-travel stop)
- **Scenes**, plus **scenario enable/disable switches** (hidden by
  default) — *with mobile credentials only*
- Devices: one per V-Box node, plus a "Vitrea V-Box" device and a "Vitrea Scenes" device
- Live push updates, commands acknowledged in ~10 ms, automatic
  reconnect, and a slow safety-net poll (one node per minute) to correct
  any missed push
- A device-list cache so a restart doesn't re-scan your box (see Options)

### With and without mobile credentials

| | No credentials (just the IP) | With mobile credentials |
|---|---|---|
| Lights, switches, blinds, fans | yes | yes |
| Names | `Node 4 key 1` | your names from the V-Box |
| Rooms / areas | no | yes (optional) |
| Light vs switch vs fan | everything on/off is a switch (use HA's "Show as: Light") | correct types |
| Scenes and scenario switches | no | yes |

The simple protocol has no names, rooms or scenario list, which is why
the mobile login is needed for those. Everything else works without it.

**Dimmers are implemented but unverified against real hardware** — the
author's own installation has no dimmer-category circuits. **If you have
one, your testing would genuinely help**: please open an issue either way
(works great, or something's off) so this note can eventually come down.

## Installing

### Via HACS (recommended)

1. HACS → the ⋮ menu → **Custom repositories** → add
   `https://github.com/codecrumb/vitrea-ha`, category **Integration**.
2. Install **Vitrea V-Box** from HACS, then restart Home Assistant.

### Manually

Download the latest release's zip (or just the `custom_components/vitrea`
folder from this repo) and copy it to
`<config>/custom_components/vitrea/`, then restart Home Assistant.

## Setting it up

**Settings → Devices & Services → Add Integration → Vitrea V-Box**:

- **Host** *(optional)* — your V-Box's IP address. Leave it empty and
  the integration scans your local network for a V-Box (if it finds
  several you pick one).
- **Mobile user / password** *(optional)* — for names, rooms and scenes.
  Use one of `vitrea1`-`vitrea8`, in a slot that no phone is using (if
  you're not sure, try `vitrea2` and leave `vitrea1` free). You can add
  these later under **Configure**.

The first start discovers your devices, which takes a few minutes on a
large install (the box answers about two keys a second). Entities
appear when it finishes; later starts use the cache.

### Options

Reachable from the integration's **Configure** button:

- **Mobile user / password** — add or change credentials after setup.
- **Rescan device on every startup** (off by default) — skips the
  cache. Turn it on, click **Reload**, then turn it back off to force a
  one-off fresh scan (e.g. after renaming something on the box).
- **Use V-Box rooms as areas** (off by default; needs credentials) —
  puts each node's device in the area named after its V-Box room,
  reusing an existing area of the same name and creating one otherwise.
  Never overrides an area you set.
- **Excluded keys** — comma-separated `node:key` pairs (0-indexed) to
  skip when building entities, for any key that turns out to be a
  duplicate/ghost circuit on your install.

## Troubleshooting

**Enable debug logging first** - it makes every report far easier to
diagnose. In `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.vitrea: debug
```

### "Could not reach the V-Box" when adding the integration

- Check the V-Box IP and that port `11502` is reachable from Home
  Assistant (same VLAN, or a firewall rule allowing it).
- If you left the host empty and nothing was found, enter the IP.
  The scan only covers the network Home Assistant itself is on (up to a
  /24 per adapter).

### The mobile login fails

Each `vitrea1`-`vitrea8` account allows one session at a time, so if the
phone app is logged in as the same account, give Home Assistant its own.
A wrong password is reported as such. Without credentials the
integration still works, with generic names.

### Everything shows as a switch / names are "Node N key K"

That's the no-credentials mode (see the table above). Add mobile
credentials under **Configure**, then reload.

### The integration reconnects

`Vitrea V-Box connection lost, reconnecting` means the connection
dropped; it retries on its own with a growing delay, entities show as
unavailable meanwhile, and states are re-read when it is back.

### A light's state in HA is stale or wrong

State arrives by push. One node per minute is re-read as a safety net.
After a restart, states shown are the last saved ones until the box has
re-sent them (a couple of minutes on a large install). If it stays
wrong, open an issue with debug logs.

### Commands fail

A command the box rejects is reported as an error in HA (for example
`error 004`: that command does not apply to that key, like a dimmer
command on a plain relay). If the link is down you get a "not
connected" error instead of a late surprise.

### Entities are missing, misnamed, or a duplicate/ghost circuit appears

- Names, rooms and types are cached after the first scan. After
  renaming something on the box, use **Configure -> Rescan device on
  every startup**, click **Reload**, then turn it back off.
- To hide a duplicate or ghost circuit, add it to **Excluded keys**
  (`node:key`, 0-indexed).

### Dimmers don't dim

Dimmer support is untested against real hardware (see above). Please
open a **Dimmer test report** issue.

### Reporting a problem

Open an issue with your Home Assistant version, integration version,
and the debug log lines around the problem.

## Known limitations

- Dimmer brightness control is unverified (see above).
- Without mobile credentials there are no names, rooms or scenes.
- Air conditioning and timer scenarios exist in the Vitrea app but are
  not supported here.
- This is a reverse-engineered protocol client, not an officially
  supported integration. Vitrea could change the protocol at any time.

## Disclaimer

This is an unofficial, community project. It is not affiliated with,
endorsed by, or supported by Vitrea. "Vitrea" and the logo are
trademarks of their owner and appear here only to identify the hardware
this integration talks to.

## License

MIT — see [LICENSE](LICENSE).
