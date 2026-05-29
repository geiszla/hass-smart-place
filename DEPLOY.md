# Deploying Smart Place to Home Assistant OS on a Raspberry Pi

End-to-end instructions for installing this custom integration on a fresh
Home Assistant OS install and getting your first entities into the UI.

Assumes Home Assistant OS is already up and running on the Pi and reachable
on your LAN (e.g. `http://homeassistant.local:8123/`).

---

## 1. Grab the Smart Place URL token

You authenticate with the secret token that lives in the URL your
Smart Place app gives you. Treat it like a password — anyone with the
token can drive your building.

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

## 2. Install the integration files on the Pi

You need to drop `custom_components/smart_place/` into the Home Assistant
`/config/` directory. Pick **one** of the three paths below.

### Option A — HACS (recommended once published)

1. If HACS isn't already installed, follow
   [the HACS install guide](https://hacs.xyz/docs/use/download/download/).
2. In Home Assistant: **HACS → ⋮ → Custom repositories**.
3. Add `https://github.com/geiszla/hass-smart-place` with category
   **Integration**.
4. Find **Smart Place** in the HACS integrations list → **Download**.
5. Skip to step 3 (restart).

### Option B — Samba share (easiest for one-off installs)

1. Install the **Samba share** add-on (Settings → Add-ons → Add-on store →
   Samba share). Start it.
2. From your computer, browse to `\\homeassistant\config\` (Windows) or
   `smb://homeassistant.local/config/` (macOS/Linux).
3. Create `custom_components/` if it doesn't exist.
4. Copy the entire `custom_components/smart_place/` folder from this repo
   into it. The final layout must be:

   ```
   /config/custom_components/smart_place/
       __init__.py
       manifest.json
       config_flow.py
       const.py
       sensor.py
       binary_sensor.py
       button.py
       strings.json
       translations/en.json
   ```

### Option C — SSH

1. Install the **Advanced SSH & Web Terminal** add-on and start it.
2. From your workstation:

   ```bash
   scp -r custom_components/smart_place root@homeassistant.local:/config/custom_components/
   ```

   (Use `homeassistant.local` or the Pi's IP, whichever resolves on your
   LAN.)

---

## 3. Restart Home Assistant

Settings → System → top-right ⋮ → **Restart Home Assistant** → Restart.

Wait ~30 seconds for the front-end to come back.

---

## 4. Add the integration via the UI

1. Settings → **Devices & Services** → **Add Integration**.
2. Search for **Smart Place** and click it.
3. Paste the token from step 1 into **Smart Place URL token** and submit.

What happens behind the scenes: the config flow opens a real WebSocket
to the Smart Place server, waits for the bootstrap reply, and only saves
the entry if it succeeded. If it fails you'll see one of:

| Error | Meaning | Fix |
|---|---|---|
| *The token was rejected by Smart Place.* | Token is wrong or expired. | Re-copy from the Smart Place URL. Tokens can rotate — paste the latest one. |
| *Could not reach the Smart Place server.* | Network or DNS issue from the Pi. | Try `ping spr1.smartplace.ch` from the Pi's SSH terminal. |
| *Unexpected error. Check the Home Assistant log for details.* | Anything else. | Settings → System → Logs (token is auto-redacted from the log). |

---

## 5. Confirm everything came up

Open **Settings → Devices & Services → Smart Place**. You should see:

- A **Smart Place** device with the bulk of the entities.
- One **`<room>` climate** sub-device per climate zone (temperature +
  humidity grouped together).
- A **Connection** diagnostic binary sensor — should read **Connected**.
  This is the one entity that stays *available* even when the WS is down,
  so you can build automations on it (e.g. notify when it flips Off).
- Sensors for outdoor temperature, wind speed, package boxes, consumption
  charts, intercom, infoboard, person info — whichever the server pushed
  during the 2-second observation window after bootstrap.
- Binary sensors for rain / hail / wind alarms, scenes, and the
  `Any light on` / `Any blind closed` group rollups.
- Four **door** buttons (Front door, Ground floor entrance, Garage
  entrance, Mailbox) — these *send* `OEFFNER<n>` commands when pressed,
  so don't press them by accident. They're greyed out while the WS is
  disconnected.

Entity discovery is one-shot: it happens during integration setup from
whatever state the server has pushed. If a sensor ID first shows up
hours later, you'll need to reload the integration to surface it
(Devices & Services → Smart Place → ⋮ → Reload).

---

## 6. Day-to-day operation

- **Disconnects:** the client reconnects automatically with backoff.
  While disconnected, all entities except **Connection** show as
  *Unavailable* so dashboards don't display stale numbers.
- **Heartbeat:** every 60 s the client pings the server; if no pong
  arrives within 30 s it tears the WS down and reconnects — no action
  needed from you.
- **Token rotation:** if Smart Place ever invalidates your token you'll
  see a **Repairs** notification asking to *Re-authenticate Smart Place*.
  Paste the new token and that's it.
- **Logs:** Settings → System → Logs. Tokens are redacted from anything
  the integration writes — but if you turn on `homeassistant.components`
  debug logging you should still spot-check the output before sharing.

---

## 7. Upgrading

- **HACS:** HACS → Smart Place → **Update**, then restart Home Assistant.
- **Samba/SSH:** overwrite `/config/custom_components/smart_place/` with
  the new version of the folder, then restart Home Assistant.

The config entry persists across upgrades; you won't need to re-enter
the token.

---

## 8. Uninstalling

1. Settings → Devices & Services → Smart Place → ⋮ → **Delete**.
2. Restart Home Assistant.
3. (Optional) delete `/config/custom_components/smart_place/`.
