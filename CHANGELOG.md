# Changelog

## 0.4.0
- Rooms as areas now also applies to existing devices (never overrides an area you set).
- Wrong password is reported as such (invalid auth) instead of "cannot connect"; a rejected
  login at startup starts a re-authentication flow. Host is normalised so a differently
  cased host no longer creates a duplicate entry.
- The unique-ID migration no longer deletes any entity; it skips a move that is blocked.
- README: disclaimer, accurate device layout. Minimum Home Assistant 2024.11.

## 0.3.2
- Scenes and scenario enable switches are grouped under a "Vitrea Scenes" device.

## 0.3.1
- Fix: node devices no longer use `via_device` (recent Home Assistant rejected it and no entities loaded).

## 0.3.0
- Commands fail fast: one try, one reconnect and retry, 10 s lifetime, newest per key wins,
  errors surface in Home Assistant. Logins are rate-limited with backoff after short-lived sessions.
- Sessions log out on close and follow the official app's post-login handshake.
- Scenarios from the full list (including ones the main list omits) and per-scenario enable switches.
- Boiler keys use the app's timer value; per-node devices; optional rooms as areas.

## 0.2.x
- Cover stop, dimmer restore-level, faster reconnects and availability fixes.

## 0.1.0
- First public release.
