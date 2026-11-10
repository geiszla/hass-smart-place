# Installing Smart Place on Home Assistant

Instructions for installing this custom integration on a Home Assistant
instance and getting the entities into the UI.

Requires Home Assistant **2026.11** or newer, reachable on your network (e.g.
`http://homeassistant.local:8123/`).

---

## 1. Grab the Smart Place URL token

You authenticate with the secret token that lives in the URL your
Smart Place app gives you. Treat it like a password — anyone with the
token can control your home.

1. Open the Smart Place web app in a desktop browser (the same URL you
   normally use from the mobile app). The address bar will look like:

   ```
   https://spr1.smartplace.ch:8770/Start5?<LONG-OPAQUE-TOKEN>
   ```

2. Copy **everything after the `?`** — that's the token. It's typically
   60+ characters of letters/digits/symbols, no spaces.

3. Keep it somewhere only you can read (password manager). Do not paste
   it into chats, screenshots, or commit it to git.

---

## 2. Before the first start: entity ids

By default Home Assistant puts the area name in front of new entity ids
(`light.living_room_dining_room_kitchen_kitchen_lights`). The integration
gives every light, socket and blind its own device and suggests an area for
it, so the ids get long. If you prefer `light.kitchen_lights`, remove "area"
from Home Assistant's "Entity ID format" setting now. Entities that already
exist keep their ids.

---

## 3. Install the integration files

### Option A — the Marketplace (recommended)

Home Assistant 2026.11 has a built-in **Marketplace** for custom
integrations. It replaces HACS, which cannot be installed on 2026.11 or
newer. The Marketplace is for administrators and is not in the sidebar.

1. Open **Settings → Marketplace**. On first use it shows a warning; read
   and accept it. Until an administrator has accepted it, Home Assistant
   does not install or update anything from the Marketplace.
2. Select **Add from link** in the upper-right corner, enter
   `https://github.com/geiszla/hass-smart-place` under **GitHub link** and
   select **Add**. If the Marketplace asks for the type, choose the
   integration type and select **Add** again. If GitHub is not connected
   yet, the Marketplace asks you to connect it: select **Continue**, open
   the GitHub link it shows, enter the code and approve.
3. Open **Smart Place** in the list, select **Install**, check the version
   and select **Install** again.

**Coming from HACS:** there is nothing to do. When Home Assistant starts
after the update to 2026.11, the Marketplace takes over what HACS had:
installed content, custom repositories and update entities (with the same
entity ids). HACS itself is removed. The old "Get HACS" installer add-on
is no longer needed (it only prints an error).

### Option B — copy the folder by hand

Home Assistant OS has no SSH by default. Use the **Samba share** add-on
(Settings → Add-ons → Add-on store), the File editor add-on or Studio Code
Server.

1. Open the `config` share (`\\homeassistant\config\` on Windows,
   `smb://homeassistant.local/config/` on macOS/Linux).
2. Create `custom_components/` if it doesn't exist.
3. Copy the **whole** `custom_components/smart_place/` folder from this
   repository into it, with all its files and sub-folders
   (`smart_place_client/`, `translations/`, `brand/`). Do not pick single
   files: the integration needs all of them.

---

## 4. Restart Home Assistant

Restart when the Marketplace asks for it, or: Settings → System →
top-right ⋮ → **Restart Home Assistant** → Restart.

---

## 5. Add the integration via the UI

1. Settings → **Devices & Services** → **Add Integration**.
2. Search for **Smart Place** and click it.
3. Paste the token from step 1 into **Smart Place URL token** and submit.

What happens behind the scenes: the config flow opens a real WebSocket
to the Smart Place server, waits for the bootstrap reply, and only saves
the entry if it succeeded. If it fails you'll see one of:

| Error | Meaning | Fix |
|---|---|---|
| *The token was rejected by Smart Place.* | Token is wrong or expired. | Re-copy from the Smart Place URL. Tokens can rotate — paste the latest one. |
| *Could not reach the Smart Place server.* | Network or DNS issue from the Home Assistant machine, or the token is wrong (the server does not answer a wrong token). | Check the token and the internet connection. |
| *Unexpected error. Check the Home Assistant log for details.* | Anything else. | Settings → System → Logs (the token is removed from the log). |

After the entry is created, Home Assistant may ask for the area of the new
devices. The areas are filled in from the room names of your heating zones
(see "Rooms" in the README); change them there if you like.

---

## 6. Confirm everything came up

Open **Settings → Devices & Services → Smart Place**. You should see:

- A **Smart Place** device with the **Connection** diagnostic sensor — it
  should read **Connected**. It stays *available* when the connection is
  down, so you can build automations on it (e.g. notify when it flips
  Off). The floor plan image is here too; once fetched it stays available
  as well, and so does the electricity price sensor. Every other entity
  shows *Unavailable* while the connection is down.
- One device per **heating zone**, named after the room: a climate entity,
  the temperature sensor and a (disabled) humidity sensor.
- One device per **light**, **socket** and **blind**, named as in the Smart
  Place app. Every blind's device also has the configuration switch
  **Lower after weather alarm**, off by default (section 7).
- One device per level group, named after it, such as **Ventilation** (a
  fan). A level group is three or more on/off scenes with the same name
  and the numbers 1..N of which exactly one is on when the integration
  first sees them; otherwise the scenes are switches.
- Category devices: **Groups** ("All blinds up/down"; "All lights" and
  the button of a blinds group other than "All" are disabled by default),
  **Intercom** (doorbell mute), **Weather**, **Energy**, **Cameras**, and
  **Doors**.
- **Scenes** and scene switches have no device: they are named like the
  scene alone ("Night"), so the voice assistant recognises them. Find
  them in the integration's entity list or on the dashboard's Scenes view.
  A scene the Smart Place app does not display is disabled by default.
- Four **door** buttons (Front door, Ground floor entrance, Garage
  entrance, Mailbox) — disabled by default. They *send* `OEFFNER<n>` when
  pressed and open real doors; enable only the ones you want.
- A **Smart Place** entry in the sidebar: the plan dashboard.
- A **Configure** button on the integration: the options (protected
  sockets, and the waiting time of "Lower after weather alarm").

Entities are added when the server first delivers their device or value —
normally all at once at setup. A device added in the Smart Place app later
appears after the next reconnect or restart. A device removed there keeps its
entity; delete the device in Home Assistant when you no longer want it. Home
Assistant refuses that until the integration was reloaded or Home Assistant
was restarted: until then the integration still provides the device.

---

## 7. Day-to-day operation

- **Disconnects:** the client reconnects automatically with backoff.
  While disconnected, all entities except **Connection** (and the price
  sensor and the plan image) show as *Unavailable* so dashboards don't
  display stale numbers. One warning is logged when the connection is
  lost.
- **After a reconnect** lights, blinds and heating read *Unknown* for a
  moment, until the server has sent their states again. A turn-on or
  turn-off in that moment is refused.
- **"Turn off" for a whole area** (`homeassistant.turn_off`, `turn_on`,
  `toggle`) acts on the area's lights, sockets and other entities, but not
  on its heating: the thermostat has no "turn off". Home Assistant reports
  an error for it ("does not support action climate.turn_off") and an
  automation stops at that step. Use `light.turn_off` / `switch.turn_off`
  for the area, or name the entities. Heating is switched off only on the
  thermostat itself (mode *Off*).
- **Protected sockets:** select the sockets that must never be switched
  under **Configure → Protected sockets**. Home Assistant then sends no
  command for them; it takes effect at once. Smart Place scenes, the "All
  lights" group, wall buttons and the Smart Place app can still switch
  them. See "Protected sockets" in the README.
- **Blinds during a weather alarm:** a blind held by a rain, wind or hail
  alarm reads *open*, has the attribute `blocked: true` and offers no
  command until the alarm ends. See "Blinds and weather alarms" in the
  README.
- **Lower after weather alarm:** with this switch on for a blind, the
  integration sends the blind's down command when it sees the alarm end
  (at once, or after the waiting time set under **Configure**). **This is
  the one place where the integration sends a command without a user
  action at that moment.** It is off by default. Each such command is
  logged at INFO level (`Lowering <blind> ...`).
- **Heartbeat:** every 60 s the client pings the server; if no pong
  arrives within 30 s it tears the connection down and reconnects — no
  action needed from you.
- **Token rotation:** if Smart Place ever invalidates your token you'll
  see a **Repairs** notification asking to *Re-authenticate Smart Place*.
  Paste the new token and that's it.
- **Logs:** Settings → System → Logs. Tokens and the installation's
  remote-access passwords are removed from anything the integration
  writes. Commands sent to Smart Place are logged at INFO level
  (`command issued: ...`) under the logger `smart_place_client`; why the
  integration lowered a blind by itself, and that a command for a
  protected socket was not sent, under `custom_components.smart_place`.
  Home Assistant shows INFO lines only when the level of these loggers is
  set to `info` (`logger:` in `configuration.yaml`).

---

## 8. Upgrading

- **Marketplace:** the installed repository has an update entity, and an
  update shows under **Settings → Updates**. Install it there, then restart
  Home Assistant. The Marketplace looks for a new version of a repository
  added from a link every 48 hours, and only while GitHub is connected.
  From this repository's checkout: `./scripts/ha-live update` followed by
  `./scripts/ha-live restart`.
- **By hand:** overwrite `/config/custom_components/smart_place/` with
  the new version of the whole folder, then restart Home Assistant.

The config entry persists across upgrades; you won't need to re-enter
the token. Existing entities keep their ids, and the options (protected
sockets, waiting time, "Lower after weather alarm" switches) are kept.

---

## 9. Uninstalling

1. Settings → Devices & Services → Smart Place → ⋮ → **Delete**. This also
   deletes the options: after adding the integration again, protect your
   sockets and turn the "Lower after weather alarm" switches on again.
2. Restart Home Assistant.
3. (Optional) remove the repository in the Marketplace or delete
   `/config/custom_components/smart_place/`.
