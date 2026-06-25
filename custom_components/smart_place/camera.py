"""Camera platform for Smart Place — the door-intercom entrance cameras.

The vendor SPA shows up to four live entrance cameras on its intercom
screen. Each is a Motion-JPEG stream (a ZoneMinder ``nph-zms`` feed)
that the Smart Place server reverse-proxies on the routed connection's
HTTP port — ``porthttp = routed_port - 1`` — at the per-camera
``/linkmap<n>`` path carried in the ``GlobalGsa`` reply
(``state.gsa_cameras``). Verified 2026-06-11 by driving a real browser
straight to the endpoint: the proxy needs no per-request auth — a plain
GET returns ``multipart/x-mixed-replace`` — so we can consume it with a
plain HTTP fetch. The twist is the routed port is per-session *and* its
lease for new connections lapses long before the app WS drops: the WS
survives for hours on its already-established connection, but a fresh
camera GET to that session's proxy starts failing once the lease lapses
(diagnosed 2026-06-25 on the live box). So the URL is built from a
*camera route* the client manages separately and re-mints on demand
(``client.camera_base_url`` / ``client.refresh_camera_route``); when a
fetch fails here we re-discover once and retry, so a ring during a
stale window still shows a picture.

Read-only by construction: fetching an image or stream issues an HTTP
GET against the proxy, never an SPA command, so this is safe under the
``smart-place-observe-only`` memory — unlike the door-opener buttons,
nothing here can actuate a door.

We deliberately implement a plain :class:`Camera` with the public
``async_aiohttp_proxy_stream`` helper rather than subclassing the
``mjpeg`` integration's ``MjpegCamera`` — importing another
integration's code would force a manifest dependency on ``mjpeg``
(hassfest), and the proxying primitive is small enough to use directly.
Proxying the stream ourselves (rather than ``async_aiohttp_proxy_web``)
also lets us pre-flight the upstream status so a stale route can be
re-discovered before we hand the stream to the frontend.

Discovery is observation-based: one camera entity per id present in
``state.gsa_cameras`` at setup time (see ``__init__.py``'s observation
window). A camera that first appears later needs an HA reload to surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components.camera import Camera
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream, async_get_clientsession

from . import SmartPlaceData, category_device_info
from .const import DOMAIN

if TYPE_CHECKING:
    from aiohttp import web
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Snapshot fetch: read the live MJPEG stream just long enough to slice
# out one complete JPEG frame (SOI ``FF D8`` … EOI ``FF D9``). The cap
# guards against a feed that never yields a frame boundary; the timeout
# bounds a stalled proxy. Frames on this installation are ~30-60 KB.
_SNAPSHOT_TIMEOUT: float = 10.0
_SNAPSHOT_CHUNK: int = 16384
_SNAPSHOT_MAX_BYTES: int = 4_000_000

_JPEG_SOI: bytes = b"\xff\xd8"
_JPEG_EOI: bytes = b"\xff\xd9"

# The vendor's GlobalGsa door-opener labels do NOT line up with the
# physical entrance cameras on this installation (verified 2026-06-12
# against the live feeds — the opener ids are mailbox/garage-swapped
# versus the camera ids, and id 4's opener reads "apartment entrance"
# while the camera is the garage entrance). So camera names are pinned
# here by GlobalGsa camera id — the same installation-specific approach
# the door buttons take. Ids absent from this map fall back to the
# vendor opener label (a reasonable default on other installations),
# then to a bare "Camera N".
_CAMERA_NAMES: dict[int, str] = {
    1: "Ground floor entrance",
    2: "Garage",
    3: "Mailbox",
    4: "Garage entrance",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one camera per intercom feed seen in the GlobalGsa reply."""
    data: SmartPlaceData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        SmartPlaceIntercomCamera(entry, data, camera_id, link)
        for camera_id, link in sorted(data.state.gsa_cameras.items())
    )


class SmartPlaceIntercomCamera(Camera):
    """One door-intercom entrance camera, served as a proxied MJPEG stream."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        data: SmartPlaceData,
        camera_id: int,
        link: str,
    ) -> None:
        """Wire the camera's name (from the paired door label), id, and link."""
        super().__init__()
        self._data = data
        self._link = link
        # Pinned name for this installation's cameras, falling back to
        # the vendor opener label then the bare id (see _CAMERA_NAMES).
        label = _CAMERA_NAMES.get(camera_id) or data.state.gsa_door_labels.get(camera_id)
        self._attr_name = f"{label} camera" if label else f"Camera {camera_id}"
        self._attr_unique_id = f"{entry.entry_id}_camera_{camera_id}"
        self._attr_device_info = category_device_info(entry, "Cameras")

    @property
    def available(self) -> bool:
        """Match the rest of the integration: unavailable while the WS is down."""
        return self._data.is_healthy

    async def async_added_to_hass(self) -> None:
        """Push availability changes (WS up/down) to HA like the other entities."""
        self._data.listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Drop the push-update subscription on entity removal."""
        with contextlib.suppress(ValueError):
            self._data.listeners.remove(self.async_write_ha_state)

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a single JPEG snapshot, sliced from the live MJPEG stream.

        The proxy only serves the multipart stream (no single-shot URL),
        so we read it until the first complete JPEG frame and return that.
        Tries the current camera route; if the fetch fails or yields no
        frame, the routed port's lease has likely lapsed, so we
        re-discover a fresh route once and retry.
        """
        websession = async_get_clientsession(self.hass)
        for attempt in range(2):
            url = await self._data.client.camera_base_url(self._link)
            if url is None:
                return None
            try:
                async with asyncio.timeout(_SNAPSHOT_TIMEOUT), websession.get(url) as response:
                    response.raise_for_status()
                    jpeg = await _read_first_jpeg(response)
                if jpeg is not None:
                    return jpeg
                _LOGGER.debug(
                    "Smart Place camera snapshot yielded no frame (%s, attempt %d)", self.entity_id, attempt + 1
                )
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "Smart Place camera snapshot failed (%s, attempt %d): %s", self.entity_id, attempt + 1, err
                )
            if attempt == 0:
                await self._data.client.refresh_camera_route(f"snapshot:{self.entity_id}")
        return None

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        """Proxy the live MJPEG stream straight through to the HA frontend.

        Pre-flights the upstream GET so a stale routed port can be
        re-discovered (once) before the stream is handed to the frontend,
        rather than streaming a dead connection.
        """
        websession = async_get_clientsession(self.hass)
        for attempt in range(2):
            url = await self._data.client.camera_base_url(self._link)
            if url is None:
                return None
            try:
                response = await websession.get(url)
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "Smart Place camera stream open failed (%s, attempt %d): %s", self.entity_id, attempt + 1, err
                )
            else:
                if response.status == 200:
                    return await async_aiohttp_proxy_stream(
                        self.hass,
                        request,
                        response.content,
                        response.headers.get("Content-Type"),
                    )
                _LOGGER.debug(
                    "Smart Place camera stream HTTP %d (%s, attempt %d)", response.status, self.entity_id, attempt + 1
                )
                response.close()
            if attempt == 0:
                await self._data.client.refresh_camera_route(f"stream:{self.entity_id}")
        return None


async def _read_first_jpeg(response: aiohttp.ClientResponse) -> bytes | None:
    """Read a multipart MJPEG response until one full JPEG frame; return its bytes.

    Slices on the JPEG start/end markers rather than parsing multipart
    boundaries, so it's agnostic to the proxy's boundary framing. Returns
    None if no complete frame arrives within the size cap.
    """
    buffer = b""
    async for chunk in response.content.iter_chunked(_SNAPSHOT_CHUNK):
        buffer += chunk
        start = buffer.find(_JPEG_SOI)
        if start != -1:
            end = buffer.find(_JPEG_EOI, start + len(_JPEG_SOI))
            if end != -1:
                return buffer[start : end + len(_JPEG_EOI)]
        if len(buffer) > _SNAPSHOT_MAX_BYTES:
            return None
    return None
