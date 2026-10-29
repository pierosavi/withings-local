"""Withings Local: read compatible scales over Bluetooth without the cloud."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import BODY_MODEL_ID, BODY_SMART_MODEL_ID, CONF_MODEL_ID, DOMAIN, LOCAL_NAME_PREFIX, SUPPORTED_MODELS
from .coordinator import WithingsLocalConfigEntry, WithingsScaleCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: WithingsLocalConfigEntry) -> bool:
    """Set up a scale. It only advertises after a weigh-in, so don't require it to be in range now."""
    _remove_single_user_entities(hass, entry)
    if entry.title.replace("-", ":").upper() == entry.data["address"].upper():
        # Set up before 0.2.1 from an advertisement without a name: use a readable title.
        hass.config_entries.async_update_entry(entry, title=LOCAL_NAME_PREFIX)
    coordinator = WithingsScaleCoordinator(hass, entry)
    entry.runtime_data = coordinator
    # Register the scale first: the per-user devices link to it with via_device_id.
    address = entry.data["address"]
    model, model_id = SUPPORTED_MODELS[entry.data.get(CONF_MODEL_ID, BODY_SMART_MODEL_ID)]
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, address)},
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        name=entry.title,
        manufacturer="Withings",
        model=model,
        model_id=model_id,
    )
    if coordinator.model_id == BODY_MODEL_ID:
        try:
            await coordinator.async_start_classic()
        except Exception as err:
            # WBS06 sync uses Classic exclusively. A loaded entry without
            # the native listener would never deliver measurements.
            raise ConfigEntryNotReady("WBS06 Classic RFCOMM listener unavailable") from err
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_stop_classic()
        raise
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WithingsLocalConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop_classic()
    return unloaded


# Keys whose sensors moved from the scale device to per-user devices in 0.2.0.
_PER_USER_KEYS = {
    "weight", "fat_mass", "fat_free_mass", "muscle_mass", "bone_mass", "hydration", "fat_ratio",
    "visceral_fat", "basal_metabolic_rate", "metabolic_age", "heart_rate", "measured_at",
}


def _remove_single_user_entities(hass: HomeAssistant, entry: WithingsLocalConfigEntry) -> None:
    """Drop entities from the single-user version (unique id `<address>-<key>`)."""
    registry = er.async_get(hass)
    address = entry.data["address"]
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.unique_id in {f"{address}-{key}" for key in _PER_USER_KEYS}:
            registry.async_remove(ent.entity_id)
