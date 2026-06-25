---
name: ha-live
description: Query the live Home Assistant box for Smart Place integration data using ./scripts/ha-live — config entry status, devices, entities and their current values, full state objects, system logs with tracebacks, live state streaming, plus reload/restart after deploys and raw REST/WebSocket escape hatches. Use when asked to verify the deployed integration, check entity values on the Pi, debug unavailable or missing entities, inspect HA logs for smart_place errors, or watch live updates.
---

# Inspect the live Smart Place deployment

`./scripts/ha-live <command>` talks to the real Home Assistant box (default
`http://homeassistant.local:8123`) over its REST + WebSocket APIs and fetches
Smart-Place-scoped data. It makes no judgement itself — use the commands to
pull the data you need and interpret it with the "What healthy looks like"
section below. Exit code `2` means it could not connect or authenticate.

## Prerequisites (one-time)

- A **long-lived access token** from an admin HA user. It can only be minted
  in the UI (the user must do this, it cannot be scripted): HA → click the
  user name bottom-left → **Security** tab → **Long-lived access tokens** →
  Create.
- Put it in `.env` at the repo root (gitignored, auto-loaded — same policy as
  `SMART_PLACE_TOKEN`): `HASS_TOKEN=...`
- If the box is not at `homeassistant.local:8123`, also set
  `HASS_URL=http://<ip>:8123` (WSL2 often cannot resolve `.local` mDNS names
  — use the Pi's IP).

If a command exits 2 with an auth error, the token is wrong/revoked or from a
non-admin user. With a connection error, check the Pi is up and `HASS_URL`
resolves from this machine. **"Name or service not known" for
`homeassistant.local` under WSL2 is normal even when the Windows browser opens
the URL fine** — WSL2 has no mDNS. Resolve the IP via Windows and persist it:

```bash
powershell.exe -NoProfile -Command "ping -4 -n 1 homeassistant.local"
# take the IP from "Pinging homeassistant.local [192.168.1.x]"
echo 'HASS_URL=http://192.168.1.x:8123' >> .env
```

## Commands

| Command | What it returns |
| --- | --- |
| `status` | HA version, config entry state (+ failure reason), device/entity counts, Connection sensor value, any unavailable entities |
| `devices` | Device-registry entries: main Smart Place device + climate sub-devices |
| `entities` | Every entity with its current value and name (`--disabled` to include disabled ones) |
| `state <entity_id>...` | Full state object(s) as JSON: value, all attributes, timestamps |
| `logs` | smart_place system-log entries with tracebacks (`--all` for the whole buffer) |
| `watch --seconds N` | Live stream of Smart Place state changes (default 60 s) |
| `reload` | Reload the config entry — re-runs one-shot entity discovery |
| `restart` | Restart HA core, wait for it to come back, print entry state |
| `get <path>` | Raw REST GET, e.g. `get /api/error_log`, `get /api/config` |
| `ws <type> [json]` | Raw WebSocket command, e.g. `ws system_log/list` |
| `config-set <domain> <key> --file <f>` | Create/update an automation, scene, or script (writes HA config; see below) |

`status`, `devices`, `entities`, and `logs` accept `--json` for
machine-readable output — prefer that when parsing programmatically.

Display caveat: the `entities` table appends the unit to the name column
(`Temperature [°C]`) for readability — the `[°C]` is **not** part of the
friendly name. Before judging how an entity is named or configured, check the
real `friendly_name`/attributes with `state <entity_id>`.

## Verification recipes

- **After deploying new code** (changed Python requires a core restart;
  reload is not enough):

  ```bash
  # NOTE: this box has NO SSH (port 22 refused — verified 2026-06-25), so
  # the scp below does NOT work here. The user deploys the files via their
  # own non-SSH channel (Samba / File editor / Studio Code Server); you
  # then run the restart + checks over HTTP. See the ha-box-no-ssh memory.
  scp -r custom_components/smart_place root@homeassistant.local:/config/custom_components/  # ✗ no sshd
  ./scripts/ha-live restart
  ./scripts/ha-live status
  ./scripts/ha-live logs
  ```

- **Check a specific value or its configuration**: `entities` to find the
  entity_id, then `state sensor.smart_place_outdoor_temperature` for the full
  object including attributes (unit, device_class, last_changed...).
- **Debug a missing sensor**: discovery is one-shot during the 2 s
  observation window after bootstrap (`SETUP_OBSERVATION_WINDOW` in
  `const.py`). If the server first pushed that ID later, `reload` re-runs
  discovery, then `entities` to confirm it appeared.
- **Confirm live pushes are flowing**: `watch --seconds 60` — climate/weather
  sensors update on server pushes; the stream prints each change.
- **Anything not covered**: use the escape hatches, e.g. full error log text
  `get /api/error_log`, entity registry dump
  `ws config/entity_registry/list`, config entries
  `ws config_entries/get '{"domain": "smart_place"}'`.

## Reading and editing automations / scenes / scripts

These live in HA's config store, *not* in the Smart Place integration, so they
have no `ha-live` listing command — reach them through the generic `get`
escape hatch and the `config-set` write command. They share one REST shape,
`/api/config/<domain>/config/<key>` (the views the GUI editors drive), and
`config-set` passes `<domain>` straight through rather than enforcing a list —
in current HA the editable domains are these three, and HA rejects any other:

- **`automation`** / **`scene`** — `<key>` is the numeric `id` (also the `id`
  attribute on the entity).
- **`script`** — `<key>` is the object_id (the part after `script.`).

**List them** (they are normal entities):

```bash
# every automation with its config id, on/off state, and last-triggered time
./scripts/ha-live get /api/states \
  | python3 -c 'import json,sys; [print(s["attributes"].get("id"), s["entity_id"], s["state"]) \
      for s in json.load(sys.stdin) if s["entity_id"].startswith("automation.")]'
```

(`jq` is not installed in this repo's env — filter with `python3` as above.)

**Read one** full config (triggers/conditions/actions):

```bash
./scripts/ha-live get /api/config/automation/config/1781254929024
```

**Edit one** — round-trip through a file so the payload stays in the server's
schema, preview with `--dry-run`, then write:

```bash
./scripts/ha-live get /api/config/automation/config/<id> > a.json
# ...edit a.json...
./scripts/ha-live config-set automation <id> --file a.json --dry-run   # shows a diff, writes nothing
./scripts/ha-live config-set automation <id> --file a.json             # prompts, then writes
```

A `config-set` POST validates the payload, writes the YAML store, **and
reloads that domain**, so the change is live at once — no separate reload
needed. A `<key>` that does not exist yet **creates** a new item. The command
prompts for confirmation (skip with `--yes`; required when piping the payload
via `--file -`) and prints the stored config back as confirmation.

`config-set` writes **config only** — it cannot itself call a service, so it
never opens a door directly (it does not break the "no service-calling
command" rule below). But an automation you write here *can* press a door
button the next time it triggers — e.g. the existing **Intercom Ringing –
Action** automation calls `button.press` on the door buttons. Review every
payload (`--dry-run` it) before writing, and never add or trigger a door
`button.press` you did not intend.

## What healthy looks like

- `status`: entry state `loaded`. `setup_retry`/`setup_error` with a reason
  means the Smart Place server is unreachable from the Pi or the URL token
  rotated (the user must re-authenticate via the Repairs flow, DEPLOY.md §6).
- Connection sensor `on`. If `off` while the entry is loaded, the upstream
  Smart Place WS dropped; the client reconnects with backoff on its own —
  check `logs` and re-run `status` after a minute.
- No unavailable entities while Connection is `on` — that would be a real
  availability-wiring bug. All-but-Connection unavailable while Connection is
  `off` is expected behaviour, not a bug.
- `logs` empty of ERROR entries. The system-log buffer is small and clears on
  every core restart, so check soon after reproducing; for full logs
  including startup (e.g. an `ImportError` that prevents the integration
  loading at all), fetch the Supervisor core journal over HTTP:
  `./scripts/ha-live get /api/hassio/core/logs`. (SSH is **not** enabled
  on this box — port 22 is refused, verified 2026-06-25 — so
  `ssh root@homeassistant.local "ha core logs"` does not work; see the
  ha-box-no-ssh memory.)

## Safety

- **Never call `button.press` on the door buttons** (Front door, Ground floor
  entrance, Garage entrance, Mailbox) — they send `OEFFNER<n>` commands that
  open real doors in the building. `ha-live` deliberately has no
  service-calling command; do not work around that via `get`/`ws` or curl.
- **Default to read-only.** Unless the user specifically asks for a change,
  use only the read-only commands. The mutating operations — `reload`,
  `restart`, `config-set`, and any entity-registry rename / helper creation /
  dashboard edit done through the raw `ws`/`get` hatches — affect the HA
  instance only, never the building directly, but run them only when explicitly
  instructed.
- `HASS_TOKEN` grants admin over HA: keep it in `.env`, never echo or commit
  it. The script never prints it.

## Maintenance note

The registry/log WebSocket commands (`config/device_registry/list`,
`config/entity_registry/list`, `system_log/list`) are HA's frontend-internal
API — stable for years but undocumented. If they start erroring after an HA
upgrade, re-check the command names in `homeassistant/components/config/` of
home-assistant/core.
