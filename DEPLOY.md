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

### Option A — HACS (recommended)

1. If HACS isn't already installed, follow
   [the HACS install guide](https://hacs.xyz/docs/use/download/download/).
2. In Home Assistant: **HACS → ⋮ → Custom repositories**.
3. Add `https://github.com/geiszla/hass-smart-place` with category
   **Integration**.
4. Find **Smart Place** in the HACS integrations list → **Download**.

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

Settings → System → top-right ⋮ → **Restart Home Assistant** → Restart.

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
  should read **Connected**. This is the one entity that stays *available*
  even when the connection is down, so you can build automations on it
  (e.g. notify when it flips Off). The floor plan image is here too.
- One device per **heating zone**, named after the room: a climate entity,
  the temperature sensor and a (disabled) humidity sensor.
- One device per **light**, **socket** and **blind**, named as in the Smart
  Place app.
- One device per level group, named after it, such as **Ventilation** (a
  fan).
- Category devices: **Groups** ("All blinds up/down"; "All lights" is
  disabled by default), **Intercom** (doorbell mute), **Weather**,
  **Energy**, **Cameras**, and **Doors**.
- **Scenes** and scene switches have no device: they are named like the
  scene alone ("Night"), so the voice assistant recognises them. Find
  them in the integration's entity list or on the dashboard's Scenes view.
- Four **door** buttons (Front door, Ground floor entrance, Garage
  entrance, Mailbox) — disabled by default. They *send* `OEFFNER<n>` when
  pressed and open real doors; enable only the ones you want.
- A **Smart Place** entry in the sidebar: the plan dashboard.

Entities are added when the server first delivers their device or value —
normally all at once at setup. A device added in the Smart Place app later
appears after the next reconnect or restart. A device removed there keeps its
entity; delete the device in Home Assistant when you no longer want it.

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
- **Heartbeat:** every 60 s the client pings the server; if no pong
  arrives within 30 s it tears the connection down and reconnects — no
  action needed from you.
- **Token rotation:** if Smart Place ever invalidates your token you'll
  see a **Repairs** notification asking to *Re-authenticate Smart Place*.
  Paste the new token and that's it.
- **Logs:** Settings → System → Logs. Tokens and the installation's
  remote-access passwords are removed from anything the integration
  writes. Commands sent to Smart Place are logged at INFO level
  (`command issued: ...`) under the logger `smart_place_client`.

---

## 8. Upgrading

- **HACS:** HACS → Smart Place → **Update**, then restart Home Assistant.
  From this repository's checkout: `./scripts/ha-live update` followed by
  `./scripts/ha-live restart`.
- **By hand:** overwrite `/config/custom_components/smart_place/` with
  the new version of the whole folder, then restart Home Assistant.

The config entry persists across upgrades; you won't need to re-enter
the token. Existing entities keep their ids.

---

## 9. Uninstalling

1. Settings → Devices & Services → Smart Place → ⋮ → **Delete**.
2. Restart Home Assistant.
3. (Optional) remove the repository in HACS or delete
   `/config/custom_components/smart_place/`.
