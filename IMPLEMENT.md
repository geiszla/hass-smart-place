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

