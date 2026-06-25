# Home Assistant Smart Place plugin — POC design

This document captures the research, alternatives, and decisions for the v1 POC of a
Home Assistant plugin that integrates the [Smart Place](https://www.smartplace.ch)
residential automation system. It is the input to the implementation phase.

> **Scope of v1 POC**
> Stand up the project skeleton, prove out the connection to the Smart Place
> back-end via WebSocket, and observe device/system state messages. Command
> sending is implemented in the client library but is **not** the focus of v1
> end-to-end behaviour.

---

## 1. Smart Place — what we know about the protocol

This section records observed protocol facts and message shapes. The
authoritative connection algorithm lives in §6.2 so the implementation has a
single source of truth.

| Item | Shape | Direction | Notes |
| ---- | ----- | --------- | ----- |
| Bootstrap page | `https://spr1.smartplace.ch:8770/Start5?<TOKEN>` | HTTPS GET | Serves an SPA (Mojolicious/Perl back-end). JS reads `window.location.search` to recover `<TOKEN>` and then opens the discovery WS. Useful for diagnostics, but not required by the client connection flow. |
| Discovery WS endpoint | `wss://spr1.smartplace.ch:8770/StartAppExt/?TOKEN=<TOKEN>` | client → server | Opened by the Start5 page on load. Use browser-compatible `Origin` and `User-Agent` headers. |
| SSL route | `GoToLinkSSL:<host>:<port-or-port/path>:<token2>` | server → client | Tells the client which installation-specific host/port/path/token to use. The observed route was `GoToLinkSSL:spr1.smartplace.ch:<port>/Start1:Leer`. |
| Legacy route | `GoToLinkOLDSYSTEM:<token2>` | server → client | Redirects to legacy server `spr0.smartplace.ch:8770/Start2?<token2>`. Observed in Start5 JavaScript, not yet as a live frame. |
| Offline marker | `HostNotOnline` | server → client | The user's installation is offline. Observed in Start5 JavaScript, not yet as a live frame. |
| Routed page | `https://<host>:<port>/<path>` or `https://<host>:<port>/Infoboard1?<token2>` | HTTPS GET | Browser loads this in an iframe after discovery. The observed `Leer` route used `/Start1`. |
| App WS endpoint | `wss://<host>:<port>/UpdatenLS` | client → server | Real app channel found in the routed page. Use the routed page origin as the WebSocket `Origin`. |
| Global config read | `GiveMeGlobalConfig` → `EINSTELLUNGENGLOBAL>...` | bidirectional WS text | First frontend bootstrap message. The observed response was a `>`-delimited text frame with 6 fields: language, standby, brightness, screensaver mode, screensaver start, screensaver duration. |
| Status-list read | `GiveStatusListe` → `StatusListe>...` | bidirectional WS text | Second frontend bootstrap/read message. The observed response was a `>`-delimited text frame with 3 fields. This did not emit separate per-device state frames during the capture window. |

### 1.1 Live connection probe, 2026-05-28

The local `access-url-secret.txt` URL resolves to `spr1.smartplace.ch:8770`
(`62.138.185.23`). The first probe attempts timed out at TCP connect, before
HTTP or WebSocket headers could matter. Later redacted probes from the same
environment succeeded, so the service should be treated as intermittently slow
to accept TCP connections rather than simply unreachable.

The connection sequence in §6.2 was verified with code. A direct discovery WS
connect using only the token, without fetching `Start5` first, returned the
normal `GoToLinkSSL:.../Start1:Leer` route. A passive app WS listen for roughly
two minutes produced no server text frames. In a follow-up probe, the
frontend's initial read messages were sent:

- `GiveMeGlobalConfig` returned one `EINSTELLUNGENGLOBAL>...` text frame.
- `GiveStatusListe` returned one `StatusListe>...` text frame.

No additional app frames arrived during roughly 100 seconds after those
responses. The integration should therefore send these bootstrap read messages
after `/UpdatenLS` connects; it should not expect the server to push initial
state on a totally silent connection.

**Auth model:** a single long-lived URL token. No OAuth, no login, no refresh
flow observed. The token is the only secret needed.

**Treat as secret:** the token (and the full URL that embeds it).
It is **never** to be written to disk, committed to git, logged in plain text,
or sent to third-party tools/services. It is held only in:

- the HA config entry (encrypted by HA's storage layer, stored under
  `~/.storage/core.config_entries`),
- environment variables in the local shell (`.env` is gitignored), or
- transient memory while the client runs.

### 1.2 Prior art and integration-path survey, 2026-05-28

Before designing the WS reverse-engineering path we searched the web for any
existing implementation or public protocol description of smartplace.ch.
Summary of what is — and isn't — out there:

| Source searched | Result |
| --------------- | ------ |
| Home Assistant integrations index, HACS, PyPI, GitHub | **No existing smart PLACE integration.** No PyPI package, no HACS component, no GitHub project that targets smartplace.ch's WebSocket protocol. |
| openHAB bindings, ioBroker adapters, Loxone, FHEM | None for smart PLACE. (Loxone has community bindings; smart PLACE doesn't.) |
| `github.com/SmartPlace` org | Unrelated. A Paris email-infrastructure outfit; last activity 2014. Two repos (`LogReporting`, `posty_ansible`). Not the Swiss building-automation company. |
| Other "Smart Place" / "SmartPlace" products | All unrelated: `smartplacetech.com`, `smartplaceusa.com`, `smartplace.am`, an Apple Store app, a Google Play app from a "Home Manager" developer. None document the smartplace.ch protocol. |
| Protocol identifiers (`StartAppExt`, `GoToLinkSSL`, `UpdatenLS`, `GiveMeGlobalConfig`, `StatusListe`, `EINSTELLUNGENGLOBAL`) | No useful public matches found during the survey. Treat these as vendor-internal frontend/backend identifiers. |
| smartplace.ch downloads page | Marketing material and video tutorials only. No API/protocol docs, no developer guides, no SDK, no third-party-integration documentation. |
| smartplace.ch installation-partner page | 404 at the URL discovered via search. No public partner documentation. |
| Vendor terms of service | Explicitly state smart PLACE AG "is not responsible for the configuration or installation of third-party systems" — third-party integration is officially the customer's responsibility. |

**Significant finding — a paid, vendor-supported alternative exists:**
HORNBACH sells a "[smart PLACE Lizenz API](https://www.hornbach.ch/de/p/smart-place-lizenz-api/12022089/)"
for CHF 139. Per the listing, it enables controlling devices and functions
via "WebRequests" (i.e. plain HTTP) — almost certainly a different surface
from the WebSocket the frontend uses. The product page contains no protocol
detail, endpoint list, auth model, rate-limit info, or documentation links;
documentation presumably comes with the license and/or via the installer.

**Decision to stick with the WS path for v1 POC:**

| Aspect | WS reverse-engineering (this design) | Paid HTTP API license |
| ------ | ------------------------------------ | --------------------- |
| Cost | None | CHF 139 + presumed installer involvement |
| Setup | Works with the user's existing app URL/token | Requires purchase + onboarding |
| Documentation | None — discovered live | Presumed available with license |
| Vendor support | None; officially unsupported third-party integration | Yes, presumed |
| Update semantics | Persistent WebSocket; bootstrap reads verified; live change push still to be captured | Unknown — likely request/response if it is plain HTTP |
| Risk of silent breakage on firmware update | High — internal protocol can change with no notice | Lower — paid customers are likelier to get notice |
| Suitability for v1 POC | Good — zero-cost path to validating "can we extract state for HA from this at all?" | Premature commitment before we know whether the internal WS path is insufficient |

We pick the WS path for v1 because it lets us prove end-to-end connectivity
and state extraction with zero purchase and zero vendor contact. If the WS
path proves too painful — frequent breakage, missing state, no write
capability we can reverse-engineer safely — buying the
licensed API and rewriting the transport layer is a discrete, well-scoped
fallback. The protocol module (§2) is the natural seam: swap the I/O source
and most parsing/dispatch code in `protocol.py` becomes irrelevant, but
the entity-mapping layer in `custom_components/smart_place/` stays put.

**Implication for the parser:** since no second source confirms our reading
of the wire format, every message shape we add to `protocol.py` should cite
the live capture or the JavaScript source that motivated it (a `# observed:`
comment with the date and frame). When the protocol changes, we should be
able to diff capture-to-capture and see exactly what moved.

---

## 2. Architecture

The work is split into two cleanly separated Python packages so the WebSocket
client can be exercised both inside Home Assistant and standalone from a CLI.

```text
repo-root/
├── smart_place_client/             # Standalone async library (no HA dependency)
│   ├── __init__.py
│   ├── protocol.py                 # COMPONENT 1 — common code shared by HA and standalone.
│   │                               # Pure parsing/state: feed bytes in, get parsed events out.
│   │                               # No I/O, no aiohttp, no Click. Exports message dataclasses
│   │                               # (GoToLinkSSL, GlobalConfig, StatusListe, …),
│   │                               # parse_frame() / encode_frame(), and SessionState (the
│   │                               # discovery → routed-page → app-WS state machine).
│   │
│   └── client.py                   # COMPONENT 2 — owns I/O. One SmartPlaceClient class with
│                                   # two classmethod constructors:
│                                   #
│                                   # SmartPlaceClient.live(token, session=...):
│                                   #   - discovery WS → parse routing frame → routed page GET
│                                   #     → app WS → bootstrap reads → loop dispatching
│                                   #     incoming frames through protocol.parse_frame().
│                                   #   - Sends from stdin / HA action handlers go out the WS.
│                                   #   - Reconnect with backoff + jitter (live-only branch).
│                                   #
│                                   # SmartPlaceClient.replay(path):
│                                   #   - Reads ndjson fixture, iterates the server-direction
│                                   #     frames, calls the SAME dispatch as live with optional
│                                   #     per-frame asyncio.sleep based on captured timestamps.
│                                   #   - Sends are accepted but appended to an in-memory log;
│                                   #     fixtures are immutable and the real server is not contacted.
│                                   #
│                                   # Click CLI lives at the bottom of this file behind
│                                   # `if __name__ == "__main__":` (HA never imports Click).
│                                   # Surface:
│                                   #   - `--live`            (mode flag, no value)
│                                   #   - `--replay <file>`   (mode flag, takes a path)
│                                   #   - `--capture <file>`  (optional tee, valid with either mode)
│                                   # Exactly one of `--live` / `--replay` must be passed;
│                                   # Click enforces the mutual exclusion and the requirement.
│
├── custom_components/smart_place/  # Home Assistant integration (thin wrapper)
│   ├── __init__.py                 # async_setup_entry, background task lifecycle.
│   │                               # Instantiates SmartPlaceClient.live(token=...) and
│   │                               # subscribes entity handlers to its event stream.
│   ├── manifest.json
│   ├── config_flow.py              # UI flow to capture the token
│   ├── const.py
│   ├── coordinator.py              # (Optional) DataUpdateCoordinator for fallback
│   ├── sensor.py / switch.py / …   # Entity platforms (added incrementally)
│   └── strings.json
│
├── tests/
│   ├── test_protocol.py            # Pure-function tests of parse_frame / encode_frame.
│   ├── test_client.py              # SmartPlaceClient(replay=fixture, ...) end-to-end tests.
│   └── fixtures/
│       └── *.ndjson                # Captured WS frames (redacted of tokens)
│
├── scripts/
│   ├── lint                        # Ruff format + check
│   ├── test                        # Pytest with coverage
│   └── setup                       # uv-based env bootstrap
│
├── .github/workflows/              # CI: hassfest, HACS, ruff, pytest
├── .vscode/                        # Recommended extensions + launch config
├── .claude/                        # Project-specific Claude Code skills
├── pyproject.toml                  # uv + ruff + pyright + pytest config
├── hacs.json
├── .gitignore
├── .ruff.toml
└── README.md
```

### Why this split

- The **client library** has no `homeassistant` import. It can be `pip install`-ed
  on its own, used from a Jupyter notebook, dropped into a different consumer
  in the future, and tested in isolation in <1s.
- The **HA integration** is the thin "translate protocol to entities" layer.
  This is the conventional HA pattern (see e.g. `pyloadapi`/`hass-loadapi`,
  `aiohue`/`hue` integration). When the integration grows complicated we can
  later cut the client out into its own PyPI package and reference it as a
  `requirements` entry in `manifest.json`.
- The **CLI** gives us a way to iterate on the protocol without running HA —
  the primary developer-experience benefit asked for in the planning
  conversation.
- The **two-file client (protocol + client)** maximises code reuse between
  the HA integration and the standalone CLI: both import the same
  `SmartPlaceClient`, just constructed via `.live(...)` or `.replay(...)`.
  The dispatch / parsing / state code is identical across modes; only the
  I/O source differs. We deliberately use two classmethod constructors on a
  single class rather than a Transport abstraction with two implementations
  — the conditionals around the I/O boundary are the simplest expression of
  "same code, two sources" for the v1 POC. **If the conditional branches
  grow**, treat that as a warning: it means the replay path is diverging
  from the live path, so the tests are exercising less and less of what
  production runs. Address by moving the difference back to the I/O
  boundary, not by adding more branches.

---

## 3. Technology choices

| Concern | Choice | Rationale |
| ------- | ------ | --------- |
| Language | **Python 3.13+** | HA is Python; no reason to swap. 3.13 is the floor for current HA core. |
| Concurrency | **asyncio** | HA's event-loop model; aiohttp is async; saves a thread. |
| WS client | **`aiohttp.ClientSession.ws_connect()`** | Already a HA core dependency — no extra wheel. Sufficient features. We add our own reconnect/backoff (see §6). |
| HTTP client | **aiohttp** | Same session, same loop, same connection pool. |
| CLI | **Click** | Mature, terse, good UX. CLI surface is small: exactly one of `--live` (no value) or `--replay <file>` is required, plus an optional `--capture <file>` tee. Lives at the bottom of `client.py` behind `if __name__ == "__main__":` so HA never imports it. |
| Project tooling | **uv** | Fast venv/install; modern Python project default. |
| Linter / formatter | **Ruff** | What HA core uses. One tool, fast. |
| Type checker | **Pyright** | Stricter than mypy in practice; what the modern blueprint uses. Runs in CI and in VS Code via Pylance. |
| Tests | **pytest** + **pytest-asyncio** | Standard. Pure-client tests don't need any HA test plumbing. `pytest-homeassistant-custom-component` (mirrors HA core's `hass`/`hass_ws_client`/`MockConfigEntry` fixtures) is deferred to when HA-level integration tests are added — see §7 "Out of scope for v1". |
| Pre-commit | **pre-commit** | Hooks: ruff (format + check), hassfest, yamllint, end-of-file fixer. |
| Validation | **hassfest GitHub Action** + **HACS Action** | Catches manifest / structure issues in CI. |
| AI tooling | **Claude Code project skills** under `.claude/skills/` | Project-specific conventions (entity naming, protocol gotchas). Anthropic's official HA MCP server is for *consuming* HA from Claude; not useful for *developing* an integration. |
| Scaffold | **`jpawlowski/hacs.integration_blueprint`** as starting point | HA 2026.4+, uv, Ruff+Pyright+pytest preconfigured, includes `AGENTS.md` for AI assistants. We do not literally `Use template` — we cherry-pick its `pyproject.toml`, `scripts/`, and CI workflows into a fresh repo we control. (Skipping its `.devcontainer/`; the developer brings their own HA for manual end-to-end verification.) |
| IDE | **VS Code** | First-class HA + Python + Pyright + Ruff support. Recommended extensions tracked in `.vscode/extensions.json`. |

---

## 4. Secret storage

**Decision:** HA config flow (production) **plus** environment-variable override
(local development only).

| Path | Where the token lives | Used when |
| ---- | --------------------- | --------- |
| HA config flow | `~/.storage/core.config_entries` (HA-owned, file-perm protected) | Normal user setup in the HA UI. |
| `.env` (gitignored) → `SMART_PLACE_TOKEN` env var | Memory of the local shell / dev process only | Local development: the CLI reads from env so we can iterate against the real WS without typing the token every time. |
| **Never** | secrets.yaml committed; logs; PR descriptions; chat to third-party services; test fixtures | — |

Implementation:

- `config_flow.py` validates the token by running the same browser-compatible
  connection flow through the app WS bootstrap reads, with a bounded total
  timeout and retry budget: discovery WS → routed page GET → `/UpdatenLS` WS
  → `GiveMeGlobalConfig` / `GiveStatusListe` responses. This avoids accepting
  a token that can route but cannot open the real app channel. A preliminary
  `Start5` GET is optional diagnostics, not required for validation.
- `manifest.json` declares `config_flow: true` and, provisionally,
  `iot_class: cloud_push`. Revisit `iot_class` if later captures show the app
  channel is only request/response and never pushes live change frames.
- `.gitignore` includes `.env`, `*.env`, `config/`, `.storage/`, and `access-url-secret*`.
- The existing `access-url-secret.txt` in the repo root is for one-time
  reference during early development and **must be deleted** (or moved to
  `.env`) before the first commit. It is added to `.gitignore` regardless.
- All logging uses a token-redacting filter (see `client.py`) so the WS URL is
  never logged in full.

---

## 5. Testing strategy

Three layers, in order from fastest/cheapest to slowest/most-realistic.
Two things are intentionally *not* in v1 and will be added later if needed:
HA-level integration tests (`pytest-homeassistant-custom-component`) and a
scripted devcontainer end-to-end harness. End-to-end verification in HA is
done manually, against whatever HA instance the developer already has.

### 5.1 Pure client unit tests (primary feedback loop)

- pytest + pytest-asyncio.
- Two layers of tests against the same code production runs:
  - **`test_protocol.py`** calls `parse_frame()` / `encode_frame()` directly
    on byte strings. Pure functions, no async, instant. Covers happy path,
    `HostNotOnline`, legacy redirect, malformed frames, bootstrap message
    encoding, token redaction in logs.
  - **`test_client.py`** instantiates `SmartPlaceClient.replay(<fixture>)`
    and asserts on the event stream it emits. Same dispatch loop production
    uses — no mock WS server, no localhost port; the only difference is that
    incoming frames come from a fixture iterator instead of an aiohttp WS.
- Target: full suite runs in **< 2 seconds**, on every save.

### 5.2 Captured frame replay

- `sp-cli --live --capture tests/fixtures/session1.ndjson` runs a normal live
  session against the real server and tees both directions (server → client
  *and* client → server frames) to a newline-delimited JSON file, with tokens
  redacted on write.
- `sp-cli --replay tests/fixtures/session1.ndjson` runs the same
  `SmartPlaceClient` but with its I/O source pointed at the fixture instead
  of an aiohttp WS. The client iterates the *server-direction* frames from
  the fixture and dispatches them through the same parsing/state code that
  live mode uses. The live smartplace.ch service is not contacted — replay
  is a fully offline loop, and there is no localhost server in the picture
  either. We deliberately do **not** replay the captured client-side frames
  back at the real server, since that would re-issue whatever device commands
  the original session contained.
- Replay fixtures are committed; they let us evolve the parser without ever
  re-touching the live system. Both `sp-cli --replay` and `test_client.py`
  drive the same `SmartPlaceClient.replay(...)` code path — one replay
  implementation, two consumers (CLI for humans, pytest for assertions).

### 5.3 Live smoke test (occasional)

- `sp-cli --live` against real `spr1.smartplace.ch` with `SMART_PLACE_TOKEN`
  set. Bidirectional: server frames print to stdout, lines typed on stdin
  are sent. There is **no software guard** against accidental sends — the
  safety is behavioural: if you don't want to send anything, don't type
  anything. To exercise send paths without touching real devices, run
  `sp-cli --replay <fixture>` instead — sends are accepted but logged
  to memory, not transmitted anywhere.
- Pair with `--capture <file>` when running `--live` so the session is
  preserved as a fixture for later parser work and replay-based testing.

---

## 6. Persistent connection

### 6.1 Lifecycle

```text
HA boots
 └─ async_setup_entry(hass, entry)
     ├─ client = SmartPlaceClient.live(token, session=async_get_clientsession(hass))
     ├─ entry.async_create_background_task(hass, client.run(), "smart_place_ws")
     └─ register entity platforms (sensor, switch, …) — they subscribe to client events

HA shuts down / config entry removed
 └─ async_unload_entry → client.aclose() → background task cancelled cleanly
```

`entry.async_create_background_task` is the right API in 2026: HA owns the
task, cancels it on unload, and warns if it leaks. No `hass.async_create_task`
(which can keep HA alive at shutdown), no bare `asyncio.create_task`.

### 6.2 The `run()` loop

Pseudocode:

```python
async def run(self) -> None:
    backoff = ExponentialBackoff(base=2.0, cap=60.0, jitter=0.3)
    while not self._closing:
        try:
            await self._one_connection_lifetime()
            backoff.reset()
        except asyncio.CancelledError:
            raise
        except SmartPlaceAuthError:
            # Token is bad; surface to HA as a reauth flow, stop trying.
            await self._trigger_reauth()
            return
        except Exception as err:                  # noqa: BLE001
            self._logger.warning("WS dropped: %s; retrying in %.1fs", err, backoff.peek())
            await asyncio.sleep(backoff.next())
```

`_one_connection_lifetime()`:

1. Open discovery WS at `spr1.smartplace.ch:8770/StartAppExt/?TOKEN=<token>`
   with `Origin: https://spr1.smartplace.ch:8770` and the same browser-like
   `User-Agent`. Fetching `Start5?<token>` first is not required; direct
   discovery WS with the token was verified live.
2. Read first frame, expect `GoToLinkSSL:host:port-or-port/path:token2`.
   Handle `HostNotOnline` by raising a typed error (becomes "unavailable"
   entities).
3. Close discovery WS and fetch the routed HTTPS page exactly like the browser
   iframe does:
   - If `token2 == "Leer"` and the route contains a path, use that path. The
     observed live route was `https://host:port/Start1`.
   - Otherwise use `https://host:port/Infoboard1?<token2>`.
4. Open app WS at `wss://host:port/UpdatenLS` with
   `Origin: https://host:port`.
5. Send the read/bootstrap message `GiveMeGlobalConfig`; parse the
   `EINSTELLUNGENGLOBAL>...` response.
6. Send the read/bootstrap message `GiveStatusListe`; parse the
   `StatusListe>...` response.
7. Iterate `ws.receive()` forever; dispatch each frame to subscribed entity
   handlers via an asyncio.Queue / event bus.
8. Heartbeat / ping-pong: rely on aiohttp's built-in WS pong (default heartbeat
   off — turn on, e.g. `heartbeat=30`).
9. On any disconnect or connect timeout, fall through to the outer retry loop.

Connection attempts should use a generous socket-connect timeout, e.g. 30-60 s,
plus the retry loop above. Timeout alone is too blunt: it can reduce false
offline reports, but it cannot recover from a single stuck/lost TCP attempt as
quickly as a bounded retry loop can. One live run saw two app-WS TCP connect
attempts time out after 20 seconds each, then the next attempt complete the
WebSocket handshake in 0.4 seconds. Only treat the installation as offline
after repeated failures or an explicit `HostNotOnline` frame.

### 6.3 Why not also a DataUpdateCoordinator

Smart Place is WS-first and the reconnect logic above can re-run the same
bootstrap reads (`GiveMeGlobalConfig`, `GiveStatusListe`) after every app WS
connect. Adding a polling coordinator would be redundant until we prove those
bootstrap reads do not provide enough current state. If live captures show that
additional periodic read messages are needed, add them over the same WS first;
only add a DataUpdateCoordinator if Home Assistant entity semantics genuinely
need a separate polling abstraction.

### 6.4 Why not an HA add-on

Add-ons buy us: a separate runtime, non-Python tooling, isolation, and the
ability to be consumed by things outside HA. We need none of those — the
client is async Python, fits HA's event loop perfectly, and is consumed only
by HA. An add-on would only add operational and packaging complexity.

---

## 7. Implementation phases

### Phase 0 — scaffolding (no Smart Place code yet)

1. `git init`, set up `.gitignore` (incl. `access-url-secret*`, `.env`, `config/`).
2. Cherry-pick `pyproject.toml`, `.ruff.toml`, `.pre-commit-config.yaml`,
   `scripts/`, and GitHub workflows from the modern blueprint. (Skip its
   `.devcontainer/`.)
3. Create empty `custom_components/smart_place/` with stub `manifest.json`
   (`domain: smart_place`, provisional `iot_class: cloud_push`,
   `config_flow: true`).
4. Get `scripts/lint` and `scripts/test` running green.
5. Hassfest + HACS CI green on first commit.

### Phase 1 — standalone client + CLI (the value-bearing core)

1. `smart_place_client.protocol` (Component 1): message dataclasses, pure
   `parse_frame()` / `encode_frame()`, `SessionState`. Start with
   `GoToLinkSSL`, `GoToLinkOLDSYSTEM`, `HostNotOnline`, `GlobalConfig`
   (`EINSTELLUNGENGLOBAL>...`), and `StatusListe`; extend as live captures
   reveal more.
2. `smart_place_client.client.SmartPlaceClient` (Component 2) with two
   classmethod constructors: `.live(token, …)` and `.replay(path)`. Live mode
   does the full discovery → routed page → app WS sequence and sends the
   bootstrap reads (`GiveMeGlobalConfig`, `GiveStatusListe`). Replay mode
   iterates the server-direction frames from the fixture through the same
   dispatch loop. No reconnect yet.
3. Click CLI at the bottom of `client.py` (behind `if __name__ == "__main__":`):
   one of `--live` or `--replay <file>` is required (mutually exclusive),
   plus `--capture <file>` tee flag. Token from `SMART_PLACE_TOKEN` env var.
   All printed/captured frames have the token redacted.
4. `test_protocol.py` and `test_client.py` — see §5.1. Goal: capture one real
   session via `--live --capture`, replay it in CI via
   `SmartPlaceClient.replay(...)`; parser and dispatch are exercised end-to-end
   without any network or mock server.
5. Add reconnect with exponential backoff + jitter to the live branch; cover
   it with tests that drive a single failing connect attempt followed by a
   replay-mode session.

### Phase 2 — HA integration wrapper

1. `config_flow.py`: single step asks for token, validates with the same
   bounded discovery → routed page → app WS → bootstrap-read flow as §6.2,
   then persists the entry.
2. `__init__.py`: `async_setup_entry` constructs the client and starts the
   background task; `async_unload_entry` cleans up.
3. One trivial entity platform (e.g. a `binary_sensor` reporting WS connection
   health) — proves the end-to-end wiring. No real device mapping yet.
4. Verify manually by dropping `custom_components/smart_place/` into the
   developer's own HA instance and adding the integration through the UI.
   Automated HA-level tests are deferred (see §7 "Out of scope for v1").

### Phase 3 — POC validation

1. Live smoke test against the developer's own HA instance: enter token via
   UI, watch the "WS connection" binary sensor flip on; pull the network for
   30 s, watch it reconnect; restart HA, watch it come back up.
2. Run `sp-cli --live --capture phase3-smoke.ndjson` against the live system
   for a few minutes (no typing — observe only); document every distinct
   message shape we see into a "Protocol notes" appendix.
3. Decision point: which entity types (light, cover, climate, …) are present
   in the captured stream — drives Phase 4 device mapping (out of v1 scope).

### Out of scope for v1

- Device-type mapping (light/switch/cover/etc.) — Phase 4.
- Sending control commands from HA entities — implemented in the client but
  not wired to HA write handlers in v1.
- HACS publication, README polish, screenshots.
- Multi-installation support (multiple config entries).
- Localisation strings beyond English.
- HA-level integration tests with `pytest-homeassistant-custom-component`
  (config-flow tests, entity-creation tests, unload/reload tests). Add when
  the integration grows beyond a single binary sensor.
- Scripted devcontainer / `scripts/develop` harness for booting a local HA.
  Add only if manual end-to-end verification proves to be a real friction
  point.

---

## 8. Decision summary

| # | Question | Decision |
| - | -------- | -------- |
| 1 | Framework / language | Python 3.13 custom integration + asyncio + aiohttp. Standalone client library has two files only: `protocol.py` (pure parsing/state, shared) and `client.py` (`SmartPlaceClient` with `.live(...)` and `.replay(...)` classmethod constructors, plus the Click CLI at the bottom). HA integration is a thin wrapper that instantiates `SmartPlaceClient.live(...)`. |
| 2 | Dev tools | uv, Ruff, Pyright, pytest + pytest-asyncio, pre-commit, hassfest + HACS GitHub Actions, Claude Code project skills under `.claude/`. Scaffold cherry-picked from `jpawlowski/hacs.integration_blueprint` (devcontainer skipped). |
| 3 | Token storage | HA config flow (production) + `SMART_PLACE_TOKEN` env var in the local shell for CLI iteration. `.env` and `access-url-secret*` gitignored. Token-redacting log filter. |
| 4 | Local testing | Layered: `test_protocol.py` (pure functions, instant) + `test_client.py` driving `SmartPlaceClient(replay=fixture)` (same dispatch as live) → manual `sp-cli --replay` for human eyeballing → `sp-cli --live` smoke test against the real server (always bidirectional; safety is behavioural, not a CLI gate). No mock WS server; replay is in-memory. HA-level integration tests and a scripted devcontainer harness are deferred — end-to-end in HA is verified manually. |
| 5 | Always-on connection | `entry.async_create_background_task` runs a reconnect-with-backoff loop inside the integration. `iot_class: cloud_push` is provisional until live-change frames are captured. No add-on. No speculative DataUpdateCoordinator. |

---

## 9. Open questions for implementation

These don't block starting; they get resolved by doing.

1. **Route variants.** One live route returned
   `GoToLinkSSL:<host>:<port>/Start1:Leer`, but the parser must still support
   `GoToLinkSSL:<host>:<port>:<token2>` and the legacy/offline messages already
   seen in the Start5 JavaScript.
2. **State coverage after bootstrap.** The verified bootstrap reads returned
   `EINSTELLUNGENGLOBAL>...` and `StatusListe>...`, but did not produce separate
   per-device frames during the capture. Determine which additional read
   messages, if any, are needed to enumerate and hydrate all HA entities.
3. **State re-delivery on reconnect.** Does re-running the bootstrap reads on
   every fresh app WS provide complete current state, or do we need periodic
   read messages over the same WS?
4. **Write command serialization format.** The frontend uses plain text
   `spsocket2.send(...)` messages. Read/bootstrap messages are visible in
   `javallg.js`; write/control messages still need to be identified and tested
   only against a known-safe target, typed into `sp-cli --live` against the
   live server.
5. **Frame schema.** The JavaScript handlers expect text frames, mostly
   prefix-delimited messages such as `EINSTELLUNGENGLOBAL>...` and
   `leuchte<ID>:<value>`. To be catalogued during Phase 3 from captured frames.

---

## 10. Protocol notes — Phase 3 captures

Recorded during `sp-cli --live --capture` against `spr1.smartplace.ch`.
Each entry cites the capture file under `tests/fixtures/`.

### 2026-05-28 (Phase 3 first capture, ~60 s observe-only)

Capture: `tests/fixtures/phase3-smoke.ndjson` (5 frames).

| Frame | Direction | Notes |
| ----- | --------- | ----- |
| `GoToLinkSSL:spr1.smartplace.ch:38435/Start1:Leer` | S→C | Discovery routed. **Routed port is dynamic per session** (38435 observed; was 8770 in the design's first sketch). Path `/Start1`, `token2="Leer"` confirm the "Leer" branch documented in §6.2 step 3. |
| `GiveMeGlobalConfig` | C→S | Bootstrap read sent by our client. |
| `GiveStatusListe` | C→S | Bootstrap read sent by our client. |
| `EINSTELLUNGENGLOBAL>2>300>0.8>1>300>undefined` | S→C | 6-field global config: language=`2`, standby=`300`, brightness=`0.8`, screensaver_mode=`1`, screensaver_start=`300`, screensaver_duration=`undefined`. Note: brightness is a `0-1` float (not a `0-100` int as one might guess), `undefined` is a literal string (not a JSON null). |
| `StatusListe>Wetter>Tagesverbrauch>` | S→C | 3-field status list: `Wetter` (weather), `Tagesverbrauch` (daily consumption), and an **empty third field**. These look like *info-board tab labels*, not per-device state — which matches DESIGN §9 Q2 ("the bootstrap reads do not produce separate per-device frames"). |

**No spontaneous server pushes** during the observation window (~60 s)
after the bootstrap reads. Consistent with the design's expectation
that the integration cannot rely on idle pushes; we will need to
identify per-device read messages later (DESIGN §9 Q2 / Q5).

**Capture is committed** because it contains no secrets — token never
appears in any frame, and the port / config values / German labels are
not privacy-sensitive. Future captures should be reviewed similarly
before commit.

### 2026-05-28 (Phase 3 second capture — push frames)

A longer capture surfaced three new server-push shapes — all simple
``KEY:<numeric-value>``. They were unknown until promoted to the
``KNOWN_MESSAGES`` registry in ``protocol.py``:

| Wire shape | Parsed as | Meaning (inferred) |
| ---------- | --------- | ------------------ |
| ``TEMPIST<N>:<value>`` | ``Temperature(sensor=N, value=...)`` | "TEMPeratur IST" — current temperature reading from indoor sensor N. One registry entry generalises across sensors (regex ``^TEMPIST\d+:``). Observed sensors so far: 1, 3, 6. |
| ``TEMPOUT:<value>`` | ``OutdoorTemperature(value=...)`` | Current outdoor temperature. |
| ``WINDGESCHWINDIGKEIT:<value>`` | ``WindSpeed(value=...)`` | "Windgeschwindigkeit" — wind speed (unit presumed m/s; not confirmed). |

These are push frames — the server emits them when the value changes,
rather than in response to a read. Same WS, same connection, no extra
request needed. ``cloud_push`` in ``manifest.json`` is justified for
weather entities at minimum.

The generalisation pattern (one registry entry covering all
``TEMPIST<N>`` sensors) is the convention going forward: any indexed
family (``leuchte<id>``, ``blind<id>``, etc.) should be one entry with
a digit-capturing regex.

### Pending captures (post-v1)

- A session that includes a state-change push (e.g. someone toggles a
  light from the SPA while we capture) — to identify per-device frame
  shapes (`leuchte<ID>:<value>`?).
- A `HostNotOnline` capture from an offline installation — to confirm
  the discovery frame matches the literal in the Start5 JavaScript.
- A `GoToLinkOLDSYSTEM` capture from a legacy installation.

### Static frontend command inventory, 2026-05-28

Source: fetched routed `settings.js` and `javallg.js` while a normal
`uv run sp-cli --live` session kept `/UpdatenLS` open. No extra live
commands were sent for this inventory; it is static analysis of
`spsocket2.send(...)` call sites.

`javallg.js` contains 160 `spsocket2.send(...)` call sites: 58 literal
strings and 102 dynamically built strings. Many are real control commands,
so treat this list as candidate protocol notes, not as a safe send list.

Likely read/enumeration commands:

| Area | Candidate command(s) | Hinted response / purpose |
| ---- | -------------------- | ------------------------- |
| Bootstrap | `GiveMeGlobalConfig`, `GiveStatusListe` | Already verified: `EINSTELLUNGENGLOBAL>...`, `StatusListe>...`. |
| Status contents / current states | `StatusInhaltListe`, `GiveMeAllStatesNew`, `GiveMeMainmenu` | `StatusInhaltListe...`, `StatusLinkInhaltFinishedListe`, `StatusInhaltFinishedListe`; `GiveMeAllStatesNew` is sent after status-link content finishes and looks like the best next candidate for broad state hydration. |
| Admin / device inventory | `GiveMeAdminMainmenu`, `GiveMeSzenenIcons`, `GiveMeScreenSaverPics` | Hinted responses include `GiveMeAdminMainmenuFinished`, `CheckLeuchtenValuesFinished`, `CheckJalousienValuesFinished`, `CheckKlimasValuesFinished`, `CheckLautsprecherValuesFinished`, `ReloadSensorFinished`, `SzenenReloadFinished`. May require admin mode / PIN context. |
| Per-device admin detail | `GiveMeAdminSettingsINHALTLeuchten:<id>`, `...S ZENEN:<id>`, `...Jalousien:<id>`, `...Klimas:<id>`, `...MediaPanel:<id>`, `...Mediacenter:<id>`, `...Lautsprecher:<id>`, `...Sensor:<id>` | Looks like detailed configuration fetch for one device/category item. Requires IDs discovered elsewhere. |
| Integrations / APIs | `GiveMeGlobalGsa`, `GiveMeAnbindungen`, `GiveMeMoeglicheAnbindungen`, `GiveMeAPIFuer><id>`, `SPMGiveMeAPIAnbindungsInfos><id>` | Hinted responses include `GlobalAnbindungenBack`, `GlobalAnbindungenBackFinish`, `GiveMeAPIFuerBack`, `GiveMeAPIAnbindungsInfosBack`. May expose integration tokens; do not commit captures blindly. |
| Media / multiroom | `GiveMeGlobalMulti`, `GiveMeMultiInfos><id>`, `GiveMeMultiAllPlayedInfos>`, `GiveMeMultiPlayListRightNowInfos><id>` | Hinted responses include `MediacenterUpdateInfos...` and multiroom/media status. |
| Charts / history | `GiveMeChartSummeWasGenau><diagram_id>`, `GiveMeChartStandsManuell<diagram_id>`, `GiveMeChartValuesFor:<chart_id>:...`, `GiveMeRecordingsHour><ts>`, `GiveMeRecordingsDay><ts>` | Reads chart/history/recording data; parameters come from UI state. |
| Scan/file area | `GiveMeScansAllFirstLevel`, `GiveMeScansOrdner><id>`, `ScanGiveMeFile><id>`, `ScanGiveMeOrdnerEditFile>` | Reads document/file structures. Likely private; avoid fixture commits. |
| Misc system/account | `GiveMeBasicInfos`, `GiveMeRechnungenAbschliessenCount`, `GiveMeToken` | `GiveMeToken` likely returns or refreshes a secret/token; avoid during captures unless needed and redaction is proven. |

Server prefix hints in the JS show likely entity families:

- Lights / outputs: `leuchte<ID>:...`, `DIMleuchte<ID>:...`, `RGBSLIDER<ID>:...`,
  `RGBABLAUF<ID>`.
- Covers/blinds: `JALUP<ID>`, `JALDOW<ID>`, `JALLUE<ID>`, `JALICO...`.
- Climate / sensors: `TEMPIST<ID>`, `TEMPSOLL<ID>`, `FEUCHTEIST<ID>`,
  `KLIMASINFO...`, `FanCoilMode...`.
- Scenes/settings/alarms/audio: `SZENE<ID>`, `SETTINGS...EINAUS`,
  `SETTINGS...SLIDER`, `Alarme...`, `Lautsprecher...`, `Vol<ID>:...`.
- Door/intercom/media controls: `SPRECHEN...`, `MUTE...`, `OEFFNER...`,
  `CALLS...`, media playback/select commands.

Write-looking commands include `leuchte<ID>`, `DIMleuchte<ID>:<value>`,
`JALUP/JALDOW/JALLUE<ID>`, `TEMPSOLL<ID>:<value>`, `SETTINGS...EINAUS`,
`SETTINGS...SLIDER`, `SZENE<ID>`, `OEFFNER<ID>`, media play/select commands,
Smart Garden runtime/threshold setters, and sauna temperature/humidity
setpoints. Do not send these during discovery/state-capture work.

### 2026-05-28 (HAR-derived registry expansion)

A pair of HAR captures from the SPA in a normal browser session
(`spr1.smartplace.ch{,2}.har`, since deleted) surfaced ~58 distinct
server-message stems. They were folded into `KNOWN_MESSAGES` as one
entry per stem; per-id pushes share a single entry via a `\d+` regex
(e.g. one `LightState` entry handles every `leuchte<id>`).

**Parallel app WebSockets.** The browser holds two simultaneous
`wss://<host>:<port>/UpdatenLS` connections during a normal session,
both opened by the SPA's `spsocket2` variable (one per page context):

| Connection | Sends | Role |
| ---------- | ----- | ---- |
| Main (`/Start1`) | `GiveMeGlobalConfig`, `GiveMeBasicInfos`, `GiveStatusListe`, `StatusInhaltListe`, `GiveMeMainmenu`, `GiveMeChartStandsManuell<id>`, `GiveMeGlobalGsa`, `Ping` (heartbeat) | Bootstrap + control plane — receives the full topology (`INHALT*`, `UnterMenu*`, `AllItems*`, chart definitions) plus the broadcast sensor stream. |
| Mediacenter iframe (`/Mediacenter?99`) | `SocketConnected:1`, `GiveMeGlobalConfig`, `GiveMeSPMediacenter`, `GiveMeSpotifyToken>99` | Media subsystem — receives only its bootstrap (`SPOTIFYTOKEN`) plus the shared sensor broadcast. |

Per-device pushes (TEMPIST, leuchte, SZENEN, ...) fan out to **both**
sockets; bootstrap responses go only to the requester. The HA
integration only needs the Main connection — there is no reason to
mimic the media iframe. The two HARs are otherwise structurally
identical (different page-open snapshots of the same session shape).

**New shape families recognised** (one registry entry each unless
noted; per-id families collapse with a `\d+` regex):

- *Singletons*: `BasicInfos` (installation/PII info), `LanguageOptions`
  (wire `GlobalConfig` — name conflict with our existing
  `EINSTELLUNGENGLOBAL` dataclass, so registry name differs from wire),
  `GsaConfig` (LAN/SIP gateway info), `AllItems` (bootstrap device
  dump), `Rain`, `Hail`, `BlindsMaintenance`, `PersonInfo`,
  `ApiToken`, `SpotifyToken`, `MediacenterUpdate`,
  `InvoicesPendingCount`, `OffersCount`, `InvoicesCount`,
  `StatusEntry` (`StatusInhaltListe_<lvl>_<row>_SPtext<id>>...`).
- *Markers* (no payload): `PongOK`, `SocketConnectedFinished`,
  `MainMenuFinished`, `StatusContentFinished`. Stored as
  `NamedFields(name=..., fields=())`.
- *Per-id values* (`prefix<N>:value`): `TemperatureSetpoint`,
  `Humidity`, `ClimateInfo`, `SceneState`, `LightState`, `BlindState`,
  `Volume`, `InfoboardSlot`, `PackageBox`, `ChartTarget`, `WindAlarm`,
  `LightsCentral`, `BlindsCentral`, `SpeakersCentral`, `Mute`,
  `DoorIntercom`, `CallInfo`, `Sound`.
  - Intercom ringing is `DoorIntercom` (`SPRECHEN<n>:ring`). KNOWN
    LIMITATION (live-verified 2026-06-25): there is **no live ring signal
    on the app WS**. `SPRECHEN<n>:ring` is a sticky latch — set once and
    replayed on every bootstrap, never clearing; a repeat ring of the
    same door emits nothing (confirmed by driving the real SPA in a
    browser on both the main and mediacenter sockets). The live ring +
    caller are delivered over **SIP** (a JsSIP INVITE on
    `wss://<GSASERVER>:<port>/asterisk/ws`), which the WS-only client
    doesn't join. `Sound` (`SOUND<n>`) is a notification-sound frame,
    parsed-but-unused — it is **not** the doorbell (no `SOUND` was ever
    seen for a ring). So the intercom sensor's "ringing" is best-effort
    only; a real doorbell entity needs a SIP client (see IMPLEMENT.md).
- *Per-id comma configs* (`prefix<N>:f1,f2,...`): `LightConfig`,
  `BlindConfig`, `ClimateConfig`, `SceneConfig`, `MediacenterConfig`,
  `MediaPanelConfig`, `VolumeConfig`, `LightSubMenu`, `BlindSubMenu`,
  `SpeakerSubMenu`, `QuickStartTile`, `Floorplan`.
- *Charts*: `ChartPointUpdate`, `ChartDefinition`, `ChartStand`,
  `ChartSumResponse`.
- *Other*: `PlaySlot` (`PLAYSLOT-<n><...<...` — `<` delimiter).

`NamedValue` and `NamedFields` gained an optional `index: int | None`
field rather than introducing dedicated indexed dataclasses — keeps
the type union short while letting per-id consumers read
`frame.index` instead of re-parsing.

**Sensitive content surfaced by this expansion:** `BasicInfos`
contains the customer's SPID, creator email, installation tag, and
creation date. `GsaConfig` contains LAN IP and SIP gateway info.
`ApiToken` and `SpotifyToken` carry third-party tokens. None of these
are committed to fixtures; the registry parses them so the dispatch
layer recognises them, but their `value` payloads should not be
logged or persisted without redaction.

### Command registry

All outgoing commands are declared in
``smart_place_client/commands.py`` as ``CommandDefinition`` entries
under the ``Commands`` namespace — mirroring ``KNOWN_MESSAGES`` for
the inbound direction. Each entry pairs:

- a CamelCase ``name``,
- a description of when it's sent and what response it triggers,
- an ``encode`` callable (no-arg for static payloads, parameterized
  for shapes like ``ChartStands`` that embed an id),
- a concrete ``example`` wire string.

Call sites go through the namespace: ``Commands.Mainmenu.encode()``
for static reads, ``Commands.ChartStands.encode(cid)`` for the
chart fetch, ``Commands.OpenFrontDoor.encode()`` for door openers.
Command names mirror the related message names where applicable
(e.g. ``Commands.StatusContent`` is the request that yields
``StatusEntry`` rows; the wire payload ``StatusInhaltListe``
stays inside the encoder).
``KNOWN_COMMANDS`` enumerates the full set for introspection / tests
/ docs.

Per the ``smart-place-observe-only`` memory, declaring a write
command (``Open*``) in the registry doesn't auto-issue it — the
library only sends when an explicit caller invokes ``client.send``.

### Bootstrap command sequence (verified live 2026-05-29)

To get the main stats + chart labels + climate-zone room names the
HA integration needs, the bootstrap is now:

```
                              # → TEMPIST<N> arrives spontaneously
                              #   (broadcast on connect, no command).
                              # → PACKETBOX<N> arrives later via the
                              #   server's broadcast fan-out.

→ StatusInhaltListe           # ← StatusInhaltListe_<lvl>_<row>_… rows,
                              #   each binding a label to a push frame
                              #   (TEMPOUT, REGEN, CHART<id>STAND<n>, …)
                              # ← StatusInhaltFinishedListe (terminator)
→ GiveMeMainmenu              # ← Big config + state dump:
                              #     ChartDefinition (labels/categories
                              #     /units, including charts not
                              #     referenced by StatusEntry rows),
                              #     ClimateConfig (room names),
                              #     LightConfig/BlindConfig/SceneConfig,
                              #     plus initial state: ChartStand
                              #     STAND1 readings, LightsCentral,
                              #     Volume, SceneState.
                              # ← GiveMeMainMenuFinished (terminator —
                              #   also the SessionState BOOTSTRAPPED
                              #   signal, since every config frame an
                              #   HA entity might need has arrived).
→ SocketConnected:1           # Triggers the full broadcast burst:
                              # ← PACKETBOX<N>:<state>, REGEN, HAGEL,
                              #   JALWARTUNG, TEMPOUT, WINDGESCHWINDIGKEIT,
                              #   WINDALARM<N>, LEUCHTENZENTRAL<N>,
                              #   INFOBOARD<N> / INFOBOARD<N>INHALT,
                              #   Vol<N>, PERSINFO, MUTE, KLIMASINFO<N>,
                              #   SPRECHEN / CALLINFO, SceneState etc.
                              # ← SocketConnectedFinished>...

For each unique CHART<id> discovered via either StatusEntry refs
or ChartDefinition frames:
→ GiveMeChartStandsManuell<id>  # ← CHART<id>STAND<n>:<value> per series

Heartbeat (runs for the life of the connection):
→ Ping                        # every 60s; closes the WS if no PongOK
                              #   arrives within 30s so the reconnect
                              #   loop fires (mirrors the SPA's
                              #   StartWebsocketTestMain cadence).
                              # ← PongOK
```

So **three static commands** plus **one command per discovered
chart** at bootstrap, plus the periodic ``Ping`` heartbeat. The SPA
also sends ``GiveStatusListe`` first (one extra round trip yielding
just the status-list column titles) and ``GiveMeGlobalConfig`` —
**neither is required by the server** (verified 2026-05-29) so we
skip them.

`SocketConnected:1` is the gate for the full broadcast burst —
without it the server stays quiet on PACKETBOX / REGEN / HAGEL /
WINDALARM / TEMPOUT / WINDGESCHWINDIGKEIT / LEUCHTENZENTRAL /
PERSINFO / Vol / etc. TEMPIST sensors do still push spontaneously
on change without it. Verified 2026-05-29 via HAR audit (the
trigger turned out to be SocketConnected:1, not "auto-push" as
earlier notes incorrectly claimed) + a live probe confirming the
broadcast burst arrives ~70ms after the send.

`_chase_chart_ids` in `client.py` collects chart ids from both
`StatusEntry` and `ChartDefinition` frames and issues the chart
fetches on either the `StatusContentFinished` or
`MainMenuFinished` marker.

### Write commands (not auto-issued)

The four door-opener commands — ``Commands.OpenGroundFloorEntrance``
(``OEFFNER1``), ``Commands.OpenMailbox`` (``OEFFNER2``),
``Commands.OpenGarageEntrance`` (``OEFFNER3``) and
``Commands.OpenFrontDoor`` (``OEFFNER4``) — are declared in the
command registry so callers can opt in explicitly. Per the
``smart-place-observe-only`` memory, the library never sends them
on its own. The HA integration surfaces them as ``button`` entities
so the user can press one to send the matching command.

---

## 11. HA entity mapping (2026-05-28)

Translates the WS dispatch into the canonical HA entity types. One
device per config entry; all entities share `identifiers={(DOMAIN,
entry_id)}`. Discovery is observation-based: `async_setup_entry`
waits for `wait_for_bootstrap` plus a brief window so the initial
broadcasts and chart-stand replies land before platforms enumerate.
New IDs that arrive after platforms have been forwarded require an
HA reload to surface.

### sensor

| Source frame | Entity | device_class | unit | state_class |
| ------------ | ------ | ------------ | ---- | ----------- |
| `TEMPOUT:<v>` (singleton) | `SmartPlaceOutdoorTemperatureSensor` | `TEMPERATURE` | °C | `MEASUREMENT` |
| `WINDGESCHWINDIGKEIT:<v>` (singleton) | `SmartPlaceWindSpeedSensor` | `WIND_SPEED` | km/h | `MEASUREMENT` |
| `TEMPIST<N>:<v>` + matching `Klimas<N>` zone | `SmartPlaceIndoorTemperatureSensor` (per N) | `TEMPERATURE` | °C | `MEASUREMENT` |
| `StandsSingelChartUpdate<id>:STAND1:<v>` | `SmartPlaceChartSensor` (per non-SUMME chart) | `ENERGY` / `WATER` | kWh / L | `TOTAL_INCREASING` |
| `PERSINFO:<…PIN:N…>` | `SmartPlacePackageDeliveryPinSensor` (singleton) | — | — | (text state) |

Chart device-class and native unit are derived from the
`ChartDefinition` frames that arrive during `GiveMeMainmenu` (the
authoritative source) with fallback to the `unit-KWh` / `unit-l`
tokens embedded in `StatusEntry` references.

Chart **state** is the daily `STAND1` reading (today's consumption
so far). It resets to 0 at midnight; HA treats that as a period
boundary under `TOTAL_INCREASING` and the Energy / Water dashboards
accumulate deltas correctly. The other STAND series (week / month /
year) surface as `stand<N>` attributes.

Chart **names** come from `ChartDefinition.label` — the first
`;`-field of the SingelDiagramm payload, with German tokens
translated (`Elektro` → `Electricity`, `Wärme` → `Heating`,
`Kaltwasser I` → `Cold water I`, etc.) and the trailing site code
(`HH77-14-01`) stripped. SUMME charts (`category == "Summe"`) are
filtered out — we surface the per-meter sub-charts instead, since
the SUMME is just their sum.

**Indoor temperature** sensors are surfaced only when the
matching `Klimas<N>` zone name exists in `state.climate_zones` (the
SPA pairs `TEMPIST<N>` with `Klimas<N>` 1:1). The display name is
the zone's room name with the trailing ``heating`` / ``Heizung``
tag stripped — e.g. `Bedroom temperature`,
`Living room/dining room/kitchen temperature`. Sensors without a
matching zone are dropped (we can't surface a meaningful name).

Parcel deliveries surface as a single `SmartPlacePackageDeliveryPinSensor`
holding just the unlock PIN. We originally assumed `PACKETBOX<N>:<code>`
carried the code while a box was occupied, but live observation
(2026-06-06) disproved it: during a real delivery every `PACKETBOX<N>`
still reported ``Frei`` and the unlock PIN arrived as free text in a
`PERSINFO` banner instead — e.g. "Sie haben eine Lieferung in der
Paketbox. Bitte verwenden Sie den PIN:4489 um diese rauszuholen." The
sensor pulls the digits after ``PIN:`` out of that banner (see
`state.package_delivery_pin`) and reads ``None`` (HA "unknown") when no
delivery is waiting. The full banner text is still available verbatim on
the separate `Personal info` sensor — the duplication is intentional.
The `PACKETBOX<N>` frame is kept in `KNOWN_MESSAGES` so it parses
cleanly (not dumped to `unknown_frames`) but is intentionally mapped to
no entity — occupancy-only at best, pending more observation. This
sensor is created unconditionally rather than observation-gated: a
delivery is transient and rare, so gating on a PIN being present at
setup would mean the entity almost never exists.

**Aggregated rollups** — HA's `group` integration is for
user-defined helpers (UI/YAML); custom integrations can't
programmatically register Group entities, but the idiomatic
alternative is just a normal sensor that reads the same shared
snapshot. One rollup lands in this platform:

- `Weather alarm` — comma-joined active alarms across `Rain`,
  `Hail`, and per-zone `WindAlarm<N>` (e.g. "Hail, Wind alarm zone
  1"). Per-source booleans + the active wind-alarm zone list land
  in attributes.

Indoor-temperature sensors (`TEMPIST<N>`) are intentionally not
exposed — without a per-sensor room label they're not meaningful on
their own. Re-add once the bootstrap response can give us labels.

### binary_sensor

| Source frame | Entity | device_class | Notes |
| ------------ | ------ | ------------ | ----- |
| (WS phase) | `SmartPlaceConnectionSensor` | `CONNECTIVITY` | Diagnostic, always present. |
| `REGEN:<code>` | `SmartPlaceRainSensor` | `MOISTURE` | `00` = off, else on. |
| `HAGEL:<code>` | `SmartPlaceHailSensor` | `PROBLEM` | `00` = off, else on. |
| `JALWARTUNG:<code>` | `SmartPlaceBlindsMaintenanceSensor` | `PROBLEM` (diagnostic) | `00` = off, else on. |
| `WINDALARM<N>:<code>` | `SmartPlaceWindAlarmSensor` (per zone) | `PROBLEM` | `00` = off, else on. |
| `SZENEN<N>:<code>` + `SceneConfig` name | `SmartPlaceSceneSensor` (per scene) | — | `01` = active. Skipped when no name known. |
| `LEUCHTENZENTRAL<N>:<code>` | `SmartPlaceAnyLightOnSensor` (per group) | — | `00` = none on, else at least one. |
| `JALZENTRAL<N>:<code>` | `SmartPlaceAnyBlindClosedSensor` (per group) | — | Best-effort: empty / `00` = none, else some. |

### button

The four `OEFFNER<n>` write commands surface as `Button` entities so
the user can press one from the HA UI to open the corresponding
door. `async_press` calls `client.send(OPEN_<...>)`. If the WS isn't
open yet, the press is logged and skipped — there's no command
queue.

### Polling

Consumption-chart values don't push: the server only emits
`CHART<id>STAND<n>:<v>` in response to an explicit
`GiveMeChartStandsManuell<id>` fetch. `SmartPlaceClient._poll_charts`
re-issues a fetch for every id in `SessionState.chart_ids` every
`CHART_POLL_INTERVAL` (default 60 s). The poll task is started by
`_run_live_once` after the bootstrap reads and cancelled when the
connection ends, so it dies cleanly on reconnect.

### Chart identification — what each `StandsSingelChartUpdate<id>` represents

Probed live 2026-05-28 with read-only
`GiveMeChartSummeWasGenau><id>` (returns a category string per
chart):

| Chart id | Unit | Category | Inferred kind |
| -------- | ---- | -------- | ------------- |
| 49 | KWh | (empty) | Electricity |
| 144 | KWh | (empty) | Electricity |
| 337 | l | `Wasser` | Water |
| 595 | l | `Wasser` | Water |

`GiveMeChartSummeWasGenau` only labels water charts explicitly;
electricity charts return an empty category and the kind is
implied by the `unit-KWh` token. We don't yet have a more
specific label (e.g. cold vs hot water, house vs PV electricity);
that would require either probing `GiveMeSingelDiagramm<id>`
(untested guess) or reading the SPA's chart-detail page. The
sensor entity names are kept generic — "Electricity chart 49",
"Water chart 337" — until labels are confirmed.

### Notably **not** mapped (yet)

- `ChartTarget` / `ChartDefinition` / `ChartStand` — would surface
  per-chart goals/series metadata; not needed for v1.
- `LightState` / `BlindState` / `ClimateInfo` / `SceneState` — the
  client parses them but the integration doesn't expose them as HA
  entities because we have no write coverage yet and the user wants
  observe-first.
- `BasicInfos` / `GsaConfig` — contain PII / LAN info and aren't
  user-facing state.
