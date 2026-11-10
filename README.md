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

Requires Home Assistant **2026.11** or newer. Install it from the Home
Assistant Marketplace (*Settings → Marketplace → Add from link*); the steps are
in [`DEPLOY.md`](DEPLOY.md). Update it under *Settings → Updates*, then restart
Home Assistant.

## What is supported

Devices are read from the Smart Place server when the integration starts, and
again whenever the server lists a new one. Nothing is configured per home.

| Smart Place | Home Assistant | Notes |
| --- | --- | --- |
| Light (switch, LED) | `light`, on/off | One device per light |
| Light (dimmer) | `light` with brightness | Built from the vendor web app's code; not tested on a real dimmer |
| Socket | `switch` (outlet) | A switch, so "all lights off" does not cut socket power. Can be protected against switching: see [Protected sockets](#protected-sockets) |
| Blind (slatted, fabric, awning) | `cover` | Open, close, reported position. Slatted blinds: "tilt open" moves to the vent position. No command while a weather alarm blocks the blind: see [Blinds and weather alarms](#blinds-and-weather-alarms) |
| Per blind: "Lower after weather alarm" | `switch` (configuration) | A setting of the integration, off by default: see [Lower after weather alarm](#lower-after-weather-alarm) |
| Heating zone | `climate` | Heat / Off, target 19–26 °C in whole degrees, measured temperature, heating or idle |
| Scene that only triggers | `scene` | Usable from the voice assistant |
| Scene with on/off state | `switch` | |
| Numbered on/off scenes (e.g. ventilation level 1/2/3) | `fan` | Three or more scenes with the same name and the numbers 1..N become one fan with N speeds, if exactly one of them is on when the integration first sees them. Otherwise they become switches |
| Group buttons of the lights and blinds pages | `button` | "All blinds up", "All blinds down"; "All lights" and the button of a blinds group other than "All" are disabled by default |
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
  error instead of risking a switch in the wrong direction. Within one second
  after the integration sent a scene or a group button, such a command waits
  for the rest of that second: the scene is about to change the states.
- **Blinds have no stop and no "go to position" command.** The position shown
  is the Smart Place server's own estimate.
- **Heating:** Smart Place setpoint 18 is shown as mode *Off*. Switching back
  to *Heat* restores the previous target. Heating is switched off only on the
  thermostat itself (mode *Off*, or the `smart_place.adjust_setpoint` action):
  the climate entity has no "turn on" / "turn off" on purpose, so that "turn
  off everything" does not switch the heating off.
- **"Turn off" for a whole area.** `homeassistant.turn_off`, `turn_on` and
  `toggle` on an area still act on that area's lights, sockets and other
  entities. Home Assistant then reports an error for the thermostat ("does
  not support action climate.turn_off"), and an automation stops at that
  step. To avoid it, use `light.turn_off` / `switch.turn_off` for the area,
  or name the entities.
- **Ventilation has no off.** One level is always active; setting the fan to
  0 % or turning it off gives an error.
- A scene that the Smart Place app does not display gets an entity that is
  disabled by default.
- A device that disappears from the Smart Place server is not removed. Delete
  its device in Home Assistant when you no longer want it. Home Assistant
  refuses that until the integration was reloaded or Home Assistant was
  restarted: until then the integration still provides the device.

## What is not supported

- Scenes that need a PIN. They get no entity.
- Daily consumption targets (they can be read as attributes of the meter
  sensors, but not changed).
- Media and speakers, ring and speak volume.
- RGB lights, fan coils, alarm devices, sauna.
- Two-way audio with the intercom, and a live doorbell signal.

## Options

*Settings → Devices & services → Smart Place → Configure* has two settings:

- **Protected sockets** — see [Protected sockets](#protected-sockets).
- **Wait before lowering (minutes)** — see
  [Lower after weather alarm](#lower-after-weather-alarm).

A change applies at once; nothing is restarted or reloaded. The settings are
stored with the integration's entry in Home Assistant, together with the
state of the "Lower after weather alarm" switches. They survive restarts,
reloads and updates. They do not survive removing the integration and adding
it again: set them again afterwards.

## Protected sockets

Some sockets must never be switched off, for example the one that powers a
home server. Smart Place can only *toggle* a socket, so even a "turn on" sent
at the wrong moment could switch it off. For a protected socket Home Assistant
therefore sends no command at all.

To protect a socket, select it under **Protected sockets** in the
[options](#options). No socket is protected by default.

While a socket is protected:

- "Turn on", "turn off" and "toggle" for its switch do nothing. No command is
  sent and no error is shown; a toggle in the UI flips back by itself. The
  integration writes one line to the log at level INFO
  (`<entity> is protected: LightToggle not sent`).
- A call for several entities (`switch.turn_off` for all switches or for an
  area, `homeassistant.turn_off`) switches the others and skips the protected
  socket.
- Its switch has the attribute `protected: true` and a lock icon. Every other
  socket has `protected: false`.
- On the plan dashboard its marker is a lock, and a tap opens the details
  instead of toggling.
- The switch still shows the real state of the socket.

**What the integration cannot prevent.** Everything that is decided inside
Smart Place can still switch a protected socket: a Smart Place scene, the "All
lights" group, a wall button and the Smart Place app. This includes the scene
entities and the "All lights" button of this integration: each sends one
command, and Smart Place decides which devices it switches. Check that no
scene you use contains the socket.

The protection is an option of the integration. It survives restarts, reloads
and updates, but not removing the integration and adding it again: protect the
socket again afterwards.

## Blinds and weather alarms

Smart Place has rain, wind and hail alarms. While an alarm holds a blind,
Smart Place moves it up and refuses to lower it: the blind is **blocked**.
Smart Place reports no position for a blocked blind.

While a blind is blocked, its cover entity:

- has the state *open* and no position;
- offers no command. The standard card shows no buttons. A call for an area
  or for all covers skips the blind. A call for the blind itself is refused
  ("does not support action ...");
- has the attributes `blocked: true` and `weather_alarm`. `weather_alarm` is
  `rain`, `wind` or `hail`, or the word Smart Place sent if it is none of
  these.

A blind that is not blocked has `blocked: false` and no `weather_alarm`. The
attribute `blocked` is there whenever the blind is available, so automations
can be built on it. Example trigger for "the alarm of this blind has ended":

```yaml
triggers:
  - trigger: state
    entity_id: cover.living_room_blinds
    attribute: blocked
    from: true
    to: false
```

Keep the `from:` line. While the connection to Smart Place is down, the blind
is *unavailable* and has no attributes. After a reconnect Smart Place sends
all values again; in the moment before the blind's value has arrived,
`blocked` keeps the value it had before the connection was lost. So a
reconnect during an alarm does not look like a new alarm, and an alarm that
ended while the connection was down gives one change from `true` to `false`
when the new value arrives. Right after a start of Home Assistant, before any
value was received, `blocked` is `false`.

On the plan dashboard a blocked blind shows a rain, wind or hail icon in place
of its position (a warning sign for any other alarm). Its buttons are grey and
do nothing.

### Lower after weather alarm

An alarm leaves the blind up. Every blind has a switch **Lower after weather
alarm** on its device, under *Configuration*, off by default. While it is on,
the integration lowers the blind when it sees the blind's alarm end.

> **This switch is the one place where the integration sends a command to
> your home without a user action at that moment.** Everything else is sent
> only when you, an automation or a script calls an action. Each of these
> commands is written to the log at level INFO, with the name of the blind
> and the reason.

How it works:

- "Lower" is the plain down command, also for slatted blinds.
- Only an alarm end that the integration saw while it was connected counts.
  If Home Assistant was stopped or was reconnecting at that moment, nothing is
  lowered: the values Smart Place sends again after a connect are not taken
  for the end of an alarm.
- The blind is lowered at once. To wait first, set **Wait before lowering
  (minutes)** in the [options](#options); it applies to all blinds. The wait
  ends without lowering if the alarm comes back, the blind reports another
  position (somebody moved it), Home Assistant sends a command for the blind
  (its up, down or vent, or a blinds group button), the switch is turned off,
  or the connection is lost.
- When it is time, the command is sent only if the switch is on, the
  connection is up, the blind is not blocked and it is not already fully
  down. It is sent once and never repeated.
- Turning the switch **on** lowers the blind at once if it is neither fully
  down nor blocked. If it is blocked, it is lowered when the alarm ends.
  Turning the switch **off** ends a running wait and sends nothing.
- The switch is a configuration entity, so a call for an area or a device
  does not include it. A call for all switches (`switch.turn_on` with
  `entity_id: all`) does: it would turn the function on for every blind and
  lower every blind that is not down.
- While the connection is down the switch is unavailable. Its setting is
  kept.

To see the log lines, set the log level of the integration to `info` (Home
Assistant shows warnings and errors by default):

```yaml
logger:
  logs:
    custom_components.smart_place: info
    smart_place_client: info
```

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

On an install that existed before this version, the devices of the heating
zones are already there. A heating-zone device without an area gets the area
of its room once, on the first start with this version. An area you clear
afterwards stays cleared.

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
  details. A [protected socket](#protected-sockets) shows a lock, and a tap
  opens the details.
- **Blinds** — the position of each blind, and buttons for down, vent (slatted
  blinds) and up. A [blocked blind](#blinds-and-weather-alarms) shows its
  alarm instead, with grey buttons.
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

- The integration sends a command only when an action is called for one of
  its entities — with one exception: the switch
  ["Lower after weather alarm"](#lower-after-weather-alarm). It is the one
  place where the integration sends a command (the blind's down command)
  without a user action at that moment. It is off by default, and each such
  command is logged at level INFO.
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
./scripts/ha-live update              # install the latest version through the Marketplace
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
