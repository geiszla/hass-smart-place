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
scripts/                        # setup / lint / lint-check / test
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
