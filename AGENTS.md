# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## What this is

A Home Assistant custom integration for the Smart Place (smartplace.ch) Swiss residential automation system, plus a standalone async WebSocket client library that can be exercised from a CLI without running HA. The integration reads sensors and controls lights, sockets, blinds, heating, scenes and ventilation. It is installed through HACS on a real Home Assistant OS box (Raspberry Pi) that controls a real building. It needs Home Assistant 2026.11 or newer.

## Safety — read first

- **Never send a command to the real building during development.** The Smart Place server is the user's real flat. Every write command acts on it: lights, sockets, blinds, heating setpoints, scenes, ventilation levels, group buttons, the doorbell mute, and the door buttons (`OEFFNER<n>` opens real doors). Declaring a command in `commands.py` is fine; *issuing* one is not.
- `sp-cli --live` drops lines typed on stdin unless it is started with `--allow-send`. Do not use `--allow-send`. Use `--replay` mode to experiment with sends (they are recorded, never transmitted).
- **Dry run:** `DRY_RUN` in `custom_components/smart_place/const.py`. When `True`, `client.issue()` logs `dry run: would send <name> (<wire>)` at WARNING instead of transmitting a write command, and the Connection binary sensor carries the attribute `dry_run: true`. It must be `False` in a release.
- `./scripts/ha-live call` is the only way to call a service on the box from here. It refuses unless the Connection sensor reports `dry_run: true`, and it always refuses a door button. `./scripts/ha-live ws` refuses `call_service`, `execute_script`, `fire_event` and similar commands. Do not work around either with curl or another client.
- Secrets live in `.env` (gitignored, auto-loaded): `SMART_PLACE_TOKEN` (Smart Place URL token), `HASS_TOKEN` + `HASS_URL` (HA long-lived admin token). Never echo or commit them. The frames `144Update:` / `145Update:` carry the installation's remote-access passwords: the parser drops their value and the client redacts them in logs, captures and the unknown-frame log; never print or store them.
- **The repository is public.** Commits and test fixtures must contain no token, password, LAN address, installation tag, customer number, parcel PIN or info-board link. `tests/test_fixtures.py` guards the fixtures.
- **Keep the load on the box low** (it is a Raspberry Pi): history queries only for a few hours, one or two named entities, with `minimal_response` and `no_attributes`; use the hourly statistics query for longer ranges; one query at a time.

## Commands

```bash
./scripts/setup        # uv sync --all-extras (uv-based project, Python 3.13+)
./scripts/lint         # ruff format + ruff check --fix (autofix)
./scripts/lint-check   # CI version: ruff format --check, ruff check, pyright
./scripts/test         # uv run pytest --cov; forwards args
./scripts/test tests/test_protocol.py -k mojibake   # single test / filter
uv run pytest tests/test_client.py::test_name       # equivalent direct form

uv run sp-cli --replay tests/fixtures/bootstrap.ndjson  # offline replay, no network
uv run sp-cli --live [--capture file.ndjson] < /dev/null  # live server — observe only

./scripts/ha-live status|entities|logs|state <id>|watch|reload|restart|update|call
                       # query the installed integration on the real HA box
                       # (see .claude/skills/ha-live/SKILL.md)
```

Tests are pure/offline (pytest + pytest-asyncio in auto mode + ndjson replay fixtures); no HA test plumbing or network needed.

Installing on the box is separate from git: the box has no SSH and takes the integration from GitHub through HACS. After a push, `./scripts/ha-live update [--version <git ref>]` installs it and `./scripts/ha-live restart` loads it (changed Python requires a core restart). "Push" means git push to GitHub, not install; do neither unless asked.

## Architecture

Two cleanly separated layers (DESIGN.md §2):

1. **`custom_components/smart_place/smart_place_client/`** — standalone client library, **no `homeassistant` imports** (importable from notebooks/CLI/pytest). It is physically nested inside the integration so HACS ships it, but the hatch `sources` mapping in `pyproject.toml` exposes it as top-level `smart_place_client` in the dev install — import it by that name in tests and the CLI.
   - `protocol.py` — pure parsing/encoding, no I/O. Frame dataclasses, `encode_frame`, the `SessionState` phase machine (discovery WS → `GoToLinkSSL` routing frame → routed page GET → app WS → bootstrap), mojibake repair, unit-hint parsing. `SessionState.reset_connection()` clears the per-connection fields before every connection attempt.
   - `messages.py` — inbound registry: one `MessageDefinition` per known wire-frame shape + the `parse_frame` dispatcher. To support a new server message, append a definition here using the helper parsers from `protocol.py`; each entry's example string must match its own pattern (enforced by `tests/test_protocol.py`).
   - `commands.py` — outgoing registry (`Commands` namespace), the mirror of `messages.py`. Every command has a typed, range-checked encoder, `example_args`, and a `write` flag (default true). Declaring a command never auto-issues it.
   - `devices.py` — frozen dataclasses for lights, blinds, climate zones, scenes, sub-menus and floor plans, parsed from the config frames. Config positions are `top,left` (vertical first).
   - `state.py` — `SmartPlaceState`, a pure value-object snapshot. Feed every parsed `ServerFrame` through `.apply()`; it returns whether anything changed. `clear_control_values()` empties the control values when the connection drops.
   - `expected.py`, `motion.py`, `conversions.py`, `rooms.py`, `scenes.py`, `plan.py` — pure helpers with injectable clocks: expected state after a command, blind direction from positions, vendor-to-HA value conversions, room suggestion, scene classification (trigger / switch / level group), plan geometry and the dashboard configuration builder.
   - `tariff.py` — EWZ rate table, tariff calendar, and `MonthToDateCost`.
   - `client.py` — owns **all** I/O. `SmartPlaceClient.live(...)` and `.replay(path)` are two constructors on one class sharing one dispatch loop; only the frame source differs. `send()` transmits on the app WebSocket only and raises `SmartPlaceNotConnectedError`; `issue()` is the entry point for registered commands (audit log, dry run). Also: reconnect with exponential backoff + jitter, 60 s heartbeat, chart polling, plan image fetch, redaction (`scrub_secrets`). The Click CLI lives at the bottom of this file (HA never imports Click).
   - **Invariant:** if the live/replay branches start growing extra parsing or state logic, the replay tests are diverging from production — move the difference back to the I/O boundary instead of adding branches.

2. **`custom_components/smart_place/*.py`** — thin HA wrapper. `__init__.py` registers the main device and the actions, builds `SmartPlaceClient.live()`, runs it as a config-entry background task, and fans each frame out: `state.apply(frame)` then notify per-entity listeners. Runtime data is `entry.runtime_data` (`SmartPlaceData`). Platforms: `binary_sensor`, `button`, `camera`, `climate`, `cover`, `fan`, `image`, `light`, `scene`, `sensor`, `switch`. Key behaviours:
   - **Entity discovery is dynamic** (`entity.async_add_discovered`): each platform maps what the state holds to entity factories and adds an entity when a new key appears. Control entities are created after a complete main-menu reply. Setup still waits for the first bootstrap plus `SETUP_OBSERVATION_WINDOW` (2 s) so everything normally appears at once. Nothing is removed automatically; `async_remove_config_entry_device` lets the user delete a device the server no longer lists.
   - **Write gate** (`entity.SmartPlacePushEntity`): an entity writes its state only when `_async_calculate_state()` differs from the last write — the server rebroadcasts unchanged values 2–3 times per second.
   - **Toggle rule** (`entity.SmartPlaceToggleEntity`): lights, sockets, on/off scenes and the doorbell mute only have a toggle command. Turn-on/off is decided against the effective state (expected state if pending, else the server value); an unknown state raises.
   - **Availability** = WS connected AND session phase in `HEALTHY_PHASES`. Everything goes *Unavailable* on disconnect except the Connection diagnostic binary sensor (always available so automations can alert on it), the price sensor and a fetched plan image.
   - **Devices:** one HA device per light, socket and blind (entity takes the device name, area suggested from the heating zones); per-zone climate devices; category devices (Weather, Energy, Scenes, Groups, Intercom, Doors, Cameras, one per level group). All link to the main device with `via_device_id`. Never use `via_device`: it raises.
   - **Dashboard:** `dashboard.py` registers a read-only sidebar dashboard through HA-internal lovelace classes; `__init__.py` guards it so a failure costs the dashboard only. `plan_view.py` builds its configuration; the action `smart_place.get_plan_config` returns the same configuration.
   - Charts (electricity/water) aren't pushed by the server; the client re-issues `GiveMeChartStandsManuell<id>` and the day's range read every `CHART_POLL_INTERVAL` (60 s).
   - Token rejection triggers HA's re-auth (Repairs) flow via the `on_reauth` callback; `config_flow.py` validates a token by doing a real connect.
   - **Do not rename existing entities or change unique ids**: automations, dashboards and long-term statistics depend on them.

The wire protocol is a German-named text-frame protocol (e.g. `TEMPIST<n>`, `REGEN`, `leuchte<n>`, `JALICO<n>`) reverse-engineered from the vendor SPA — DESIGN.md §1 and §10 hold the protocol notes and live-capture logs; §11 holds the frame→entity mapping.

## Tooling notes

- Pyright only type-checks the client library + tests. The HA-facing modules are excluded in `pyproject.toml` (they'd need `homeassistant` installed, which isn't a dep); hassfest CI and the live box cover them. Don't "fix" this by adding `homeassistant` to deps. To type-check them once, copy `custom_components/` to a scratch directory outside the repository and run pyright there in a throwaway environment with `homeassistant` installed (the project's `pyproject.toml` excludes those files even when they are named on the command line).
- The matching `pytest-homeassistant` package (same version as Home Assistant) can run the integration on a real HA core with scripted sockets, also in a throwaway environment outside the repository. IMPLEMENT.md describes how this was done for the control entities.
- Ruff is the formatter and linter (line length 120, Google docstrings); config and the deliberate ignore list are in `pyproject.toml`. CI = lint-check + pytest + hassfest/HACS validation.
- Fixtures in `tests/fixtures/*.ndjson` are captured live frames, redacted. When a fixture disagrees with live behaviour, trust a fresh live capture over the fixture.
- Home Assistant changes fast (2026.11: `probatio` instead of `voluptuous`, `native_*` climate members, `async_entity_id_changed`, device registry changes). Verify every HA API against the source of the targeted version before using it.

## Reference docs

- `README.md` — what is supported, rooms, the plan dashboard, actions.
- `DESIGN.md` — architecture, decision log, protocol notes, entity mapping. Update it when decisions change.
- `IMPLEMENT.md` — running implementation log, including divergences from the design and the protocol points that still need a live test.
- `DEPLOY.md` — install/upgrade instructions and what healthy looks like in the UI.
- `.claude/skills/ha-live/SKILL.md` — full `ha-live` command reference and verification recipes for the installed instance.
