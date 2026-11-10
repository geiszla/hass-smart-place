# Smart Place — Home Assistant integration

Custom Home Assistant integration for the
[Smart Place](https://www.smartplace.ch) Swiss residential automation system,
plus a standalone Python client library that can be exercised from the CLI
without running Home Assistant.

> **Unofficial.** The integration talks to the same WebSocket the vendor's web
> app uses. That protocol is not documented and was worked out from the web
> app; smart PLACE AG does not support it and can change it without notice.
> Use it at your own risk: the entities switch real lights, blinds, heating
> and doors.

Requires Home Assistant **2026.11** or newer. Install through HACS as a custom
repository; see [`DEPLOY.md`](DEPLOY.md).

## What is supported

Devices are read from the Smart Place server when the integration starts, and
again whenever the server lists a new one. Nothing is configured per home.

| Smart Place | Home Assistant | Notes |
| --- | --- | --- |
| Light (switch, LED) | `light`, on/off | One device per light |
| Light (dimmer) | `light` with brightness | Built from the vendor web app's code; not tested on a real dimmer |
| Socket | `switch` (outlet) | A switch, so "all lights off" does not cut socket power |
| Blind (slatted, fabric, awning) | `cover` | Open, close, reported position. Slatted blinds: "tilt open" moves to the vent position |
| Heating zone | `climate` | Heat / Off, target 19–26 °C in whole degrees, measured temperature, heating or idle |
| Scene that only triggers | `scene` | Usable from the voice assistant |
| Scene with on/off state | `switch` | |
| Numbered on/off scenes (e.g. ventilation level 1/2/3) | `fan` | Three or more scenes with the same name and the numbers 1..N become one fan with N speeds |
| "All" buttons of the lights and blinds pages | `button` | "All blinds up", "All blinds down"; "All lights" is disabled by default |
| Doorbell mute | `switch` | |
| Door openers | `button` | Disabled by default: they open real doors |
| Floor plan | `image` and a dashboard | See [Plan dashboard](#plan-dashboard) |
| Temperatures, weather, weather alarms | `sensor`, `binary_sensor` | |
| Electricity, heating and water meters | `sensor` | With electricity price and cost for the EWZ (Zurich) tariff |
| Intercom cameras | `camera` | |
| Parcel box PIN, info board, intercom caller | `sensor` | |

Good to know:

- **Lights, sockets, on/off scenes and the doorbell mute can only be toggled**
  on the Smart Place side. The integration therefore decides "turn on" and
  "turn off" from the last known state. Right after a start or a reconnect,
  until the server has reported the state, such a command is refused with an
  error instead of risking a switch in the wrong direction.
- **Blinds have no stop and no "go to position" command.** The position shown
  is the Smart Place server's own estimate.
- **Heating:** Smart Place setpoint 18 is shown as mode *Off*. Switching back
  to *Heat* restores the previous target. The climate entity has no on/off
  buttons on purpose, so that "turn off everything" does not switch the
  heating off.
- **Ventilation has no off.** One level is always active; setting the fan to
  0 % or turning it off gives an error.
- A device that disappears from the Smart Place server is not removed. Delete
  its device in Home Assistant when you no longer want it.

## What is not supported

- Scenes that need a PIN. They get no entity.
- Daily consumption targets (they can be read as attributes of the meter
  sensors, but not changed).
- Media and speakers, ring and speak volume.
- RGB lights, fan coils, alarm devices, sauna.
- Two-way audio with the intercom, and a live doorbell signal.

## Rooms

Smart Place does not say which room a device is in. The integration suggests a
Home Assistant area for every light, socket and blind:

1. It looks for the name of a heating zone's room in the device name. "Kitchen
   spot lights" goes to the zone "Living room/dining room/kitchen". If several
   rooms fit, the longest name wins.
2. If no name fits and the device is placed on the floor plan, it takes the
   heating zone whose marker is nearest on the plan.
3. Otherwise it suggests nothing.

If the heating zone's device already has an area in Home Assistant, that area
is suggested (also when you renamed it). Otherwise the zone's room name is
used, and Home Assistant creates the area.

The suggestion is made **once, when Home Assistant creates the device**. The
integration never changes the area of a light, socket or blind afterwards. To
change it, open the device in Home Assistant and pick another area.

Everything works without areas: control, the plan dashboard and the automatic
dashboards do not depend on them. Scenes, group buttons and door buttons get
no area on purpose, so that a command for a whole area cannot trigger them.

**Tip — entity ids.** By default Home Assistant puts the area in front of new
entity ids, which gives ids such as
`light.living_room_dining_room_kitchen_kitchen_lights`. If you prefer
`light.kitchen_lights`, remove "area" from Home Assistant's "Entity ID format"
setting **before** you install or update to a version that adds entities.
Existing entity ids are not changed.

## Plan dashboard

The integration adds a read-only dashboard **Smart Place** to the sidebar. It
shows the floor plan from your Smart Place installation with the devices at
the places they have in the Smart Place app. There is one view per function,
because the markers would overlap otherwise:

- **Lights** — an icon per light and socket. Tap toggles, hold opens the
  details.
- **Blinds** — the position of each blind, and buttons for down, vent (slatted
  blinds) and up.
- **Climate** — measured and target temperature per zone, and − / + buttons.
- **Scenes** — scenes, scene switches, ventilation, doorbell mute and the
  group buttons.

Devices without a usable position on the plan are listed below the plan.
Disabled entities are left out. With several floor plans the views repeat per
plan.

To use the views in a dashboard of your own, run the action
`smart_place.get_plan_config` (*Developer tools → Actions*). It returns the
same configuration; copy the views you want into your dashboard's YAML.

The dashboard is built on parts of Home Assistant that are not a public
interface. If a Home Assistant update breaks them, the integration logs one
warning and works on without the dashboard; `smart_place.get_plan_config`
keeps working.

## Actions

| Action | What it does |
| --- | --- |
| `smart_place.adjust_setpoint` | Moves a heating zone's setpoint by a number of whole degrees along the Smart Place scale 18–26, where 18 is Off. Used by the − / + buttons of the plan. |
| `smart_place.get_plan_config` | Returns the configuration of the plan dashboard. |

## Development

```text
custom_components/smart_place/                    # Home Assistant integration (thin wrapper)
custom_components/smart_place/smart_place_client/ # Standalone async library (no HA dependency)
tests/                                            # pytest + redacted capture fixtures
scripts/                                          # setup / lint / lint-check / test / ha-live
```

Install dependencies (uv-based) and run the checks:

```bash
./scripts/setup
./scripts/lint-check
./scripts/test
```

Run the standalone CLI:

```bash
# Put SMART_PLACE_TOKEN=<token> into .env (gitignored) once.
uv run sp-cli --replay tests/fixtures/control-bootstrap.ndjson  # offline replay, no network
uv run sp-cli --live < /dev/null                                # watch the live server
uv run sp-cli --live --capture output/session.ndjson < /dev/null
```

You can override the token per call: `SMART_PLACE_TOKEN=... uv run sp-cli --live`.
An exported variable wins over `.env`. See DESIGN.md §4.

### Safety

- `sp-cli --live` prints what the server sends. Lines typed on stdin are
  **dropped** unless you start it with `--allow-send`; with that flag every
  line goes to your real building. In `--replay` mode lines are recorded and
  never transmitted.
- Captures are redacted for the URL token and the remote-access password
  frames. They still contain details of your home; check a capture before you
  share it.
- For testing the Home Assistant side without touching the building, set
  `DRY_RUN = True` in `custom_components/smart_place/const.py`: commands are
  then logged (`dry run: would send ...`) and not sent, and the Connection
  sensor shows the attribute `dry_run: true`.

### Inspecting the installed integration (`ha-live`)

`sp-cli` talks to the **Smart Place server**. `./scripts/ha-live` talks to a
running **Home Assistant** over its REST and WebSocket APIs and shows what the
integration made of it: devices, entities, states, logs.

```bash
# Put HASS_TOKEN=<long-lived token> into .env (HA UI → your user →
# Security → Long-lived access tokens). HASS_URL overrides the default
# (http://homeassistant.local:8123).
./scripts/ha-live status              # HA version, config entry, Connection sensor
./scripts/ha-live entities            # entities with their current state values
./scripts/ha-live state <entity_id>   # full state object
./scripts/ha-live watch               # stream live state changes
./scripts/ha-live logs                # smart_place log entries + tracebacks
./scripts/ha-live update              # install the latest version through HACS
./scripts/ha-live restart             # restart Home Assistant core
./scripts/ha-live call <domain.service> <entity_id> [key=value ...]
                                      # only works while the integration is in dry run
```

## Reference

- [`DEPLOY.md`](DEPLOY.md) — installing and upgrading.
- [`DESIGN.md`](DESIGN.md) — protocol notes, architecture, decision log.
- [`IMPLEMENT.md`](IMPLEMENT.md) — implementation log, including the protocol
  points that still need a test on a real installation.

## License

[MIT](LICENSE)
