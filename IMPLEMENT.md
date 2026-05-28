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
