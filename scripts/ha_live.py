"""Query a live Home Assistant instance for Smart Place integration data.

A read-mostly CLI over the HA REST + WebSocket APIs, scoped to the
``smart_place`` integration. It fetches data — config entry status,
devices, entities and their states, system logs, a live state stream —
and leaves any judgement about health to the caller. The only mutating
commands are ``reload`` and ``restart``, which affect the HA instance
itself (never the building).

Authentication uses a long-lived access token from an admin HA user
(HA UI → your user → Security → Long-lived access tokens). Put it in
``.env`` at the repo root as ``HASS_TOKEN=...`` (auto-loaded, like
``SMART_PLACE_TOKEN``), or export it. ``HASS_URL`` overrides the URL.

Run via the wrapper: ``./scripts/ha-live <command> [options]``.

Note: the device/entity-registry and system-log WebSocket commands are
the frontend-internal API (``config/device_registry/list`` etc.) — they
have been stable for years but are not formally documented, so if this
script breaks after an HA upgrade, re-check the command names against
``homeassistant/components/config/`` in home-assistant/core.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
import json
import sys
import time
from typing import Any

import aiohttp
import click
from dotenv import load_dotenv

DOMAIN = "smart_place"
DEFAULT_URL = "http://homeassistant.local:8123"

REST_TIMEOUT = aiohttp.ClientTimeout(total=15)
WS_TIMEOUT = 15.0
# Entry reload re-runs the Smart Place bootstrap (CONFIG_FLOW_TIMEOUT=60s)
# plus the 2s observation window, so give it headroom.
RELOAD_TIMEOUT = 90.0
RESTART_DOWN_TIMEOUT = 30.0
RESTART_UP_TIMEOUT = 180.0


class ApiError(Exception):
    """Fatal problem talking to Home Assistant (connectivity / auth / protocol)."""


class HassApi:
    """Minimal REST + WebSocket client for one Home Assistant instance."""

    def __init__(self, url: str, token: str, session: aiohttp.ClientSession) -> None:
        """Store connection parameters; no I/O happens until the first call."""
        self.url = url.rstrip("/")
        self._token = token
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def get(self, path: str) -> Any:
        """GET a REST endpoint and return the decoded JSON (or raw text)."""
        async with self._session.get(f"{self.url}{path}", headers=self._headers) as response:
            response.raise_for_status()
            if response.content_type == "application/json":
                return await response.json()
            return await response.text()

    async def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        """POST to a REST endpoint and return the decoded JSON (or raw text)."""
        async with self._session.post(f"{self.url}{path}", headers=self._headers, json=payload) as response:
            response.raise_for_status()
            if response.content_type == "application/json":
                return await response.json()
            return await response.text()

    async def ws_command(self, command_type: str, **payload: Any) -> Any:
        """Send one WebSocket command and return its ``result`` payload."""
        ws = await self._connect_ws()
        self._next_id += 1
        msg_id = self._next_id
        await ws.send_json({"id": msg_id, "type": command_type, **payload})
        while True:
            async with asyncio.timeout(WS_TIMEOUT):
                reply = await self._receive_json(ws)
            if reply.get("id") != msg_id or reply.get("type") != "result":
                continue  # stray event from an earlier subscription
            if not reply.get("success"):
                error = reply.get("error", {})
                raise ApiError(f"WS command {command_type!r} failed: {error.get('code')}: {error.get('message')}")
            return reply.get("result")

    async def events(self, event_type: str) -> Any:
        """Subscribe to an event type and yield event payloads indefinitely."""
        await self.ws_command("subscribe_events", event_type=event_type)
        ws = self._ws
        assert ws is not None
        while True:
            msg = await self._receive_json(ws)
            if msg.get("type") == "event":
                yield msg["event"]

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        """Open + authenticate the WebSocket connection (idempotent)."""
        if self._ws is not None and not self._ws.closed:
            return self._ws
        ws = await self._session.ws_connect(f"{self.url}/api/websocket", heartbeat=30)
        async with asyncio.timeout(WS_TIMEOUT):
            first = await self._receive_json(ws)
            if first.get("type") != "auth_required":
                raise ApiError(f"unexpected WebSocket greeting: {first.get('type')!r}")
            await ws.send_json({"type": "auth", "access_token": self._token})
            reply = await self._receive_json(ws)
        if reply.get("type") != "auth_ok":
            raise ApiError("WebSocket auth failed — is HASS_TOKEN valid (and from an admin user)?")
        self._ws = ws
        return ws

    @staticmethod
    async def _receive_json(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        """Read one text frame and decode it, failing loudly on close/binary."""
        msg = await ws.receive()
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ApiError(f"WebSocket closed unexpectedly ({msg.type.name})")
        return json.loads(msg.data)


def _print_json(data: Any) -> None:
    """Pretty-print a JSON-serializable value."""
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))


def _mentions_smart_place(entry: dict[str, Any]) -> bool:
    """Return True if a system_log entry involves this integration."""
    haystack = " ".join(
        [
            entry.get("name") or "",
            entry.get("exception") or "",
            str(entry.get("source") or ""),
            *(entry.get("message") or []),
        ]
    )
    return "smart_place" in haystack


async def _domain_entries(api: HassApi) -> list[dict[str, Any]]:
    """Return all smart_place config entries on the instance."""
    entries = await api.get("/api/config/config_entries/entry")
    found = [entry for entry in entries if entry["domain"] == DOMAIN]
    if not found:
        raise ApiError(f"no {DOMAIN} config entry found — add the integration first (see DEPLOY.md §4)")
    return found


async def _domain_registry_entities(api: HassApi) -> list[dict[str, Any]]:
    """Return the entity-registry entries belonging to smart_place."""
    entry_ids = {entry["entry_id"] for entry in await _domain_entries(api)}
    registry = await api.ws_command("config/entity_registry/list")
    return [e for e in registry if e.get("config_entry_id") in entry_ids]


async def _states_by_id(api: HassApi) -> dict[str, dict[str, Any]]:
    """Return all current states keyed by entity_id."""
    return {s["entity_id"]: s for s in await api.get("/api/states")}


def _entry_label(entry: dict[str, Any]) -> str:
    """Return a one-line label for a config entry, including its state."""
    reason = f" — {entry['reason']}" if entry.get("reason") else ""
    return f"“{entry['title']}” ({entry['entry_id'][:8]}…): {entry['state']}{reason}"


def _execute(obj: dict[str, str], handler: Callable[[HassApi], Awaitable[None]]) -> None:
    """Run one async command handler with a shared session + error handling."""
    if not obj["token"]:
        click.echo(
            "HASS_TOKEN is required. Create a long-lived access token in the HA UI\n"
            "(your user → Security → Long-lived access tokens) and put it in .env\n"
            "as HASS_TOKEN=... or export it.",
            err=True,
        )
        sys.exit(2)

    async def runner() -> None:
        async with aiohttp.ClientSession(timeout=REST_TIMEOUT) as session:
            await handler(HassApi(obj["url"], obj["token"], session))

    try:
        asyncio.run(runner())
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            click.echo("Authentication failed (401/403) — HASS_TOKEN is wrong, revoked, or not an admin's.", err=True)
        else:
            click.echo(f"HTTP error from Home Assistant: {err.status} {err.message}", err=True)
        sys.exit(2)
    except (aiohttp.ClientError, TimeoutError, OSError) as err:
        message = (
            f"Could not reach Home Assistant at {obj['url']}: {err}\n"
            "Is the box up and on the LAN? Override the URL with HASS_URL or --url."
        )
        if ".local" in obj["url"]:
            message += (
                "\nWSL2 cannot resolve .local mDNS names even when Windows can —"
                " get the IP from Windows with\n"
                '  powershell.exe -NoProfile -Command "ping -4 -n 1 homeassistant.local"\n'
                "and set HASS_URL=http://<ip>:8123 in .env."
            )
        click.echo(message, err=True)
        sys.exit(2)
    except ApiError as err:
        click.echo(str(err), err=True)
        sys.exit(2)


@click.group(name="ha-live")
@click.option(
    "--url",
    envvar="HASS_URL",
    default=DEFAULT_URL,
    show_default=True,
    help="Base URL of the Home Assistant instance.",
)
@click.option(
    "--token",
    envvar="HASS_TOKEN",
    default=None,
    help="Long-lived access token (defaults to $HASS_TOKEN; .env is auto-loaded).",
)
@click.pass_context
def cli(ctx: click.Context, url: str, token: str | None) -> None:
    """Query a live Home Assistant box for Smart Place integration data."""
    # The token is checked in _execute, not here, so `<command> --help` works
    # without one.
    ctx.obj = {"url": url, "token": token}


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_obj
def status(obj: dict[str, str], as_json: bool) -> None:
    """Show HA version, Smart Place config entries, and headline counts."""

    async def handler(api: HassApi) -> None:
        config = await api.get("/api/config")
        entries = await _domain_entries(api)
        entry_ids = {entry["entry_id"] for entry in entries}
        devices = await api.ws_command("config/device_registry/list")
        entry_devices = [d for d in devices if entry_ids & set(d.get("config_entries") or [])]
        registry = [
            e for e in await api.ws_command("config/entity_registry/list") if e.get("config_entry_id") in entry_ids
        ]
        enabled = [e for e in registry if not e.get("disabled_by")]
        states = await _states_by_id(api)
        connection = next(
            (
                states[e["entity_id"]]
                for e in enabled
                if e["entity_id"].startswith("binary_sensor.")
                and e["entity_id"] in states
                and states[e["entity_id"]]["attributes"].get("device_class") == "connectivity"
            ),
            None,
        )
        unavailable = [e["entity_id"] for e in enabled if states.get(e["entity_id"], {}).get("state") == "unavailable"]

        if as_json:
            _print_json(
                {
                    "ha_version": config["version"],
                    "location_name": config["location_name"],
                    "config_entries": entries,
                    "device_count": len(entry_devices),
                    "entity_count": len(enabled),
                    "disabled_entity_count": len(registry) - len(enabled),
                    "connection_sensor": connection,
                    "unavailable_entities": unavailable,
                }
            )
            return

        print(f"Home Assistant {config['version']} (“{config['location_name']}”) at {api.url}")
        for entry in entries:
            print(f"  config entry {_entry_label(entry)}")
        disabled_note = f" (+{len(registry) - len(enabled)} disabled)" if len(registry) > len(enabled) else ""
        print(f"  {len(entry_devices)} devices, {len(enabled)} enabled entities{disabled_note}")
        if connection is None:
            print("  Connection sensor: not found")
        else:
            print(f"  Connection sensor ({connection['entity_id']}): {connection['state']}")
        if unavailable:
            print(f"  unavailable entities ({len(unavailable)}): {', '.join(unavailable[:8])}")

    _execute(obj, handler)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output the raw device-registry entries as JSON.")
@click.pass_obj
def devices(obj: dict[str, str], as_json: bool) -> None:
    """List Smart Place devices from the device registry."""

    async def handler(api: HassApi) -> None:
        entry_ids = {entry["entry_id"] for entry in await _domain_entries(api)}
        registry = await api.ws_command("config/device_registry/list")
        entry_devices = [d for d in registry if entry_ids & set(d.get("config_entries") or [])]
        if as_json:
            _print_json(entry_devices)
            return
        for device in sorted(entry_devices, key=lambda d: (d.get("via_device_id") is not None, d.get("name") or "")):
            kind = "sub-device" if device.get("via_device_id") else "device"
            details = ", ".join(
                str(value)
                for value in (device.get("manufacturer"), device.get("model"), device.get("area_id"))
                if value
            )
            print(f"  {device.get('name')}  [{kind}] {f'({details})' if details else ''}  id={device['id']}")

    _execute(obj, handler)


@cli.command()
@click.option("--disabled", "include_disabled", is_flag=True, help="Include disabled entities.")
@click.option("--json", "as_json", is_flag=True, help="Output merged registry + state objects as JSON.")
@click.pass_obj
def entities(obj: dict[str, str], include_disabled: bool, as_json: bool) -> None:
    """List Smart Place entities with their current state values."""

    async def handler(api: HassApi) -> None:
        registry = await _domain_registry_entities(api)
        if not include_disabled:
            shown = [e for e in registry if not e.get("disabled_by")]
            hidden = len(registry) - len(shown)
        else:
            shown, hidden = registry, 0
        states = await _states_by_id(api)

        if as_json:
            _print_json(
                [
                    {**entry, "state_object": states.get(entry["entity_id"])}
                    for entry in sorted(shown, key=lambda e: e["entity_id"])
                ]
            )
            return

        by_platform = Counter(e["entity_id"].split(".", 1)[0] for e in shown)
        breakdown = ", ".join(f"{count} {platform}" for platform, count in sorted(by_platform.items()))
        print(f"  {len(shown)} entities ({breakdown})")
        for entry in sorted(shown, key=lambda e: e["entity_id"]):
            state = states.get(entry["entity_id"])
            value = state["state"] if state else "(no state)"
            if entry.get("disabled_by"):
                value = f"(disabled by {entry['disabled_by']})"
            name = entry.get("name") or entry.get("original_name") or ""
            unit = (state or {}).get("attributes", {}).get("unit_of_measurement")
            print(f"  {entry['entity_id']:<55} {value:<16} {name}{f' [{unit}]' if unit else ''}")
        if hidden:
            print(f"  (+{hidden} disabled entities hidden — use --disabled to include)")

    _execute(obj, handler)


@cli.command()
@click.argument("entity_ids", nargs=-1, required=True)
@click.pass_obj
def state(obj: dict[str, str], entity_ids: tuple[str, ...]) -> None:
    """Show the full state object(s) — value, attributes, timestamps — as JSON."""

    async def handler(api: HassApi) -> None:
        for entity_id in entity_ids:
            try:
                _print_json(await api.get(f"/api/states/{entity_id}"))
            except aiohttp.ClientResponseError as err:
                if err.status != 404:
                    raise
                click.echo(f"{entity_id}: not found", err=True)

    _execute(obj, handler)


@cli.command()
@click.option("--all", "show_all", is_flag=True, help="Show every system-log entry, not just smart_place ones.")
@click.option("--json", "as_json", is_flag=True, help="Output the raw log entries as JSON.")
@click.pass_obj
def logs(obj: dict[str, str], show_all: bool, as_json: bool) -> None:
    """Show system-log entries (with tracebacks) for smart_place."""

    async def handler(api: HassApi) -> None:
        log_entries = await api.ws_command("system_log/list")
        if not show_all:
            log_entries = [e for e in log_entries if _mentions_smart_place(e)]
        if as_json:
            _print_json(log_entries)
            return
        if not log_entries:
            scope = "" if show_all else "smart_place "
            print(f"  (no {scope}log entries — the buffer is small and clears on every core restart)")
        for entry in log_entries:
            stamp = datetime.fromtimestamp(entry["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
            count = f" ×{entry['count']}" if entry.get("count", 1) > 1 else ""
            print(f"\n  [{stamp}] {entry.get('level')}{count} {entry.get('name')}")
            for message in entry.get("message") or []:
                print(f"    {message}")
            if entry.get("exception"):
                for line in entry["exception"].rstrip().splitlines():
                    print(f"    {line}")

    _execute(obj, handler)


@cli.command()
@click.option("--seconds", default=60, show_default=True, help="How long to stream before exiting.")
@click.pass_obj
def watch(obj: dict[str, str], seconds: int) -> None:
    """Stream live state changes of Smart Place entities."""

    async def handler(api: HassApi) -> None:
        registry = await _domain_registry_entities(api)
        watched = {e["entity_id"] for e in registry if not e.get("disabled_by")}
        print(f"Watching {len(watched)} entities for {seconds}s (Ctrl-C to stop)...")
        try:
            async with asyncio.timeout(seconds):
                async for event in api.events("state_changed"):
                    data = event.get("data", {})
                    if data.get("entity_id") not in watched:
                        continue
                    old = (data.get("old_state") or {}).get("state", "?")
                    new = (data.get("new_state") or {}).get("state", "?")
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"  [{stamp}] {data['entity_id']}: {old} → {new}")
        except TimeoutError:
            pass

    _execute(obj, handler)


@cli.command()
@click.pass_obj
def reload(obj: dict[str, str]) -> None:
    """Reload the Smart Place config entry (re-runs one-shot entity discovery)."""

    async def handler(api: HassApi) -> None:
        for entry in await _domain_entries(api):
            entry_id = entry["entry_id"]
            print(f"Reloading config entry “{entry['title']}”...")
            result = await api.post(f"/api/config/config_entries/entry/{entry_id}/reload")
            if isinstance(result, dict) and result.get("require_restart"):
                print("  entry cannot be reloaded in place — a core restart is required (use restart)")
                continue
            deadline = time.monotonic() + RELOAD_TIMEOUT
            while time.monotonic() < deadline:
                current = next((e for e in await _domain_entries(api) if e["entry_id"] == entry_id), None)
                if current is not None and current["state"] == "loaded":
                    break
                await asyncio.sleep(3)
            current = next((e for e in await _domain_entries(api) if e["entry_id"] == entry_id), None)
            if current is not None:
                print(f"  config entry {_entry_label(current)}")

    _execute(obj, handler)


@cli.command()
@click.pass_obj
def restart(obj: dict[str, str]) -> None:
    """Restart Home Assistant core and wait until the API is back up."""

    async def handler(api: HassApi) -> None:
        print("Restarting Home Assistant core...")
        await api.post("/api/services/homeassistant/restart")

        went_down = False
        deadline = time.monotonic() + RESTART_DOWN_TIMEOUT
        while time.monotonic() < deadline:
            try:
                await api.get("/api/")
            except (aiohttp.ClientError, TimeoutError, OSError):
                went_down = True
                break
            await asyncio.sleep(2)
        if not went_down:
            print("  API never went down — the restart may not have happened")

        deadline = time.monotonic() + RESTART_UP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                await api.get("/api/")
            except (aiohttp.ClientError, TimeoutError, OSError):
                await asyncio.sleep(3)
            else:
                print("  API is back up")
                for entry in await _domain_entries(api):
                    print(f"  config entry {_entry_label(entry)}")
                return
        raise ApiError(f"API did not come back within {RESTART_UP_TIMEOUT:.0f}s of the restart")

    _execute(obj, handler)


@cli.command(name="get")
@click.argument("path")
@click.pass_obj
def get_cmd(obj: dict[str, str], path: str) -> None:
    """GET an arbitrary REST path (e.g. /api/config) and print the response."""

    async def handler(api: HassApi) -> None:
        result = await api.get(path)
        if isinstance(result, str):
            print(result)
        else:
            _print_json(result)

    _execute(obj, handler)


@cli.command(name="ws")
@click.argument("command_type")
@click.argument("payload", required=False)
@click.pass_obj
def ws_cmd(obj: dict[str, str], command_type: str, payload: str | None) -> None:
    """Send an arbitrary WebSocket command and print its result.

    PAYLOAD is an optional JSON object merged into the message, e.g.:
    ./scripts/ha-live ws config_entries/get '{"domain": "smart_place"}'
    """

    async def handler(api: HassApi) -> None:
        extra = json.loads(payload) if payload else {}
        _print_json(await api.ws_command(command_type, **extra))

    _execute(obj, handler)


if __name__ == "__main__":
    # Auto-load .env so HASS_TOKEN/HASS_URL live next to SMART_PLACE_TOKEN;
    # exported env vars win over .env values (same policy as sp-cli).
    load_dotenv()
    cli()
