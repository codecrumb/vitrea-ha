# Changelog

## 0.5.0-beta.3
- **Exact names again, with nothing to enter.** The box's own device list (admin protocol) is read once, cached,
  and used first: real names, rooms, types, scenes, and devices the phone app never lists (a fan). The vendor-default
  admin login is tried automatically; a different password goes in the options. If it is refused or the admin
  session is busy (Control4), it is skipped without disturbing anyone and the older sources are used.
- Physical keys keep their old entity IDs, so entities you had before beta 1 (and their renames and areas) come back.
  Generic "Node X key Y" entities from beta 2 that now have real names are left over: delete them in the entity registry.
- No more full state dump on restart or at first start with the admin list: states are re-read one node at a time with
  gaps, and commands go first, so a command is never stuck behind a multi-minute dump.
- New options: admin user / password (only if not the default).

## 0.5.0-beta.2
- **Fix: some lights did nothing and showed the wrong state (seen live: "Matan Main").** The mobile
  protocol numbers only the physical keys of a node, the simple protocol numbers every key
  (multi-way aliases and keys with no mobile slot included), so the two are not offset by a
  constant. Everything is now addressed by the box's own (simple-protocol) key. Names from the
  mobile login are attached only on nodes where the pairing is certain (same number of keys in
  both lists and compatible kinds). On every other node the keys get generic names
  ("Node 41 key 7") and correct control. Entities that were named from the old, wrong pairing
  on those nodes disappear (delete the leftovers in the entity registry); renames and areas on
  paired nodes are kept.
- Keys that have no mobile slot at all (a fan, for example) now work and appear.
- The first start discovers devices even with mobile credentials (needed for the pairing), then
  caches them.
- The one-node-per-minute safety poll no longer runs while a full state dump is streaming
  (the box rejected those queries with error 005).
- Excluded keys accept generic keys as `node:sN` (for example `41:s7`).

## 0.5.0-beta.1
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
- Known gap (beta): the simple protocol omits or only button-reports a few keys (about 12 of
  217 on the author's box). They are missing without mobile credentials; please report any.
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
