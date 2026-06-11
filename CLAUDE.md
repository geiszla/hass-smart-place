# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration for the Smart Place (smartplace.ch) Swiss residential automation system, plus a standalone async WebSocket client library that can be exercised from a CLI without running HA. The integration is deployed manually to a real Home Assistant OS box (Raspberry Pi) that controls a real building.

## Safety — read first

- **Observe-only during development.** The Smart Place server is the user's real building. Never send live commands: the door buttons / `OEFFNER<n>` commands open real doors. Declaring a command in `commands.py` is fine; *issuing* one is not.
- The `sp-cli --live` CLI is bidirectional with **no software gate** — anything typed on stdin is sent to the real server. Use `--replay` mode to experiment with sends (they're logged, never transmitted).
- `./scripts/ha-live` deliberately has no service-calling command (e.g. `button.press`); do not work around that via its `get`/`ws` escape hatches or curl.
- Secrets live in `.env` (gitignored, auto-loaded): `SMART_PLACE_TOKEN` (Smart Place URL token), `HASS_TOKEN` + `HASS_URL` (HA long-lived admin token). Never echo or commit them; the library installs a log filter that redacts the token.

## Commands

```bash
./scripts/setup        # uv sync --all-extras (uv-based project, Python 3.13+)
./scripts/lint         # ruff format + ruff check --fix (autofix)
./scripts/lint-check   # CI version: ruff format --check, ruff check, pyright
./scripts/test         # uv run pytest --cov; forwards args
./scripts/test tests/test_protocol.py -k mojibake   # single test / filter
uv run pytest tests/test_client.py::test_name       # equivalent direct form

uv run sp-cli --replay tests/fixtures/bootstrap.ndjson  # offline replay, no network
uv run sp-cli --live [--capture file.ndjson]            # live server — observe-only!

./scripts/ha-live status|entities|logs|state <id>|watch|reload|restart
                       # query the deployed integration on the real HA box
                       # (see .claude/skills/ha-live/SKILL.md)
```

Tests are pure/offline (pytest + pytest-asyncio in auto mode + ndjson replay fixtures); no HA test plumbing or network needed.

Deploying to the Pi is separate from git: `scp -r custom_components/smart_place root@homeassistant.local:/config/custom_components/` then `./scripts/ha-live restart` (changed Python requires a core restart; `reload` only re-runs entity discovery). "Push" means git push to GitHub, not deploy.

## Architecture

Two cleanly separated layers (DESIGN.md §2):

1. **`custom_components/smart_place/smart_place_client/`** — standalone client library, **no `homeassistant` imports** (importable from notebooks/CLI/pytest). It is physically nested inside the integration so HACS ships it, but the hatch `sources` mapping in `pyproject.toml` exposes it as top-level `smart_place_client` in the dev install — import it by that name in tests and the CLI.
   - `protocol.py` — pure parsing/encoding, no I/O. Frame dataclasses, `encode_frame`, the `SessionState` phase machine (discovery WS → `GoToLinkSSL` routing frame → routed page GET → app WS → bootstrap), mojibake repair, unit-hint parsing.
   - `messages.py` — inbound registry: one `MessageDefinition` per known wire-frame shape + the `parse_frame` dispatcher. To support a new server message, append a definition here using the helper parsers from `protocol.py`; each entry's example string must match its own pattern (enforced by `tests/test_protocol.py`).
   - `commands.py` — outgoing registry (`Commands` namespace), the mirror of `messages.py`. Declaring a command never auto-issues it; sending requires an explicit `client.send(...)`.
   - `state.py` — `SmartPlaceState`, a pure value-object snapshot. Feed every parsed `ServerFrame` through `.apply()`; it accumulates temperatures, weather, alarms, chart series.
   - `client.py` — owns **all** I/O. `SmartPlaceClient.live(...)` and `.replay(path)` are two constructors on one class sharing one dispatch loop; only the frame source differs. The Click CLI lives at the bottom of this file (HA never imports Click). Reconnect with exponential backoff + jitter, 60 s heartbeat, chart polling.
   - **Invariant:** if the live/replay branches start growing extra parsing or state logic, the replay tests are diverging from production — move the difference back to the I/O boundary instead of adding branches.

2. **`custom_components/smart_place/*.py`** — thin HA wrapper. `__init__.py` builds `SmartPlaceClient.live()`, runs it as a config-entry background task, and fans each frame out: `state.apply(frame)` then notify per-entity listeners. Platforms: `sensor`, `binary_sensor`, `button`, `camera`. Key behaviours:
   - **Entity discovery is one-shot**: setup waits for bootstrap plus a `SETUP_OBSERVATION_WINDOW` (2 s, `const.py`) for the broadcast burst, then platforms enumerate whatever the server pushed. IDs that appear later need an integration reload to surface.
   - **Availability** = WS connected AND session phase in `HEALTHY_PHASES`. Everything goes *Unavailable* on disconnect except the Connection diagnostic binary sensor, which is always available so automations can alert on it.
   - Entities are grouped into sub-devices (per-room climate, category devices) hung off the main device via `via_device`.
   - Charts (electricity/water) aren't pushed by the server; the client re-issues `GiveMeChartStandsManuell<id>` every `CHART_POLL_INTERVAL` (60 s).
   - Token rejection triggers HA's re-auth (Repairs) flow via the `on_reauth` callback; `config_flow.py` validates a token by doing a real connect.

The wire protocol is a German-named text-frame protocol (e.g. `TEMPIST<n>`, `REGEN`, `PACKETBOX<n>`) reverse-engineered from the vendor SPA — DESIGN.md §1 and §10 hold the protocol notes and live-capture logs; §11 holds the frame→entity mapping.

## Tooling notes

- Pyright only type-checks the client library + tests. The HA-facing modules are excluded in `pyproject.toml` (they'd need `homeassistant` installed, which isn't a dep); hassfest CI and the live box cover them. Don't "fix" this by adding `homeassistant` to deps.
- Ruff is the formatter and linter (line length 120, Google docstrings); config and the deliberate ignore list are in `pyproject.toml`. CI = lint-check + pytest + hassfest/HACS validation.
- Fixtures in `tests/fixtures/*.ndjson` are captured live frames (token-redacted). When a fixture disagrees with live behaviour, trust a fresh live capture over the fixture.

## Reference docs

- `DESIGN.md` — architecture, decision log, protocol notes, entity mapping. Update it when decisions change.
- `IMPLEMENT.md` — running implementation log, including divergences from the design.
- `DEPLOY.md` — end-to-end Pi install/upgrade instructions and what healthy looks like in the UI.
- `.claude/skills/ha-live/SKILL.md` — full `ha-live` command reference and verification recipes for the deployed instance.
