# Changelog

## 0.5.0
- **Control and live state now use the V-Box's simple protocol (port 11502): no login, no
  session limit, ~10 ms command acks and push updates.** The mobile protocol (11503) is no
  longer held open, which removes the reconnect/dropped-session problems and means the phone
  app and Home Assistant no longer kick each other off.
- **Zero-config setup:** the host is optional (leave it empty and the integration scans your
  network for a V-Box), and mobile credentials are optional. Without them every device is
  discovered from the box and named "Node N key K" (on/off keys are switches, blinds are
  covers); with them you still get names, rooms, types and scenes, via a short login that is
  closed again.
- Existing installs keep working unchanged (same unique IDs, same credentials). Scenes and
  scenario enable switches still need mobile credentials: the simple protocol cannot list
  scenarios.
- Device list is cached after the first discovery (a few minutes on a large install). Last
  known states are saved across restarts. One node is re-read per minute as a safety net.
- Dimmer levels are sent with the simple protocol's dimmer command, which is unverified on
  hardware. Please report if you have a dimmer.
- Fix-ups in the options form: mobile user/password can be added or changed later.

## 0.4.1
- Fix: Home Assistant could freeze (UI spinning, API hanging) when the V-Box went mute. Entity
  state reads piled up behind one send lock and exhausted Home Assistant's thread pool. Requests
  now never wait longer than their own timeout, reads fail instantly while the session is known
  bad, are de-duplicated per node and capped, and the keepalive allows one ping in flight.

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
