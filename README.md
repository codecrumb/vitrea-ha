# Vitrea V-Box for Home Assistant

> **If you can use [Vitrea's official Home Assistant integration](https://github.com/vitrea-sh/hass-vitrea), use that instead.** This project exists specifically for the case where you *can't* — see below.

A Home Assistant custom integration for the Vitrea V-Box smart-home
coordinator, built by reverse-engineering the wire protocol the
official **Vitrea ControlPro** mobile app uses (not the admin/Control4
protocol).

## Why this exists

Vitrea's own official integration talks to the V-Box over the *admin*
protocol — the same channel used by Control4 and the installer tools.
If your V-Box already has Control4 (or anything else) occupying that
admin session, the official integration doesn't work alongside it.

This integration instead uses the V-Box's **mobile protocol** (the one
the phone app uses, port 11503, separate login accounts
`vitrea1`-`vitrea8`), so it runs completely independently of whatever
else is already talking to your box over the admin protocol. Use this
only if that's your situation — otherwise the official integration is
better supported and more complete.

## What it supports

- **Lights** (on/off and dimmable — see note below)
- **Switches**
- **Fans** (on/off relays, no speed control)
- **Covers / blinds** (continuous absolute position, with mid-travel stop)
- **Scenes** (your V-Box's saved scenarios)
- Live push updates (no polling needed in the normal case), with a
  reconnect watchdog, an idle keepalive, and a slow safety-net poll to
  correct any state drift from a missed push
- A metadata cache so startup doesn't re-scan your whole box every
  time (toggle-able, see Options below)

**Dimmers are implemented but unverified against real hardware** — the
author's own installation has no dimmer-category circuits, so this has
never been exercised against a real dimmer. **If you have one, your
testing would genuinely help** — please open an issue either way
(works great, or something's off) so this note can eventually come
down.

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

- **Host** — your V-Box's IP address
- **Mobile port** — `11503` (default)
- **Mobile user** — one of `vitrea1`-`vitrea8`. Pick a slot that isn't
  already used by the phone app on your own devices (if you're not
  sure, try `vitrea2` and leave `vitrea1` free).
- **Mobile password** — the password for that account

### Options

Reachable from the integration's **Configure** button:

- **Rescan device on every startup** (off by default) — skips the
  metadata cache. The full scan can take a few minutes on a large
  install, so this is off by default; turn it on, click **Reload**,
  then turn it back off to force a one-off fresh scan (e.g. after
  renaming something on the box itself).
- **Excluded keys** — comma-separated `node:key` pairs (0-indexed) to
  skip when building entities, for any key that turns out to be a
  duplicate/ghost circuit on your particular install.

## Troubleshooting

**Enable debug logging first** - it makes every report far easier to
diagnose. In `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.vitrea: debug
```

### "Failed to connect" when adding the integration

- Check the V-Box IP and that port `11503` is reachable from Home
  Assistant (same VLAN, or a firewall rule allowing it).
- Check the user/password. A wrong password shows up in the log as
  `login failed for user 'vitreaN': bad credentials`.
- **Each `vitrea1`-`vitrea8` account allows one session at a time.** If
  the phone app (or another HA instance) is logged in as the same
  account, one of the two gets kicked off. Give Home Assistant its own
  account.

### The integration keeps reconnecting

Log lines like `session unresponsive` or `Vitrea mobile connection
down, reconnecting` mean the connection dropped. An occasional one is
normal: the V-Box itself silently stops answering a mobile session every
few minutes (measured on one install, cause unknown). The integration
pings it every 10 seconds and replaces a dead session in the background,
so you normally won't notice.

If it happens constantly (several times a minute), the account is most
likely being used somewhere else (see above) or the V-Box is overloaded.
Use a dedicated account, and don't run the CLI, other scripts, or a
second HA against the same account.

### A light's state in HA is stale or wrong

State normally arrives by push. A slow safety-net poll (every 5
minutes) corrects any missed push, and logs
`Vitrea poll corrected ...` when it fixes something. If you see that
often, please open an issue with debug logs.

### Commands feel slow, or two presses are serialized

The mobile protocol only allows one request in flight at a time, so
simultaneous commands are queued by design, not a bug. Firing many
commands at once can make the box stop answering for a moment.

### Entities are missing, misnamed, or a duplicate/ghost circuit appears

- Names, rooms and types are cached after the first scan. After
  renaming something on the box, use **Configure -> Rescan device on
  every startup**, click **Reload**, then turn it back off.
- To hide a duplicate or ghost circuit, add it to **Excluded keys**
  (`node:key`, 0-indexed).
- Startup can take a few minutes on a large install while the first
  scan runs.

### Dimmers don't dim

Dimmer support is untested against real hardware (see above). Please
open a **Dimmer test report** issue.

### Reporting a problem

Open an issue with your Home Assistant version, integration version,
and the debug log lines around the problem.

## Known limitations

- Dimmer brightness control is unverified (see above).
- This is a reverse-engineered protocol client, not an officially
  supported integration. It's been run live against a real V-Box for
  months without major issues, but Vitrea could change the protocol at
  any time.

## License

MIT — see [LICENSE](LICENSE).
