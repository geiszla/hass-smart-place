# Smart Place — Home Assistant integration (POC)

Custom Home Assistant integration for the
[Smart Place](https://www.smartplace.ch) Swiss residential automation system,
plus a standalone Python client library that can be exercised from the CLI
without running Home Assistant.

> **Status: v1 POC.** Stand up the project, prove out the WebSocket
> connection, and observe device/system state messages. Device-type mapping,
> control commands wired to HA entities, HACS publication, etc. are out of
> scope for v1. See [`DESIGN.md`](DESIGN.md) for the full design and
> [`IMPLEMENT.md`](IMPLEMENT.md) for the running implementation log.

## Layout

```text
smart_place_client/             # Standalone async library (no HA dependency)
  protocol.py                   # Pure parsing/state (parse_frame / encode_frame)
  client.py                     # SmartPlaceClient.live(...) / .replay(...) + Click CLI
custom_components/smart_place/  # Home Assistant integration (thin wrapper)
tests/                          # pytest + fixtures
scripts/                        # setup / lint / lint-check / test / ha-live
```

## Quick start

Install dependencies (uv-based):

```bash
./scripts/setup
```

Run the standalone CLI against the live Smart Place server:

```bash
# Put SMART_PLACE_TOKEN=<token> into .env (gitignored) once.
# The CLI auto-loads .env on start, so no per-terminal export is needed.
uv run sp-cli --live                                       # observe stdout, type to stdin to send (use with care)
uv run sp-cli --live --capture tests/fixtures/session.ndjson  # tee a fixture
uv run sp-cli --replay tests/fixtures/session.ndjson       # offline replay, no network
```

You can still override the token per-invocation: `SMART_PLACE_TOKEN=... uv run sp-cli --live`.
An exported / explicit env var always wins over `.env`. See DESIGN.md §4
for the full secret-storage rationale.

### Inspecting the deployed integration (`ha-live`)

`sp-cli` talks to the **Smart Place server** — the raw frames *before* the
integration processes them. `./scripts/ha-live` talks to a running **Home
Assistant** instance over its REST + WebSocket APIs and shows the *result*:
the devices, entities, states, categories, and units the integration
actually produced. The two sit at opposite ends of the pipeline and don't
overlap — use `ha-live` to verify that a code change landed correctly on the
live box.

```bash
# Put HASS_TOKEN=<long-lived token> into .env (HA UI → your user →
# Security → Long-lived access tokens). HASS_URL overrides the default
# (http://homeassistant.local:8123).
./scripts/ha-live status              # HA version + Smart Place config entries
./scripts/ha-live entities            # entities with their current state values
./scripts/ha-live state sensor.smart_place_electricity_today  # full state object
./scripts/ha-live watch               # stream live state changes
./scripts/ha-live logs                # smart_place log entries + tracebacks
./scripts/ha-live reload              # reload the config entry (re-runs setup)
./scripts/ha-live ws <command> [json] # arbitrary frontend WS command
```

The registry/log WebSocket commands use HA's frontend-internal API; if a
command breaks after an HA upgrade, re-check the names against
`homeassistant/components/config/` in home-assistant/core.

Run lint and tests:

```bash
./scripts/lint-check
./scripts/test
```

## Safety

The CLI is bidirectional and there is **no software gate** against accidental
sends — safety is behavioural. If you don't want to issue commands to your
real building, don't type anything on stdin. To experiment with sending,
use `--replay` mode where sends are accepted but never transmitted.

## Reference

- [`DESIGN.md`](DESIGN.md) — protocol notes, architecture, decision log.
- [`IMPLEMENT.md`](IMPLEMENT.md) — implementation progress and any
  divergence from the design.
