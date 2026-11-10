"""Build the plan dashboard configuration from the live state and the entity registry.

The layout itself is pure and lives in ``smart_place_client.plan``. This
module supplies what only Home Assistant knows: which entity belongs to
which vendor device (resolved by unique id, disabled entities left out),
the image entities, the temperature unit, and the door buttons and
cameras of the Intercom view.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .entity import async_classify_scenes
from .smart_place_client import (
    DEVICE_LIGHT,
    DOORS,
    LIGHT_KIND_SOCKET,
    DashboardEntities,
    NamedEntity,
    PlanBackground,
    build_dashboard,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import SmartPlaceConfigEntry


@callback
def async_build_plan_config(hass: HomeAssistant, entry: SmartPlaceConfigEntry) -> dict[str, Any]:
    """Return the dashboard configuration (title and views) for a config entry."""
    data = entry.runtime_data
    state = data.state
    registry = er.async_get(hass)

    def entity_id(platform: Platform, key: str) -> str | None:
        """Return the enabled entity with unique id ``<entry id>_<key>``, if any."""
        found = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        if found is None or (reg_entry := registry.async_get(found)) is None or reg_entry.disabled:
            return None
        return found

    def existing(candidates: list[str | None]) -> list[str]:
        return [candidate for candidate in candidates if candidate is not None]

    def camera(camera_id: int) -> NamedEntity | None:
        """Return the enabled camera entity with the name the integration gave it."""
        found = entity_id(Platform.CAMERA, f"camera_{camera_id}")
        if found is None:
            return None
        name = registry.entities[found].original_name
        assert name is not None
        return NamedEntity(found, name)

    scenes = async_classify_scenes(entry, data)
    lights = {
        light.id: found
        for light in state.lights.values()
        if (
            found := entity_id(Platform.SWITCH, f"socket_{light.id}")
            if light.kind == LIGHT_KIND_SOCKET
            else entity_id(Platform.LIGHT, f"light_{light.id}")
        )
    }
    entities = DashboardEntities(
        lights=lights,
        blinds={
            blind_id: found for blind_id in state.blinds if (found := entity_id(Platform.COVER, f"blind_{blind_id}"))
        },
        climates={
            zone_id: found for zone_id in state.climate if (found := entity_id(Platform.CLIMATE, f"climate_{zone_id}"))
        },
        scenes=existing([entity_id(Platform.SCENE, f"scene_{scene_id}") for scene_id in scenes.triggers]),
        switches=existing(
            [
                *(entity_id(Platform.SWITCH, f"scene_switch_{scene_id}") for scene_id in scenes.switches),
                *(entity_id(Platform.SWITCH, f"mute_{mute_id}") for mute_id in sorted(state.mutes)),
            ],
        ),
        fans=existing([entity_id(Platform.FAN, f"level_group_{group.scene_ids[0]}") for group in scenes.level_groups]),
        buttons=existing(
            [
                entity_id(Platform.BUTTON, "all_lights_off"),
                entity_id(Platform.BUTTON, "all_lights_on"),
                *(
                    entity_id(Platform.BUTTON, f"blinds_group_{kind}{menu_id}")
                    for menu_id in sorted(state.blind_sub_menus)
                    for kind in ("up_", "down_", "")
                ),
            ],
        ),
        doors=[
            NamedEntity(found, door.name) for door in DOORS if (found := entity_id(Platform.BUTTON, f"door_{door.key}"))
        ],
        cameras=[found for camera_id in sorted(state.gsa_cameras) if (found := camera(camera_id))],
    )
    images = {
        plan_id: PlanBackground(entity_id=found, width=image.width, height=image.height)
        for plan_id, image in data.client.plan_images.items()
        if (found := entity_id(Platform.IMAGE, f"floorplan_{plan_id}"))
    }
    return build_dashboard(
        plans=state.plans,
        images=images,
        lights=state.lights,
        blinds=state.blinds,
        zones=state.climate,
        entities=entities,
        temperature_unit=hass.config.units.temperature_unit,
        protected_lights=data.protected[DEVICE_LIGHT],
    )
