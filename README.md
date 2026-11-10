# Smart Place — Home Assistant integration

Custom Home Assistant integration for the
[Smart Place](https://www.smartplace.ch) Swiss residential automation system,
plus a standalone Python client library that can be exercised from the CLI
without running Home Assistant.

**Contents**

- [Installation](#installation)
  - [Updating](#updating)
  - [Troubleshooting](#troubleshooting)
- [What is supported](#what-is-supported)
- [What is not supported](#what-is-not-supported)
- [Devices and cards](#devices-and-cards)
  - [Scenes and the voice assistant](#scenes-and-the-voice-assistant)
- [Options](#options)
- [Protected sockets](#protected-sockets)
- [Blinds and weather alarms](#blinds-and-weather-alarms)
  - [Lower after weather alarm](#lower-after-weather-alarm)
- [Rooms](#rooms)
  - [When a device is renamed in Smart Place](#when-a-device-is-renamed-in-smart-place)
- [Plan dashboard](#plan-dashboard)
- [Automation blueprints](#automation-blueprints)
- [Actions](#actions)
- [Development](#development)
- [Reference](#reference)
- [License](#license)

> **Unofficial.** The integration talks to the same WebSocket the vendor's web
> app uses. That protocol is not documented and was worked out from the web
> app; smart PLACE AG does not support it and can change it without notice.
> Use it at your own risk: the entities switch real lights, blinds, heating
> and doors.

## Installation

### Requirements

- Home Assistant **2026.11** or newer.
- The token from the address of your Smart Place web app (step 1).
- Internet access from Home Assistant: the integration connects to the Smart
  Place server.

### 1. Get the Smart Place token

The integration signs in with the secret token from the address of your Smart
Place web app. Treat it like a password: anyone who has it can control your
home.

1. Open the Smart Place web app in a desktop browser (the same address the
   mobile app uses). The address bar looks like this:

   ```text
   https://spr1.smartplace.ch:8770/Start5?<LONG-OPAQUE-TOKEN>
   ```

2. Copy **everything after the `?`**. That is the token: usually more than 60
   letters, digits and symbols, without spaces.
3. Keep it where only you can read it (a password manager). Do not paste it
   into chats or screenshots, and do not commit it to git.

### 2. Before the first start: entity ids

By default Home Assistant puts the area name in front of new entity ids. The
integration gives every light, socket and blind its own device and suggests an
area for it (see [Rooms](#rooms)), so the ids get long:
`light.living_room_dining_room_kitchen_kitchen_lights`. If you prefer
`light.kitchen_lights`, remove "area" from Home Assistant's "Entity ID format"
setting **before** you install, or before you update to a version that adds
entities. Entities that already exist keep their ids.

### 3. Install the integration

**From the Marketplace (recommended).** Home Assistant 2026.11 has a built-in
**Marketplace** for custom integrations. It replaces HACS, which cannot be
installed on 2026.11 or newer. The Marketplace is for administrators and is
not in the sidebar.

1. Open **Settings → Marketplace**. On first use it shows a warning; read and
   accept it. Until an administrator has accepted it, Home Assistant does not
   install or update anything from the Marketplace.
2. Select **Add from link** in the upper-right corner, enter
   `https://github.com/geiszla/hass-smart-place` under **GitHub link** and
   select **Add**. If the Marketplace asks for the type, choose the
   integration type and select **Add** again. If GitHub is not connected yet,
   the Marketplace asks you to connect it: select **Continue**, open the
   GitHub link it shows, enter the code and approve.
3. Open **Smart Place** in the list, select **Install**, check the version and
   select **Install** again.
4. Restart Home Assistant when the Marketplace asks for it.

*Coming from HACS:* there is nothing to do. When Home Assistant starts after
the update to 2026.11, the Marketplace takes over what HACS had: installed
content, custom repositories and update entities (with the same entity ids).
HACS itself is removed. The old "Get HACS" installer add-on is no longer
needed (it only prints an error).

**By hand.** Home Assistant OS has no SSH by default. Use the **Samba share**
add-on (Settings → Add-ons → Add-on store), the File editor add-on or Studio
Code Server.

1. Open the `config` share (`\\homeassistant\config\` on Windows,
   `smb://homeassistant.local/config/` on macOS and Linux).
2. Create `custom_components/` if it does not exist.
3. Copy the **whole** folder `custom_components/smart_place/` from this
   repository into it, with all its files and sub-folders
   (`smart_place_client/`, `translations/`, `brand/`, `blueprints/`). Do not
   pick single files: the integration needs all of them.
4. Restart Home Assistant: Settings → System → ⋮ (top right) → **Restart Home
   Assistant**.

### 4. Add the integration

1. Settings → **Devices & services** → **Add integration**.
2. Search for **Smart Place** and select it.
3. Paste the token from step 1 into **Smart Place URL token** and submit.

Home Assistant then connects to the Smart Place server with the token and
waits for its first reply. It saves the integration only if that worked.
Otherwise it shows one of these messages:

| Message | Meaning | What to do |
| --- | --- | --- |
| *The token was rejected by Smart Place.* | The token is wrong or no longer valid. | Copy it again from the address of the Smart Place web app. Smart Place can replace a token; use the current one. |
| *Could not reach the Smart Place server.* | A network or DNS problem on the Home Assistant machine, or a wrong token (the server does not answer a wrong token). | Check the token and the internet connection. |
| *Unexpected error. Check the Home Assistant log for details.* | Anything else. | Settings → System → Logs. The token is removed from the log. |

After that, Home Assistant may ask for the area of the new devices. The areas
are already filled in from the room names of your heating zones (see
[Rooms](#rooms)); change them there if you like.

### 5. Check the result

Open **Settings → Devices & services → Smart Place**. A healthy install shows:

- A **Smart Place** device with the diagnostic sensor **Connection**, which
  reads *Connected*. It stays available while the connection is down, so an
  automation can watch it (for example, notify you when it turns off).
- One device per **heating zone**, named after the room: a climate entity, the
  temperature sensor and a humidity sensor that is disabled by default.
- One device per **light**, **socket** and **blind**, named as in the Smart
  Place app. Each blind's device also has the switch
  ["Lower after weather alarm"](#lower-after-weather-alarm).
- The devices **Scene**, **Ventilation**, **Weather**, **Energy**, **Camera**
  and **Door**. What is on which device: see
  [Devices and cards](#devices-and-cards).
- Four **door** buttons (Front door, Ground floor entrance, Garage entrance,
  Mailbox). They open real doors, as the buttons in the Smart Place app do;
  disable the ones you do not want in Home Assistant.
- **Smart Place** in the sidebar: the [plan dashboard](#plan-dashboard).
- A **Configure** button on the integration: the [options](#options).

Entities appear when the Smart Place server first reports their device or
value, normally all at once when the integration starts. A device you add in
the Smart Place app later appears after the next reconnect or restart.

After a start, every entity except Connection and the electricity price is
*unavailable* until Smart Place has sent all its values once. Smart Place
sends them up to some seconds after the list of devices, and the integration
waits for them (ten seconds at most). So no entity shows a half-read state
first: an intercom that seems to ring, a parcel that seems to arrive.

### Updating

- **Marketplace:** the Marketplace provides an update entity for the
  integration, and an update shows under **Settings → Updates**. Install it
  there, then restart Home Assistant. The Marketplace looks for a new version
  of a repository added from a link every 48 hours, and only while GitHub is
  connected.
- **By hand:** replace `/config/custom_components/smart_place/` with the new
  version of the whole folder, then restart Home Assistant.

An update keeps the integration's entry: you do not enter the token again,
existing entities keep their ids, and the [options](#options) stay.

### Removing

1. Settings → Devices & services → Smart Place → ⋮ → **Delete**. This also
   deletes the [options](#options).
2. Restart Home Assistant.
3. Optional: remove the repository in the Marketplace, or delete
   `/config/custom_components/smart_place/`.
4. Optional: delete `/config/blueprints/automation/smart_place/`. The
   [blueprints](#automation-blueprints) and the automations made from them
   stay until you delete them.

### Troubleshooting

- **The integration cannot be added.** See the messages under
  [4. Add the integration](#4-add-the-integration).
- **Everything is unavailable.** The connection to the Smart Place server is
  down, and the Connection sensor reads *Disconnected*. The integration
  reconnects by itself, with growing pauses between the attempts, and logs one
  warning when the connection is lost. Meanwhile every entity is
  *unavailable*, so that dashboards do not show old values. The exceptions
  are the Connection sensor, the electricity price and a floor plan image
  that was already loaded. The integration also checks the connection every
  60 seconds and reconnects when the server does not answer within 30
  seconds.
- **Lights, blinds and heating read *Unknown* for a moment after a
  reconnect**, until Smart Place has sent their states again.
- **"Re-authenticate Smart Place" under Repairs.** Smart Place no longer
  accepts the token. Get the current one (step 1) and enter it there.
- **An entity is missing.** Some entities are disabled by default, for
  example scenes the Smart Place app does not display, the humidity sensors
  and the button of a blinds group other than "All". Enable them in the
  integration's entity list. A device added in the Smart Place app appears
  after the next reconnect or restart.
- **A socket is missing from dashboards and from "turn off" for its room.**
  It is a [protected socket](#protected-sockets): its switch is hidden on
  purpose.
- **There is no "Smart Place" dashboard, or it opens another one.** See the
  note on the address under [Plan dashboard](#plan-dashboard).
- **Logs.** Settings → System → Logs. The token and the installation's
  remote-access passwords are removed from everything the integration writes.
  Home Assistant shows warnings and errors by default. The integration writes
  at level INFO each command it sends to Smart Place (`command issued: ...`),
  why it lowered a blind by itself (`Lowering ...`) and that it did not send a
  command for a protected socket (`... is protected: ...`). To see these
  lines, add this to `configuration.yaml` and restart:

  ```yaml
  logger:
    logs:
      custom_components.smart_place: info
      smart_place_client: info
  ```

## What is supported

Devices are read from the Smart Place server when the integration starts, and
again whenever the server lists a new one. Nothing is configured per home.

| Smart Place | Home Assistant | Notes |
| --- | --- | --- |
| Light (switch, LED) | `light`, on/off | One device per light |
| Light (dimmer) | `light` with brightness | Built from the vendor web app's code; not tested on a real dimmer |
| Socket | `switch` (outlet) | A switch, so a call for all lights (`light.turn_off`) does not cut socket power. The integration's own "All lights off" button does switch sockets. Can be protected against switching: see [Protected sockets](#protected-sockets) |
| Blind (slatted, fabric, awning) | `cover` | Open, close, reported position. Slatted blinds: "tilt open" moves to the vent position. No command while a weather alarm blocks the blind: see [Blinds and weather alarms](#blinds-and-weather-alarms) |
| Per blind: "Lower after weather alarm" | `switch` (configuration) | A setting of the integration, off by default: see [Lower after weather alarm](#lower-after-weather-alarm) |
| Heating zone | `climate` | Heat / Off, target 19–26 °C in whole degrees, measured temperature, heating or idle |
| Scene that only triggers | `scene` | Usable from the voice assistant |
| Scene with on/off state | `switch` | |
| Numbered on/off scenes (e.g. ventilation level 1/2/3) | `fan` | Three or more scenes with the same name and the numbers 1..N become one fan with N speeds, if exactly one of them is on when the integration first sees them. Otherwise they become switches |
| All lights | `button` | "All lights off" and "All lights on" switch every light and socket that is not in that state yet, one after the other. A [protected socket](#protected-sockets) is never switched. Smart Place's own "All" button of the lights page is not used: what it does is not known |
| All heating | `button` | "All heating off" switches off every heating zone that heats. "Heating back on" switches exactly those zones on again, each at the target it had. A zone that was already off stays off, and without a preceding "All heating off" the button does nothing. The zones to switch back on are kept across restarts |
| Group buttons of the blinds page | `button` | "All blinds up", "All blinds down"; the button of a blinds group other than "All" is disabled by default |
| Doorbell mute | `switch` | |
| Door openers | `button` | They open real doors; not offered to the voice assistant by default |
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

## Devices and cards

Home Assistant's automatic dashboard shows one card per device that has no
area, and puts a device with an area on the card of that area. The
integration's devices are named for that:

| Device | What is on it |
| --- | --- |
| **Smart Place** | Connection, floor plan image, doorbell mute, "All lights off", "All lights on", "All heating off", "Heating back on", the blinds group buttons, intercom caller, parcel PIN, info board |
| One per heating zone, named after the room | Thermostat, temperature, humidity |
| One per light, socket and blind, named as in the Smart Place app | The light, switch or cover; for a blind also "Lower after weather alarm" |
| **Scene** | Every scene and every scene switch |
| **Ventilation** (one per group of numbered scenes, named after the group) | The fan |
| **Weather** | Outdoor temperature, wind speed, weather alarms |
| **Energy** | Meters, electricity price and cost |
| **Camera** | The intercom cameras |
| **Door** | The door buttons |

The devices of lights, sockets, blinds and heating zones get an area (see
[Rooms](#rooms)). The others have none on purpose, so that a command for a
whole area cannot trigger a scene or open a door.

Home Assistant builds an entity's full name from the device name and the
entity name: "Weather Outdoor temperature", "Door Mailbox", "Scene Night". A
new install gets its entity ids from these names
(`sensor.weather_outdoor_temperature`, `button.door_mailbox`).

On an install from before these names, the devices are renamed by themselves
at the first start; **entity ids do not change**. Two devices of that
version, "Smart Place Groups" and "Smart Place Intercom", are no longer used
and stay behind: delete them on their device pages (⋮ → Delete). "Smart Place
Groups" still holds the old "All lights" button, which is no longer provided;
it is deleted with the device.

### Scenes and the voice assistant

A scene is called "Scene Night" in Home Assistant, but you say "Night". The
voice assistants do not go by the name: they go by the entity's list of
**aliases**. By default that list holds one entry that stands for the full
name. When the integration adds a scene or a scene switch and the list is
still that default, it replaces it with the scene's own name ("Night").

- A list you edited is never touched. This includes a list that still has the
  default entry next to an alias of your own; the default entry then reads
  "Scene Night".
- The alias is set once. If the scene is renamed in Smart Place later, change
  the alias yourself: entity settings → **Voice assistants**.
- Existing entity ids stay (`scene.night`). A scene created on a new install
  gets an id with the device in it (`scene.scene_night`).

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

- **Its switch is hidden in Home Assistant.** Home Assistant leaves a hidden
  entity out of its automatic dashboards and out of every call for an area, a
  device, a floor or a label. So "turn off" for the socket's room
  (`light.turn_off` and `switch.turn_off` with the area, or
  `homeassistant.turn_off`) switches the room's lights and other sockets and
  does not reach the protected one. The entity still exists: find it in the
  integration's entity list (change the filter to show hidden entities) and
  use it in dashboards and automations as before.
- "Turn on", "turn off" and "toggle" for its switch do nothing. No command is
  sent and no error is shown; a toggle in the UI flips back by itself. The
  integration writes one line to the log at level INFO
  (`<entity> is protected: LightToggle not sent`). This is what stops a call
  that names the switch, `switch.turn_off` for all switches
  (`entity_id: all`, which includes hidden entities) and a voice assistant
  the socket is exposed to (hiding does not change that setting).
- The buttons "All lights off" and "All lights on" do not switch it.
- Its switch has the attribute `protected: true` and a lock icon. Every other
  socket has `protected: false`.
- On the plan dashboard its marker is a lock and a tap does nothing. A long
  press opens the details.
- The switch still shows the real state of the socket.

The integration hides the switch when you protect the socket and at every
start. It shows the switch again when you remove the protection, unless you
hid it yourself. If you make a protected socket's switch visible by hand, it
stays visible until the next start; it stays protected.

**What can still switch a protected socket.** Everything that is decided
inside Smart Place: a Smart Place scene, a wall button and the Smart Place
app. This includes the scene entities of this integration: each sends one
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
when the new value arrives. Right after a start of Home Assistant the blind
is *unavailable* until Smart Place has sent its values; if they do not come
within ten seconds, `blocked` is `false` until they do.

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

Home Assistant shows these log lines only at the log level `info`: see "Logs"
under [Troubleshooting](#troubleshooting).

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

The suggestion is made **when Home Assistant creates the device**. To change
the area, open the device in Home Assistant and pick another one. The
integration changes the area of a light, socket or blind afterwards in one
case only: when the device is renamed in Smart Place and you have not chosen
another area for it (next section).

On an install that existed before this version, the devices of the heating
zones are already there. A heating-zone device without an area gets the area
of its room once, on the first start with this version. An area you clear
afterwards stays cleared.

Everything works without areas: control, the plan dashboard and the automatic
dashboards do not depend on them. Scenes, group buttons and door buttons get
no area on purpose, so that a command for a whole area cannot trigger them.

The area also ends up in the entity ids of new entities. To avoid that, see
[2. Before the first start: entity ids](#2-before-the-first-start-entity-ids).

### When a device is renamed in Smart Place

The integration compares the names of the lights, sockets and blinds each
time it connects to Smart Place (at a start and after every reconnect). For a
device whose name changed there:

- The device in Home Assistant gets the new name, and with it the names of
  its entities. No restart is needed.
- If the device is still in the area that was suggested for the old name, it
  moves to the area suggested for the new name. "No area" counts as the
  suggestion for a name that fits no room. A device that you moved to
  another area, or took out of its area, stays where you put it.
- A name you gave the device in Home Assistant stays; it hides the Smart
  Place name as before.
- **Entity ids never change by themselves**: automations and dashboards keep
  working. A light renamed from "Corridor lights" to "Hall lights" is still
  `light.corridor_lights`. To get ids from the new names, open the device in
  Home Assistant and choose **⋮ → Recreate entity IDs**, or select the
  entities under *Settings → Devices & services → Entities* and choose
  **Recreate entity IDs of selected**. Then replace the old ids in your
  automations, scripts and dashboards yourself. The plan dashboard follows
  by itself.

Scenes, heating zones and the ventilation group take a new Smart Place name
at the next start of Home Assistant. Their areas and aliases are not changed.

## Plan dashboard

The integration adds a read-only dashboard **Smart Place** to the sidebar. It
shows the floor plan from your Smart Place installation with the devices at
the places they have in the Smart Place app. There is one view per function,
because the markers would overlap otherwise:

- **Lights** — an icon per light and socket. Tap toggles, hold opens the
  details. A [protected socket](#protected-sockets) shows a lock, and a tap
  does nothing. Below the plan: "All lights off" and "All lights on".
- **Blinds** — per blind a small window that shows its position (empty: fully
  up, half filled: partly down, filled: fully down; a tap opens the details
  with the percentage), and buttons for down, vent (slatted blinds) and up.
  A [blocked blind](#blinds-and-weather-alarms) shows its alarm instead, with
  grey buttons. Below the plan: "All blinds up" and "All blinds down".
- **Climate** — measured and target temperature per zone, and − / + buttons.
  While a zone calls for heat, a flame stands before its target and both are
  in the heating colour. Below the plan: "All heating off", "Heating back on"
  and the ventilation.
- **Scenes** — one list of the scenes, in the order Smart Place lists them: a
  scene to activate has a button, a scene with an on/off state a switch. The
  doorbell mute is the last row.
- **Intercom** — one block per door: its name, the cameras that show it and a
  wide button that opens the door. A camera is put with the door whose name
  starts like the camera's ("Garage camera" and "Garage entrance camera" both
  go with "Garage entrance"). Cameras that fit no door are listed under
  "Cameras". The view is there while at least one door button or camera is
  enabled. **A tap on the button opens the real door at once**, without a
  question.

The plan is one column wide: at most 500 px on a desktop, so the whole plan
is visible without scrolling, and as wide as the screen on a phone. Only the
part of the image with the drawing and the markers is shown; the empty sides
of the image from Smart Place are cut off. Every marker lies on a backdrop in
the colour of your theme's cards, so it is readable on white walls and on dark
floors. On a screen narrower than 412 px the markers shrink with the plan.

Devices without a usable position on the plan are listed below the plan.
Disabled entities are left out. With several floor plans the views repeat per
plan.

The dashboard's address is `/smart-place`, and a view has its own address,
for example `/smart-place/intercom`. If that address is already taken — a
dashboard of your own called "Smart Place" gets the same one — the
integration uses `/smart-place-plan` instead. Both dashboards are then in the
sidebar under the same title; rename yours if that confuses. Only when both
addresses are taken there is no dashboard, and the integration logs one
warning.

To use the views in a dashboard of your own, run the action
`smart_place.get_plan_config` (*Developer tools → Actions*). It returns the
same configuration; copy the views you want into your dashboard's YAML.

The dashboard is built on parts of Home Assistant that are not a public
interface. If a Home Assistant update breaks them, the integration logs one
warning and works on without the dashboard; `smart_place.get_plan_config`
keeps working.

## Automation blueprints

The integration ships five automation blueprints. A blueprint is a ready-made
automation with a form: you choose your entities and texts, and Home Assistant
creates the automation.

- **Installed automatically.** Each time the integration starts, it copies its
  blueprints to `blueprints/automation/smart_place/` in the Home Assistant
  configuration folder. They appear under *Settings → Automations & scenes →
  Blueprints*; their names start with "Smart Place:". To use one, select it
  there, fill in the form and save.
- **Never overwritten.** A blueprint file that already exists is left as it
  is, also when the integration is updated. Your own changes to a blueprint
  are kept, and an update of the integration does not change your
  automations.
- **Updating a blueprint.** To get the current version from GitHub, open the
  blueprint's menu on the Blueprints page and select **Re-import blueprint**.
  The automations made from it then use the new version.
- A blueprint that you delete is copied again the next time the integration
  starts.

The notifications are sent through the Home Assistant app on the phone.
Package, info board and intercom notifications go to every phone that has the
app, also to a phone that is added later. The consumption warning and the wind
reminder go to the phones you select in the form. The notification channel
and the persistent reminder are options of the Android app. A tap on a
notification opens something on Android and on iOS: for a sensor its details
with the history, on your default dashboard.

**Smart Place: package delivered.** Sends the PIN of the parcel box when a
package is delivered, that is when the sensor "Package delivery PIN" gets a
new value. A new notification replaces the previous one. A tap opens the PIN
sensor.

**Smart Place: new info board entry.** Sends the text of a new entry on the
building's info board. A tap on the notification opens the Smart Place web
app.

**Smart Place: consumption warning.** Sends a notification when today's
consumption of a meter reaches a level of its daily target: orange (from
60 %) or red (from 90 %). You select the meter's "target status" and "target
use" sensors and the phones. One automation covers both levels, each with its
own title; a level you deselect sends nothing. The notification for red
replaces the one for orange. A tap opens the "target use" sensor.

**Smart Place: intercom ringing.** Sends a notification with door buttons
when somebody rings, and presses the door's button entity when a button is
tapped: this opens a real door. You select the intercom sensors and the button
entities of the ground floor entrance, the front door and the garage entrance;
a door without a button entity gets no button. If the caller text names one
of the doors (the words to look for can be changed), only that door's button
is shown, otherwise all of them. A tap on the notification opens the intercom
page of the Smart Place dashboard. Make only one automation from this
blueprint and select all intercom sensors in it: every automation made from
it presses the door button when a notification button is tapped.

**Smart Place: high winds.** Shows a silent reminder on the selected phones
while the wind speed is above a limit. The reminder is sent again with every
new reading above the limit, so a reminder that was dismissed comes back. It
is removed, on every phone that has the app, when the wind speed falls below a
second, lower value. A tap opens the wind sensor. The sensor can be the Smart Place wind speed sensor or a
helper made from it, for example a Statistics helper with the average of
5 minutes. Nothing is stored between readings, so a restart of Home Assistant
in the middle does no harm. One case is not covered: if the wind speed falls
below the lower value while Home Assistant is stopped, the reminder stays
until the next time the wind speed falls below that value.

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

- [`DESIGN.md`](DESIGN.md) — protocol notes, architecture, decision log.
- [`IMPLEMENT.md`](IMPLEMENT.md) — implementation log, including the protocol
  points that still need a test on a real installation.

## License

[MIT](LICENSE)
