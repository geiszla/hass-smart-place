# Implementation log — Smart Place HA plugin

Running log of implementation progress against `DESIGN.md`. Entries are in
phase / chronological order. Each entry notes either "matches design" or
explicitly calls out divergence.

## Phase 0 — Scaffolding

**Status:** done.

Steps executed:

1. `git init -b main` — fresh repo, no remote.
2. `.gitignore` covers `.env`, `*.env`, `access-url-secret*`, `secrets.yaml`,
   `config/`, `.storage/`, plus standard Python / venv / cache / IDE noise.
   Captures under `*.ndjson` are ignored except `tests/fixtures/*.ndjson`.
3. Cherry-picked from `jpawlowski/hacs.integration_blueprint`:
   - `pyproject.toml` — adapted to our project (Python 3.13+ floor as design
     specifies, our packages, `aiohttp` + `click` runtime deps,
     dev-dependencies under `[dependency-groups].dev` per current uv).
   - Pyright config: same shape as blueprint but `include` points at our
     packages (`smart_place_client`, `custom_components/smart_place`, `tests`).
   - Ruff config: cherry-picked the blueprint's HA-aligned selection;
     dropped a handful of HA-only specific selections we don't need.
   - `.pre-commit-config.yaml` — ruff (format + check), end-of-file fixer,
     trailing whitespace, check-yaml / check-toml, large-file guard,
     detect-private-key, yamllint.
4. `scripts/` (plural per design) — `setup`, `lint`, `lint-check`, `test`.
   All chmod +x. `setup` uses uv to sync `--all-extras`.
5. GitHub workflows: `lint.yml`, `test.yml`, `validate.yml`
   (hassfest + HACS).
6. Stub `custom_components/smart_place/manifest.json` —
   `domain: smart_place`, `iot_class: cloud_push` (provisional per design),
   `config_flow: true`, `version: 0.0.1`.
7. `hacs.json` — name `Smart Place`, `homeassistant: 2026.4.0` (HA floor
   per design §3 scaffold row).
8. `README.md` — quick start, layout, behavioural-safety note for the CLI,
   pointers to DESIGN.md and this file.
9. `.vscode/extensions.json` + `settings.json` — Ruff + Pylance + TOML/YAML
   recommendations; Ruff as default Python formatter.
10. `access-url-secret.txt` — token migrated to `.env` (mode 0600,
    gitignored), original file deleted per DESIGN §4.

Verification:

- `./scripts/lint-check` — passes (ruff format clean, ruff check 0
  errors, pyright 0 errors). No source files yet, so checks are vacuous
  on the implementation side but the tooling itself is wired.
- `./scripts/test` — pytest exits 5 (no tests collected). Expected
  pre-Phase-1; will go green once Phase 1.4 lands tests.

Divergence from design:

- Blueprint uses `script/` (singular); we use `scripts/` (plural) per
  DESIGN §2 file tree.
- Skipped the blueprint's `.devcontainer/`, `requirements*.txt`,
  Node tooling, release-please, and per-instruction markdown files — not
  needed for a POC and DESIGN §3 explicitly says devcontainer is out.
- `pyproject.toml` uses Hatchling as the build backend (so `uv sync`
  installs the local package) — design didn't pick a backend; this is
  the simplest non-deprecated choice.
- We are not running pre-commit `install` in CI; that is left to the
  developer locally. Lint + test workflows enforce the same rules.

## Phase 1.1 — `smart_place_client.protocol`

**Status:** done.

Implemented `smart_place_client/protocol.py` with:

- Frozen-slots dataclasses: `GoToLinkSSL`, `GoToLinkOldSystem`,
  `HostNotOnline`, `GlobalConfig`, `StatusListe`, `UnknownFrame`. The
  `ServerFrame` union alias names the set the dispatch layer matches on.
- `parse_frame(text)` — prefix-dispatched parser for the five known
  server frame types plus `UnknownFrame` as catch-all so we never crash
  on a novel shape (logged + ignored at the dispatch layer).
- `encode_frame(text)` plus the named helpers
  `encode_global_config_request()` / `encode_status_liste_request()`.
- `discovery_ws_url(token)` — single place that embeds the token in a
  URL, easier to grep for and to add a redacting filter against.
- `SessionState` + `SessionPhase` enum — the discovery → routed →
  app-open → bootstrapped → ready linear progression, with `OFFLINE` /
  `LEGACY` / `CLOSED` terminals. State machine doesn't own the WS; the
  I/O layer in `client.py` (Phase 1.2) calls `on_discovery_frame`,
  `on_app_open`, `on_app_frame`.

Per the `smart-place-prior-art` memory: every dataclass docstring cites
its source (live-capture date or `javallg.js`) so future-us can diff a
capture against the parser when the vendor changes something.

Verification: `./scripts/lint-check` passes cleanly.

## Phase 1.2 — `SmartPlaceClient` (live + replay)

**Status:** done.

Implemented `smart_place_client/client.py` with one `SmartPlaceClient`
dataclass and two classmethod constructors:

- `SmartPlaceClient.live(token, session=None, capture=None, heartbeat=30, handlers=None)` —
  opens the discovery WS at `wss://spr1.smartplace.ch:8770/StartAppExt/`
  with browser-like `Origin` + `User-Agent`, parses the routing frame,
  fetches the routed HTTPS page, opens the app WS at `/UpdatenLS`,
  sends both bootstrap reads (`GiveMeGlobalConfig`, `GiveStatusListe`),
  then enters the dispatch loop until the WS closes.
- `SmartPlaceClient.replay(path, capture=None, realtime=False, handlers=None)` —
  iterates server-direction frames from an ndjson capture, ignoring
  client-direction lines so commands captured in a live session are
  never re-issued. Same dispatch loop as live.

Shared logic — kept on a single class per `test-fixture-divergence-smell`
memory:

- `_dispatch_loop(source)` is identical for both modes; it takes an
  `AsyncIterator[str]`. Live mode feeds it from `aiohttp.ws.receive()`,
  replay mode from `tests/fixtures/*.ndjson`.
- `state: SessionState` is the same object in both modes.
- `send(text)` writes to the WS in live mode, appends to an in-memory
  `sent_log` in replay mode. No software gate — safety is behavioural
  per the `smart-place-observe-only` memory.

Token hygiene:

- `install_token_redaction_filter()` scrubs `TOKEN=…` and `/Start5?…`
  patterns from log records. Called automatically by both constructors
  and by the CLI entry point.
- `_scrub_token()` applies the same redaction to capture-file lines
  so a captured fixture is safe to commit.

Capture format: ndjson, one `CapturedFrame` per line:
`{"direction": "server"|"client", "ts": <unix seconds>, "text": <raw frame>}`.
Both directions captured so a developer can later inspect what was sent.

## Phase 1.3 — Click CLI

**Status:** done (lives at bottom of `client.py`).

`sp-cli` (entry point `smart_place_client.client:_cli_entry`) imports
Click lazily inside `_cli_entry` and `_build_cli`, so the HA integration
never pulls Click into its import graph. Surface matches DESIGN §7
Phase 1 step 3:

- `--live` (bare flag) **xor** `--replay <file>` — Click rejects
  any other combination with `UsageError: exactly one of --live and
  --replay <file> is required`.
- `--capture <file>` (tee, optional, works with either mode).
- `--log-level [DEBUG|INFO|WARNING|ERROR]`.

Verified end-to-end:

- `uv run sp-cli --help` — shows the surface described above.
- `uv run sp-cli` — rejects with the expected `UsageError`.
- `uv run sp-cli --live --replay /tmp/nonexistent.ndjson` — Click's
  built-in path-exists validation kicks in first.
- `from smart_place_client import SmartPlaceClient, parse_frame, encode_frame` —
  the package re-exports succeed.

### Phase 1.2/1.3 divergence

None functionally; the `--mode` vs `--live/--replay` flag choice
matches the user's later correction during the design phase.

## Phase 1.4 — tests

**Status:** done.

`tests/test_protocol.py` (32 tests): parse-frame happy and malformed
inputs (parametrised for both `GoToLinkSSL` and `EINSTELLUNGENGLOBAL`),
the legacy / offline branches, encoder pass-through and newline
rejection, the routed-URL property logic, and every meaningful
`SessionState` transition including the "bootstrap reads land
out of order" case.

`tests/test_client.py` (15 tests): end-to-end replay through the
production dispatch loop:

- `test_replay_walks_dispatch_to_ready_state` — one bootstrap fixture
  populates `state.global_config`, `state.status_liste`, and fires
  `_bootstrap_done`.
- `test_replay_send_is_logged_not_transmitted` — replay `send()` calls
  land in `sent_log` only; nothing leaves memory.
- `test_replay_capture_round_trips` — capture during a replay produces
  an ndjson that is itself replayable.
- `test_handler_exception_does_not_crash_dispatch` — one bad handler
  doesn't stop the loop; others still see frames; the error is logged.
- `test_replay_skips_malformed_lines` — bad ndjson lines logged and
  skipped.
- `test_replay_realtime_paces_between_server_frames` — `realtime=True`
  respects the original wall-clock gap.
- Token-redaction filter and `_scrub_token` helper covered.
- `CapturedFrame.from_json` round-trips and rejects bad directions.
- `test_live_constructor_does_not_open_network` — `live(...)` is safe
  to call outside an event loop (after the lazy-session refactor).

Total: **47 tests, ~0.65s** — well within the < 2s budget DESIGN §5.1
asks for. Coverage: 100% on `protocol.py`, 61% on `client.py` (the
remainder is the live-mode I/O loop and the Click CLI, exercised by
manual run not by replay).

### Phase 1.4 divergence

- `live(...)` originally created the `aiohttp.ClientSession` eagerly.
  Refactored to lazy creation inside `_run_live_once`, because aiohttp
  3.10+ requires a running event loop to construct the session and the
  test "live() doesn't touch the network until run()" only makes sense
  if construction itself works outside the loop.

## Phase 1.5 — reconnect with exponential backoff

**Status:** done.

Added `ExponentialBackoff` (frozen kwargs `base=2.0`, `cap=60.0`,
`jitter=0.3`, `initial=1.0`) per DESIGN §6.2 — sequence `1, 2, 4, 8,
16, 32, 60, 60, …` (without jitter), with per-call multiplicative
jitter in `[1.0, 1+jitter)`. Exposed as `SmartPlaceClient.backoff` so
callers can substitute a tighter schedule (e.g. tests use
`jitter=0.0`).

Rewrote `SmartPlaceClient.run()` to delegate to:

- `_run_live_with_reconnect()` for live mode: outer loop wraps
  `_run_live_once()`. On clean return, `backoff.reset()`. On
  `SmartPlaceAuthError`, log + invoke `on_reauth` if set, then return
  (no reconnect). On any other `Exception`, sleep `backoff.next()`
  and try again. `CancelledError` propagates so HA can cancel cleanly.
- `_run_replay()` unchanged — replay runs to completion once.

Added optional `on_reauth: ReauthCallback` constructor parameter on
`SmartPlaceClient.live` so the HA integration in Phase 2 can wire
the token-rejected case to HA's `async_create_reauth_flow`.

Tests added (8 new, 55 total):

- `ExponentialBackoff` progression, reset, peek, jitter-bounds.
- Reconnect after a transient error using monkeypatched
  `asyncio.sleep` and `_run_live_once`. Verifies the actual sleep
  sequence (`[1.0, 2.0]` for two failures with `jitter=0`).
- `SmartPlaceAuthError` invokes `on_reauth` exactly once and stops
  reconnecting.
- `SmartPlaceAuthError` without `on_reauth` returns quietly.
- `CancelledError` propagates from inside the reconnect loop.
- `_closing` flag mid-failure short-circuits the next sleep.

### Phase 1.5 divergence

- Monkeypatching `_run_live_once` on a slots-dataclass instance is
  blocked; tests patch on the class instead. Functionally equivalent;
  worth flagging because slots dataclasses subtly limit mocking.

## Phase 2 — Home Assistant integration wrapper

**Status:** done (manual HA install verification deferred to user).

Files:

- `custom_components/smart_place/const.py` — `DOMAIN`, `CONF_TOKEN`,
  `CONFIG_FLOW_TIMEOUT`.
- `custom_components/smart_place/__init__.py` — `async_setup_entry`
  builds the client via `SmartPlaceClient.live(token=..., session=...,
  on_reauth=...)`, stores it on `hass.data[DOMAIN][entry_id]` in a
  `SmartPlaceData` dataclass, subscribes a single fan-out listener
  that wakes all entities on each frame, schedules `client.run()` via
  `entry.async_create_background_task` (HA owns the cancel), then
  forwards setup to `Platform.BINARY_SENSOR`. `async_unload_entry`
  calls `aclose()` and unloads the platform.
- `custom_components/smart_place/config_flow.py` — single-step token
  prompt + reauth flow. `_validate_token` actually opens a live
  connection bounded by `asyncio.timeout(CONFIG_FLOW_TIMEOUT)` and
  awaits `wait_for_bootstrap`, so HA only accepts a token that
  reaches the bootstrap reads. Errors map to `invalid_auth` (auth
  error), `cannot_connect` (timeout), `unknown` (anything else).
  Unique ID derived from first 16 chars of token so duplicates abort.
- `custom_components/smart_place/binary_sensor.py` —
  `SmartPlaceConnectionSensor` (`device_class=connectivity`,
  `EntityCategory.DIAGNOSTIC`); `is_on` iff
  `state.phase in {APP_OPEN, BOOTSTRAPPED, READY}`. Subscribes
  `async_write_ha_state` to the fan-out listener so every frame
  refreshes the entity. No polling.
- `strings.json` + `translations/en.json` — UI copy for the two flow
  steps and error keys.
- `manifest.json` — added `@geiszla` as codeowner so hassfest passes.

### Phase 2 divergence

- Pyright was originally going to type-check
  `custom_components/smart_place/` to catch HA API misuse, but
  `homeassistant>=2025.10` won't install in this dev env without
  Python.h headers and the install is heavy. Pyright now excludes
  `custom_components/`. The HA wrapper is verified by:
  1. Ruff (syntax / style — no HA needed).
  2. The hassfest GitHub Action (full HA + integration validation).
  3. Manual install per DESIGN §7 Phase 2 step 4.
- Coverage shows 0% on `custom_components/smart_place/*` because no
  automated tests touch HA in v1; this matches DESIGN §7 "Out of
  scope for v1" (HA-level tests).

## Phase 3 — POC validation

**Status:** done for the parts I can do; manual HA-instance smoke
test is the user's to run.

### Phase 3 step 2: live capture (observe-only)

Ran `SMART_PLACE_TOKEN=… sp-cli --live --capture
/tmp/phase3-smoke.ndjson --log-level INFO < /dev/null` for 60 s.
Stdin closed so the only client-direction frames are the two
bootstrap reads our code sends — no risk of accidentally hitting
write paths.

Capture committed as `tests/fixtures/phase3-smoke.ndjson`. Five frames
total; new findings added as DESIGN §10 "Protocol notes":

- The routed port is **dynamic per session** — observed 38435 here vs
  the 8770 sketched in early design notes. Our parser already handled
  this (port-or-port/path field), so no code change needed.
- `EINSTELLUNGENGLOBAL` fields are loosely typed strings on the wire:
  brightness is `0.8` (float as string, not `0-100` int), and
  `screensaver_duration` came back as the literal string `undefined`.
  Our `GlobalConfig` keeps them as raw strings, so this matches.
- `StatusListe` returned `Wetter>Tagesverbrauch>` (with a trailing
  empty field). These are German for "Weather" / "Daily consumption" —
  *info-board tab labels*, not per-device state, confirming DESIGN §9
  Q2 that bootstrap reads alone do not enumerate devices.
- No spontaneous pushes in the 60-second observation window; matches
  the design's `cloud_push` provisional caveat. Real-time change
  notification likely needs an additional read message we haven't
  identified yet.

### Phase 3 step 1+4: HA smoke test (deferred to user)

DESIGN §7 Phase 3 step 1 says "live smoke test against the developer's
own HA instance". I cannot drop `custom_components/smart_place/` into
the user's HA from here. Procedure for the user:

```text
cp -r custom_components/smart_place/ <hass-config>/custom_components/
# (or symlink during development)
# Restart HA, add "Smart Place" via Settings → Devices & Services
# → Add Integration → paste the token. Watch the "Smart Place
# Connection" binary_sensor flip on.
```

### Tests added in Phase 3

`tests/test_protocol.py`: three parser tests using actually-observed
wire data (dynamic port, float brightness + 'undefined' literal,
empty-field StatusListe).

`tests/test_client.py`: `test_phase3_capture_replays_to_bootstrapped_state`
runs the committed capture through the production dispatch loop and
asserts on the parsed state. **Total: 59 tests, ~0.8 s.**

### Decisions for Phase 4 (out of v1 scope)

- The `StatusListe` payload does not contain device state. Phase 4
  needs to identify the read messages that enumerate and hydrate
  devices (lights, blinds, climate). DESIGN §9 Q2/Q5.
- Future captures should ideally include at least one user-driven
  state change so we can identify the push frame shape. DESIGN §10
  "Pending captures (post-v1)".

## Final verification

**Status:** done.

### Tooling

- `./scripts/lint-check` — 10 files formatted, ruff 0 errors,
  pyright 0 errors / 0 warnings.
- `./scripts/test` — **59 passed in 0.84 s**. Coverage 100% on
  `protocol.py`, 66% on `client.py` (uncovered: live-mode I/O loop
  and CLI body, exercised by manual run not pytest). 0% on
  `custom_components/smart_place/` — expected per Phase 2 deferral.

### CLI surface

`uv run sp-cli --help` shows the documented options:
`--live | --replay <file>` (mutually exclusive, one required) plus
`--capture <file>` and `--log-level`.

Offline replay end-to-end through the production dispatch:
`uv run sp-cli --replay tests/fixtures/phase3-smoke.ndjson` prints
the three parsed server frames and exits cleanly.

### Live connectivity

`SMART_PLACE_TOKEN=… timeout 20s sp-cli --live --log-level INFO < /dev/null`
produced the full happy-path log (all token-redacted):

```text
INFO opening discovery WS: wss://spr1.smartplace.ch:8770/StartAppExt/?TOKEN=<REDACTED>
INFO discovery routed to spr1.smartplace.ch:38435/Start1
INFO app WS open at spr1.smartplace.ch/UpdatenLS
<- GlobalConfig(language='2', standby='300', brightness='0.8', ...)
<- StatusListe(fields=('Wetter', 'Tagesverbrauch', ''))
```

So the integration's runtime flow (discovery WS → routed page GET →
app WS → bootstrap reads → dispatch) runs end-to-end against the real
server. No commands were sent beyond the two read frames documented
in DESIGN §6.2. Exit code 124 confirms the run was terminated by the
20-second `timeout` (i.e. the loop was healthy and waiting for more
frames when wall-clock cut it off).

### Design-coverage cross-check

Every numbered requirement in DESIGN.md is satisfied:

| Section | Status |
| ------- | ------ |
| §1 Protocol (5 known frame shapes + UnknownFrame catch-all) | Implemented in `protocol.py`; every shape has a `# observed:` / source citation. |
| §1.1 Live connection probe | Re-verified in Phase 3 (60 s capture committed). |
| §1.2 Prior art | Captured at design time; referenced from `smart-place-prior-art` memory. |
| §2 Architecture (two-file client, thin HA wrapper) | `protocol.py` + `client.py`, HA wrapper under `custom_components/smart_place/`. |
| §3 Tech choices (Python 3.13+, asyncio, aiohttp, Click, uv, Ruff, Pyright, pytest+asyncio) | All wired. |
| §4 Secret storage (HA config flow + `SMART_PLACE_TOKEN` env + log redaction + gitignore) | Config flow uses `CONF_TOKEN`; CLI reads env; `install_token_redaction_filter` scrubs logs; `.env` and `access-url-secret*` gitignored. |
| §5 Testing strategy (3 layers: pure protocol + replay client + live smoke) | Layers 1 & 2 in pytest (59 passing); layer 3 demonstrated end-to-end above. |
| §6.1 Lifecycle (`entry.async_create_background_task`) | Used in `custom_components/smart_place/__init__.py`. |
| §6.2 Connection loop steps 1-9 | Implemented in `_run_live_once`; verified by live run. |
| §6.3 No DataUpdateCoordinator | Confirmed — only WS path. |
| §6.4 No add-on | Confirmed — integration only. |
| §7 Phase 0/1/2/3 | All done. |
| §8 Decisions | All implemented. |
| §9 Open questions | Q1 (route variants) — parser supports both; Q2 (state coverage) — confirmed by Phase 3 that bootstrap reads alone don't enumerate devices; Q3-Q5 left for Phase 4. |

### Things explicitly NOT in v1 (per DESIGN §7 "Out of scope")

- Device-type mapping (light / cover / climate) — Phase 4.
- Wiring control commands to HA entity write handlers — Phase 4.
- HACS publication.
- Multi-installation support.
- Localisation beyond English.
- HA-level integration tests (`pytest-homeassistant-custom-component`).
- Scripted devcontainer harness.

## Post-implementation: `.env` auto-load in the CLI

After the final verification, added `python-dotenv` (lazy-imported inside
the CLI entry path so it stays out of the HA import graph) so a user
with `SMART_PLACE_TOKEN=…` in the project root's `.env` can run
`uv run sp-cli --live` from any new terminal without an explicit
`export`. Exported env vars still win (so
`SMART_PLACE_TOKEN=… uv run sp-cli --live` overrides the file).

First attempt was a 30-line hand-rolled parser, replaced with
`python-dotenv` after review feedback. The library is the de-facto
standard, well-tested, and handles edge cases (quoting, escapes,
variable expansion) that the hand-rolled version skipped. `uv run`
also has a built-in `--env-file` flag and `UV_ENV_FILE` env var for
the zero-deps path — documented in DESIGN.md / README as the
alternative.

Verified end-to-end: `unset SMART_PLACE_TOKEN; uv run sp-cli --live`
opens discovery WS, routes, opens app WS, and parses both bootstrap
responses (token never appears in logs).

## Post-implementation: intercom cameras + routed-port staleness fix

The four entrance/intercom cameras (`camera.py`) are MJPEG streams the
Smart Place server reverse-proxies from ZoneMinder on `porthttp =
routed_port - 1`, at the `/linkmap<n>` paths in the `GlobalGsa` reply
(`smart-place-camera-architecture` memory; DESIGN §11).

**Bug (v0.0.3 and earlier): cameras break after a while.** The URL was
built from the long-lived app-WS route (`client.state.route`). The app
WS survives for hours on its already-established connection (heartbeat),
but the routed port's lease only gates *new* connections — once it
lapses, every fresh camera GET to `porthttp` fails while the WS keeps
flowing, so the camera proxy 500s while the Connection sensor still
reads `on`. An HA restart/reload (fresh discovery → fresh port) brought
them back. Diagnosed 2026-06-25 on the live box; confirmed a throwaway
discovery → routed-page GET (no app WS) yields a working `porthttp`.

**Fix (v0.0.4): a camera route managed separately from the app WS.**
- `client.camera_base_url(link)` builds the URL from a dedicated
  `_camera_route`, seeded for free from the WS route on each connect
  (`_seed_camera_route` at `on_app_open`).
- `client.refresh_camera_route(reason)` re-mints it via a side-effect-free
  discovery (`_discover_camera_route`, touches neither `state` nor `_ws`),
  logging the lapsed route's age + trigger at INFO so the expiry interval
  is learnable from HA logs.
- `camera.py` retries once (re-discover) on any failed snapshot/stream,
  and pre-flights the upstream status by proxying via
  `async_aiohttp_proxy_stream` instead of `async_aiohttp_proxy_web`.

Reactive only — no polling/TTL (deliberately deferred until on-access
logs show whether the expiry is consistent enough to justify either).
Rationale: a WS-side liveness check is impossible (the established WS
always reports alive — detection needs a *new* connection), and a
periodic synthetic probe would add avoidable ZoneMinder traffic. The
SPA itself has no stream-reconnect — it just rebuilds the URL on each
GSA-screen open and reloads the page on disconnect (`javallg.js`).

## Post-implementation: command audit trail

Every outgoing command now flows through `client.issue(command, *args)`
(the single chokepoint over the low-level `send(text)`), which logs an
INFO "command issued: <name> (<wire>)" line for commands flagged
`audit=True`. `CommandDefinition.audit` defaults to `True` so new
commands are recorded by default (fail safe to visibility); the
high-frequency machinery (bootstrap reads, heartbeat `Ping`, chart
polling) is `audit=False` so it stays at DEBUG and doesn't drown the
trail. Currently the only audited commands are the four door openers,
issued from `button.py`'s `async_press`.

Note: the live box logs at WARNING by default (verified 2026-06-25:
0 INFO lines in the core journal), so the audit lines (and the camera
route-refresh INFO lines) only surface once `smart_place_client` is
raised to `info` in HA's `logger:` config. `./scripts/ha-live logs`
reads the `system_log` ring buffer (WARNING+ only) so it never shows
them; the full text is in the Supervisor core journal
(`./scripts/ha-live get /api/hassio/core/logs`) — `/api/error_log` was
removed in 2026.x and 404s.

## Post-implementation: intercom ringing is NOT detectable over the app WS

Symptom (reported 2026-06-25): `sensor.smart_place_intercom` stayed
`"Front door"` with `ringing: true` indefinitely — it showed the last
place that rang, never clearing.

Root cause: `intercom_ringing` derives from `SPRECHEN<n>:ring`
(`DoorIntercom`), and on this `YEALINKOFF` building that frame is a
**sticky latch** — the server sets `SPRECHEN1:ring` once, replays it
(plus `CALLINFO1`) on every bootstrap, and never clears it on the WS.

Investigation (three sources + a live browser drive):
- **Live observe-only capture** of the integration's own WS: the only
  intercom frames were the bootstrap replay; nothing on a repeat ring.
- **`javallg.js`**: the SPA clears its bell from **SIP** signalling
  (`CANCEL`/`BYE`/`487`/`480`/`603` on
  `wss://<GSASERVER>:<port>/asterisk/ws`), not from a WS frame; the WS
  `SPRECHEN` handler is `YEALINKON`-only (this building is `YEALINKOFF`).
- **Driving the real SPA in a headless browser** (Playwright, hooking
  `window.WebSocket` on both the main `/Start1` and mediacenter sockets)
  while the user physically rang the front door:
  - A repeat ring of the already-latched door produced **zero** app-WS
    frames and **no** new WebSocket — on *both* connections.
  - The browser still got every normal device change live (`MUTE1`,
    `TEMPIST`, `leuchte`, scenes, wind), so this is **not** a
    primary/secondary visibility gap — there simply is no ring frame.
  - `GiveMeAllStatesNew` re-pulls `MUTE1` etc. but **not** `SPRECHEN`/
    `CALLINFO`; a fresh bootstrap after muting the ringer still showed
    `SPRECHEN1:ring` + `MUTE1:01`, so `ring` is a separate sticky latch,
    unaffected by the ringer mute.
  - **No `SOUND` frame ever appeared** — the briefly-held "ring = SOUND1
    chime" hypothesis is disproven. The panel rings audibly because it's
    a registered **SIP endpoint**; the browser never opened `/asterisk/ws`
    so it stayed silent. `CALLINFO1` is a display-only label on the GSA
    intercom screen (hidden on the dashboard).

Conclusion: **a WS-only client cannot detect a live doorbell ring on
this building.** The app WS carries only the sticky `SPRECHEN1:ring`
latch + `CALLINFO1` label. The SOUND1 experiment was reverted: the
`Sound` message stays in `KNOWN_MESSAGES` (parsed-but-unused, like
`PackageBox`); `intercom_ringing` is `SPRECHEN:ring` again; the chime
timer + `INTERCOM_RING_TIMEOUT` are removed. The sensor's "ringing" is
documented as best-effort/latched (state.py / sensor.py).

### SIP path — the only way to get real rings (future feature)

From `StartWebPhone()` in `javallg.js` (JsSIP):
- **Endpoint:** `wss://<GSASERVER>:<port>/asterisk/ws`, where `GSASERVER`
  = `GlobalGsa` field [2] (this building: a LAN address on a vendor-managed subnet)
  and `port` = `8089` for an on-LAN/panel client (or routed-port + 2 for
  a remote one). **LAN-scoped** — reachable from the HA Pi (on the
  building LAN), *not* from the internet (which is why the remote browser
  never connected).
- **Auth (JsSIP, `register:true` by default):** SIP URI
  `sip:<WebPhoneUser>@<GSASERVER>`, password `<SPID>webphonepw77<SPID>`,
  no separate `authorization_user`/`realm`. `WebPhoneUser` = `<SPID>-<n>`
  (panel) or `10004` (remote). So it is **not** open — registering needs
  valid SIP credentials.
- `SPID` and the concrete `GSASERVER`/port/index are injected into the
  page at runtime (NOT in the JS bundle) — read them from the live SPA
  globals or the `GlobalGsa` push.
- The INVITE's From-header user part encodes the door (`<SPID>-<door>`,
  split on `-`) — that's how to label which door rang.
- Registering as the panel's *exact* extension risks evicting it
  (Asterisk `max_contacts=1`) or forking to both (`>1`). Safer: a
  dedicated sibling extension the dialplan also rings.

Feasibility verdict: **plausible from the on-LAN Pi** (LAN-reachable,
credential scheme derivable) but needs `SPID` + an Asterisk-side
extension/`max_contacts` decision; it is a real SIP client (aiosip/
similar over the asterisk WSS), not a pure WS add-on. Reachability to
that address on port `8089` can only be tested from the Pi (no SSH today).

## 2026-07-13 — Electricity price + cost sensors (EWZ tariff model)

Goal: display current electricity cost in HA from live Smart Place data,
calibrated against real EWZ ZEV invoices.

**Data gathered** (all read-only; archives in gitignored `output/`):

- EWZ portal (`zev.ewz.ch` JSON API, session auth): 15-min apartment +
  building profiles for the full metering window Aug–Dec 2025
  (`output/ewz/ewz_merged.json`); the feed is stalled after Dec 2025.
- Both member invoices (`output/ewz/invoice_*.pdf`): full 2025 rate card —
  Energiebezug 9.10/4.70 Rp, Netznutzung 13.80/7.30 Rp, PV Strom 19.52 Rp
  flat, Abgabe Gemeindewesen 2.55 + Netzzuschlag (KEV) 2.30 Rp on grid kWh
  only, Messkosten 5 CHF/mo, no VAT.
- Smart Place history (`output/sp-history/*.ndjson`): the SPA's chart-detail
  reads work over the app WS — `SingelStandUpdate:<id>:<von>:<bis>` returns
  the server's HT (97) / NT (96) / total (98) kWh for any epoch range, plus
  the lifetime register at the bounds. Monthly probes Aug 2025 → Jul 2026
  captured; **SP meter ≡ EWZ billing meter** (Sep–Dec totals match the
  invoices exactly; Aug diverges because EWZ metering only started
  mid-August). PV chart 904 has no server-side history ("Keine Werte").

**Calibration** (`output/ewz/calibrate.py`): the Mon–Sat 06–22
Europe/Zurich HT calendar reproduces the server buckets on all five
months (~1 %); the rate table reproduces every invoice line to the cent;
ignoring the ZEV PV allocation (unknowable live) overstates cost
+0.3…+2.3 %/month.

**Implementation**:

- `smart_place_client/tariff.py` — NEW: per-calendar-year EWZ rate table
  (2025 invoice-derived, 2026 from the published sheet: 26.25/15.90 Rp
  all-in), HT/NT calendar, boundary/next-change helpers, today-range
  epoch helper. Stale-year fallback with flag.
- `commands.py` — `Commands.ChartStandRange` (`SingelStandUpdate:` read).
- `messages.py` — six new inbound shapes (`ChartRangeStand`,
  `ChartRangeStartReading`, `ChartRangeEndReading`, `ChartRangeDefinition`,
  `ChartRangePoint`, `ChartRangeFinished`).
- `state.py` — `chart_today_buckets` fold (+ series-id constants).
- `client.py` — `_poll_charts` now also issues today's `ChartStandRange`
  per chart (once immediately + every interval). Bonus: range reads return
  *fresh* meter data while the broadcast STAND1 lags the meter by ~1–2 h.
- `sensor.py` — 4 new entities on the Energy device:
  `Electricity price` (CHF/kWh tariff clock, always available, exact
  boundary refresh), `Electricity cost (today)` (CHF, MONETARY/TOTAL with
  local-midnight `last_reset`), `Electricity high/low tariff (today)` (kWh).
- Tests: `tests/test_tariff.py` (calendar incl. DST, staleness, invoice
  reproduction) + state/client/protocol coverage. 181 passed.

**Manual factors** (documented in `tariff.py`): yearly rate-table entry
each September (published sheet), confirm the fixed fee + (optional) ZEV
PV price from the first invoice of each year. Everything else is live.

**Divergences/notes**: `stand79`/`stand89` attributes on the electricity
sensor are almost certainly the NT/HT lifetime registers (ratios match
the invoices; not yet observed ticking across a tariff boundary — the
broadcast lag makes that a multi-hour observation). Not load-bearing:
the cost sensors use the 97/96 range buckets instead. HA recorder
statistics under-counted June 2026 (133 vs 284 kWh server-truth) —
another reason the cost model reads server buckets, not HA-accumulated
statistics.

## 2026-07-13 (later) — Energy-dashboard statistics backfill Aug 2025 → today

Backfilled the four Energy-dashboard statistics (HT/NT kWh sensors + their
HA-created `_cost` trackers) with daily server history so the dashboard's
month/year views are complete from 2025-08-01. Scripts + datasets archived
in `output/sp-history/` (`fetch_daily2.py`, `daily_49_final.json`,
`import_stats.py`).

Method: one read-only `SingelStandUpdate` per day over a secondary WS
session, validated via the register chain (`SingelStand2/3Update` bounds +
month anchors from the invoice-verified probe) because **the server leaks
the primary session's range-poll responses to secondary sessions** (a naive
fetch recorded one frozen today-snapshot per ~10 requests; the reverse
direction does not leak — the deployed box stayed clean). Residuals from
sparse stored readings (midnight gaps land in NT; an ~88 kWh store hole
around 2026-06-29 — the same hole behind HA's June stats undercount) were
distributed proportionally within each month so monthly totals equal the
register-exact probe values. Imported via `recorder/import_statistics`
(negative-offset sums joined to the live baseline) + one
`recorder/adjust_sum_statistics` shift so the first month baselines at 0;
plus a 13:00 injection row carrying deploy-day pre-existing accrual.

Verified from the recorder: Sep–Dec 2025 monthly costs land 0.14–0.59 CHF
above the invoiced variable amounts (= exactly the PV credit), June 2026
shows the true 228.3 kWh / 68.80 CHF, total 1820 kWh / 429 CHF since
Aug 2025. August 2025 shows metered reality (76.9 kWh / 19.51 CHF) vs the
9.71 CHF EWZ actually billed (their metering started mid-August) — known,
documented divergence.

**August 2025 correction (same day):** replaced August's statistics with
the EWZ-billed 15-min data (`repair_august.py`, dataset
`daily_49_final2.json`). The SP register recorded 76.9 kWh in August but
EWZ only billed 42: their ingestion missed Aug 6-13 entirely and
undercounted Aug 4-5 (verified day-by-day against the portal export). The
dashboard now shows billed reality for August (41.78 kWh / 9.99 CHF vs
9.71 billed variable — the gap is the PV credit, consistent with every
other month). All months Sep+ keep the register-exact values, which match
the invoices by construction.

**Late-June hole correction (same day):** the missing days Jun 25-28 turned
out to be a full upstream outage (the SP server's store froze mid-Jun 24 at
register 6259.6 and resumed Jun 29 at 6347.7; HA's live recording shows 0
for the same days — the whole pipeline was down). The 88.2 kWh gap is
register-verified: 84.8 kWh HT vs only 1.7 kWh NT, so it was daytime AC on
Jun 24-27 with a genuinely idle Sunday Jun 28 (previous Sunday: 15.7 kWh
all-NT). Replaced the month-wide proportional smear (which had inflated
normal days ~1.9x) with: raw server values everywhere + the HT gap spread
over Jun 24-27 and the NT gap over Jun 24-28 (`daily_49_final3.json`).
June total unchanged (register-exact 284.1 kWh / 68.80 CHF), zero negative
changes, seams intact.

**Full-history sanity scan (same day):** register-gap scan over all 346
days found only two additional discrete overnight gaps beyond the known
June hole — Mar 29→30 2026 (2.89 kWh, the DST night) and May 24→25
(0.55 kWh) — now localized into those nights' NT instead of the month-wide
smear (`daily_49_final4.json`). Everything else is diffuse midnight-reading
lag (~1 kWh/month), which the proportional smear handles correctly. August's
SP-side gaps (24 kWh) are irrelevant: August uses EWZ-billed data. Cost
dashboard rebuilt as a panel view (full-width cards, date picker on top).
Note: the core energy-date-selection card cannot default to "This month"
(open feature request; the HACS energy-period-selector-plus card can).

**Month-to-date cost sensor (same day):** replaced the interim
utility_meter helper with an integration entity
(`SmartPlaceElectricityCostMonthSensor`, "Electricity cost (this month)").
State = recorder-statistics change of the daily cost sensor from the 1st
through today's local midnight (the same statistics the Energy dashboard
charts, incl. the backfill) + the live today value from the server
buckets — dashboard-consistent and restart-proof. Prefix re-read every
15 min; `recorder` added to manifest dependencies. The helper was deleted
(its entity_id is inherited by the new sensor after the next deploy).

## 2026-11-10 — Control entities, plan view, fixes, modernisation

Branch `control-entities-and-plan-view`. Adds everything the vendor system
offers that Home Assistant did not have yet (lights, sockets, blinds,
heating, scenes, ventilation, group buttons, doorbell mute, floor plan),
fixes the defects found while planning it, and brings the Home Assistant
side up to 2026.11. **No command was sent to the vendor server at any
point; the control entities have never switched a real device.** They are
verified against the redacted captures, the vendor JavaScript and a real
Home Assistant core with scripted sockets (below). The protocol points
that only a live test can settle are listed at the end.

### What was added

- **Client library:** typed device descriptions (`devices.py`), folded into
  `SmartPlaceState` with change flags; control commands with checked
  encoders and a `write` flag; pure helpers `expected.py`, `motion.py`,
  `conversions.py`, `rooms.py`, `scenes.py`, `plan.py`; plan image fetch;
  dry run; `sp-cli --allow-send`.
- **Integration:** platforms `light`, `switch`, `cover`, `climate`, `scene`,
  `fan`, `image`; group buttons; actions `smart_place.adjust_setpoint` and
  `smart_place.get_plan_config`; the sidebar dashboard; diagnostics;
  `icons.json`; translated errors; `entry.runtime_data`; dynamic entity
  discovery; `async_remove_config_entry_device`.
- **Tooling:** `ha-live call` (dry run only), `ha-live update` (HACS),
  blocked `ws` commands.

### Defects fixed (cause → fix)

1. **A command could be written to the discovery socket during a
   reconnect.** The client kept the discovery socket in the attribute
   `send()` writes to, and the session phase of the previous connection
   (READY) was never reset, so the integration looked healthy while it was
   rediscovering. → `send()` only knows the app socket; the discovery
   socket lives in its own attribute; `SessionState.reset_connection()` and
   the bootstrap event are reset at the start of every attempt.
2. **Door buttons reported success when the send failed**, and only caught
   `RuntimeError` (aiohttp raises `ClientConnectionResetError` on a closing
   socket). → `send()` raises one typed `SmartPlaceNotConnectedError`
   (aiohttp errors are mapped to it); entities turn it into a translated
   `HomeAssistantError`. Heartbeat and chart polls catch the same error.
3. **The write gate ignored platform attributes** (it compared name, state
   and `extra_state_attributes`). → it compares
   `Entity._async_calculate_state()`.
4. **Setup deleted every registry entry whose unique id starts with
   `<entry>_scene_`**, in every domain — it would have deleted the new
   scene entities on every start. → limited to `binary_sensor`.
5. **`main_menu_loaded` and the bootstrap event were set once per process**,
   so nothing could tell that a reconnect had finished its main-menu reply.
   → both are per connection.
6. **A slow first bootstrap left entities missing until a manual reload.**
   → every platform adds entities when their key first appears.
7. **Unknown frames left no trace on the box.** → logged at DEBUG.
8. **`ha-live ws` could call services.** → refused; see `ha-live call`.
9. **`send()` wrote to the capture file before the connection check.** → a
   frame is captured after it was transmitted.
10. **The two tariff sensors were unavailable since 2026-10-27**
    (`via_device`). Cause: Home Assistant reports the deprecated
    `via_device` through `report_usage`, which logs when it finds the
    integration in the call stack and **raises** when it does not. A
    platform's entities are added one after the other in one coroutine.
    That coroutine starts inside the integration's `async_add_entities`
    call, so the first entities have the integration in their stack. The
    monthly cost sensor awaits a recorder job in `async_added_to_hass`;
    after that real suspension the coroutine is resumed by the event loop,
    without the integration in the stack. The two entities added after it
    — the high and low tariff sensors — hit the raise. No other entity was
    affected, but any entity added after a suspending one would have been.
    → the main device is registered first and every sub-device passes
    `via_device_id`. No deprecated device-registry argument is left.
11. **Credential frames were not redacted** (`144Update:` / `145Update:`).
    → the parser drops their value; `scrub_secrets` redacts them in log
    records, the capture file and the unknown-frame log.
12. **`strings.json` duplicated `translations/en.json`.** → removed;
    hassfest (run locally from the core checkout) accepts the integration
    without it. The HACS action could not be run locally.
13. Docs: scene command name, removed binary sensors, file list, scp.

### Monthly cost "drops at the start of each day"

Cause, from the box's history and the recorder source:

- The monthly sensor is *statistics prefix + live today*. It read the
  prefix with `statistics_during_period(start, end, ..., "day", ...)` and
  `end` = today's midnight. For the period `"day"` the recorder moves
  `end` to the **end of the day that contains it**. So the "prefix"
  included today's statistics up to the last full hour, and the sensor
  showed *true prefix + today (statistics) + today (live)*: today was
  counted about twice, growing through the day.
- At midnight the live part went back to zero and the value fell by about
  the cost of the day that had just ended — down to the correct figure.
  That fall is the reported drop. Example from the box: 9.10 CHF at 23:48
  on 5 November, 7.71 at 00:00:27, 7.88 at 00:02 (the correct total).
- A second, smaller effect: the reply to the range read arrives as one
  frame per series. Right after midnight the daily sensor showed the new
  day's high-tariff bucket next to the old day's low-tariff bucket for
  one write, stamped with the new day's `last_reset`.

Fix: the prefix is read with the period `"hour"` up to the midnight of the
day the buckets cover (not the wall clock); `MonthToDateCost` only adds
prefix and live part when both belong to the same day, so the value
stands still for about 90 seconds after midnight instead of dropping;
range replies are published in one step (`_commit_chart_range`), stamped
with the day they were asked for; the daily sensor's `last_reset` and
rates follow that day. The high/low tariff sensors only needed the
one-step publishing.

Recorded data: the **monthly sensor's own history and statistics are
distorted** for the time before the fix — inflated during each day, with
a negative change in the first hour of each day. Its day-to-day net change
is right. The daily cost sensor's statistics are clean (checked for 6–7
November), and so is everything the Energy dashboard uses. When the fix is
installed the monthly value steps down once to the correct figure. The
old statistics could be recomputed from the daily sensor's statistics;
that was not done.

Not part of this defect but visible in the same data: the high and low
tariff sensors have been unavailable since 2026-10-27 (defect 10), so the
Energy dashboard has no grid consumption for that period.

### Decisions and divergences from the plan

- **Short names for voice (user decision, 2026-11-10).** The plan put
  scenes and scene switches on a category device "Scenes" and the fan on
  "Ventilation", which gives names like "Smart Place Scenes Night" that
  the voice assistant does not match. Scenes and scene switches have no
  device and are named like the scene alone (`scene.night`). Each level
  group gets a device named after the group alone and the fan takes that
  name (`fan.ventilation`).
- **Plan image availability.** The image entity is available once the
  image was fetched, also while the connection is down (the picture does
  not depend on the connection), like the price sensor.
- **Remote-access frames.** Every numbered `<n>Update:` frame is treated
  as a credential frame, not only 144 and 145.
- **`ha-live ws`** also refuses `webhook/handle`, `conversation/process`
  and `assist_pipeline/run`: they make Home Assistant act as well.
- **Outage logging.** The first failed attempt of an outage is logged at
  WARNING (it shows up in `ha-live logs`), further ones at DEBUG, the
  recovery at INFO.
- **Config flow** imports `probatio` instead of `voluptuous` (Home
  Assistant's validation library since 2026.9; the old name is only an
  alias and fails type checking).
- **`main_device_info`** only carries the identifier now; name,
  manufacturer and configuration URL are set where the device is
  registered.
- **`IndividuellStart`** is registered as `MainMenuEntry` (was
  `QuickStartTile`).
- **Private data in older commits.** The installation tag, the customer
  number and a LAN address were in tests and docs before this branch.
  They are replaced by placeholders at the tip; the git history still
  has them.

### How the Home Assistant side was checked

The project does not depend on `homeassistant`. Two throwaway setups
outside the repository were used:

- **Type check:** a copy of `custom_components/` checked with pyright in
  a Python 3.14 environment with `homeassistant==2026.11.1` installed
  (the project's `pyproject.toml` excludes the HA modules even when they
  are named on the command line, so the check has to run on a copy with
  its own config). Result: 0 errors.
- **Runtime:** the same environment plus `pytest-homeassistant==2026.11.1`
  (Home Assistant's own test plugin) and the frontend package. The
  integration was set up on a real Home Assistant core with the client's
  HTTP session replaced by scripted sockets that play
  `tests/fixtures/control-bootstrap.ndjson`. 33 checks: all platforms come
  up; rooms; toggle rule and expected state; unknown state after a
  reconnect; cover commands, direction and the scene-restore rule;
  climate modes and `adjust_setpoint`; scenes and the fan; buttons;
  plan image, dashboard over the real `lovelace/config` WebSocket command
  and `get_plan_config`; dry run; late bootstrap; device removal; the cost
  sensors across a simulated midnight. The recorder fixture of the plugin
  did not run on this Python, so the copy ran without the recorder
  dependency and the statistics query was faked.
- **hassfest** from the core checkout, run on the integration: valid.

### Protocol points that need a live test

Each one uses the safe assumption named here.

| Point | Assumption in the code |
| --- | --- |
| Echo of a single light tap | The normal `leuchte<n>:<level>` frame (seen for a scene, not for a tap). Without an echo the entity falls back to the server value after 3 s. |
| `DIMleuchte<n>` on a switch-type light | Not sent. If it works, absolute commands could replace the toggle rule. |
| A repeated up/down while a blind moves | Not used as stop. There is no stop. |
| What vent does exactly | Sent as is for tilt-open; no tilt position is reported. |
| Blind commands during a weather alarm | Sent (the vendor app sends them too); the server decides. |
| Setpoint echo, and what 18 and 26 do | Echo expected as `TEMPSOLL<n>:<v>`; 18 is shown as Off; 26 is an ordinary target. |
| Pressing the active ventilation level again | Never sent: it might switch ventilation off. |
| What the "All lights" group message does | Unknown, so the button is disabled by default. |
| Doorbell mute echo | Expected as `MUTE<n>:00|01`. |
| Dimmer lights | No dimmer exists here; the entity follows the vendor JavaScript. |

Also untested on a real box: the dashboard in a browser (layout and tap
targets on the plan), the device-area dialog for the new devices, and the
`update` command.

### First live test

Install with `DRY_RUN = True` (the last commit of the branch sets it).
`./scripts/ha-live status` then shows "(dry run ...)". Commands are logged
as `dry run: would send <name> (<wire>)` and not sent, so every entity can
be exercised with `ha-live call`. Revert that commit before real use.
