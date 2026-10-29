"""Sensors: each user's latest weigh-in, plus scale-level diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import BODY_MODEL_ID, CONF_USERS, DOMAIN, GUEST_USER_ID, SUPPORTED_MODELS
from .coordinator import ScaleData, WeighIn, WithingsLocalConfigEntry, WithingsScaleCoordinator


@dataclass(frozen=True, kw_only=True)
class UserSensorDescription(SensorEntityDescription):
    value_fn: Callable[[WeighIn], float | datetime | None]


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ScaleData], float | datetime | None]


def _measure(name: str) -> Callable[[WeighIn], float | None]:
    return lambda weigh_in: weigh_in.values.get(name)


def _mass(key: str) -> UserSensorDescription:
    return UserSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_measure(key),
    )


USER_SENSORS: tuple[UserSensorDescription, ...] = (
    _mass("weight"),
    _mass("fat_mass"),
    _mass("fat_free_mass"),
    _mass("muscle_mass"),
    _mass("bone_mass"),
    _mass("hydration"),
    UserSensorDescription(
        key="fat_ratio",
        translation_key="fat_ratio",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_measure("fat_ratio"),
    ),
    UserSensorDescription(
        key="visceral_fat",
        translation_key="visceral_fat",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_measure("visceral_fat"),
    ),
    UserSensorDescription(
        key="basal_metabolic_rate",
        translation_key="basal_metabolic_rate",
        native_unit_of_measurement=UnitOfEnergy.KILO_CALORIE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_measure("basal_metabolic_rate"),
    ),
    UserSensorDescription(
        key="metabolic_age",
        translation_key="metabolic_age",
        native_unit_of_measurement=UnitOfTime.YEARS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_measure("metabolic_age"),
    ),
    UserSensorDescription(
        key="heart_rate",
        translation_key="heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_measure("heart_rate"),
    ),
    UserSensorDescription(
        key="measured_at",
        translation_key="measured_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda weigh_in: weigh_in.measured_at,
    ),
)

SCALE_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.battery,
    ),
    ScaleSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_sync,
    ),
)


# A guest weigh-in carries no user ID and only a weight.
GUEST_SENSOR_KEYS = {"weight", "measured_at"}
# WBS06 exposes only weight and weigh-in time per user.
BODY_SENSOR_KEYS = GUEST_SENSOR_KEYS


def user_display_name(entry: WithingsLocalConfigEntry, user_id: int, known_users: list[int]) -> str:
    """Account first name when known, else "User N" numbered among real users (guest excluded)."""
    if user_id == GUEST_USER_ID:
        return "Guest"
    if name := (entry.data.get(CONF_USERS) or {}).get(str(user_id)):
        return name
    return f"User {[u for u in known_users if u != GUEST_USER_ID].index(user_id) + 1}"


async def async_setup_entry(
    hass: HomeAssistant, entry: WithingsLocalConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ScaleSensor(coordinator, d) for d in SCALE_SENSORS)

    @callback
    def add_users(user_ids: list[int]) -> None:
        async_add_entities(
            UserSensor(coordinator, d, user_id, user_display_name(entry, user_id, coordinator.known_users))
            for user_id in user_ids
            for d in USER_SENSORS
            if user_id != GUEST_USER_ID or d.key in GUEST_SENSOR_KEYS
            if coordinator.model_id != BODY_MODEL_ID or d.key in BODY_SENSOR_KEYS
        )

    add_users(coordinator.known_users)
    entry.async_on_unload(coordinator.async_add_new_user_listener(add_users))


class _WithingsSensor(RestoreSensor):
    """Keeps its last value: the scale only advertises right after a weigh-in, so availability
    means "we have a reading", not "the scale is in Bluetooth range"."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: WithingsScaleCoordinator) -> None:
        self.coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
        self._update_from_coordinator()
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator_update))

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_coordinator()
        self.async_write_ha_state()

    def _current_value(self) -> float | datetime | None:
        raise NotImplementedError

    def _update_from_coordinator(self) -> None:
        if (value := self._current_value()) is not None:
            self._attr_native_value = value

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None


def _scale_device(coordinator: WithingsScaleCoordinator) -> DeviceInfo:
    model, model_id = SUPPORTED_MODELS[coordinator.model_id]
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        name=coordinator.title,
        manufacturer="Withings",
        model=model,
        model_id=model_id,
    )


class ScaleSensor(_WithingsSensor):
    entity_description: ScaleSensorDescription

    def __init__(self, coordinator: WithingsScaleCoordinator, description: ScaleSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}-{description.key}"
        self._attr_device_info = _scale_device(coordinator)

    def _current_value(self) -> float | datetime | None:
        return self.entity_description.value_fn(self.coordinator.data)


class UserSensor(_WithingsSensor):
    entity_description: UserSensorDescription

    def __init__(
        self, coordinator: WithingsScaleCoordinator, description: UserSensorDescription, user_id: int, user_name: str
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.user_id = user_id
        self._attr_unique_id = f"{coordinator.address}-user{user_id}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.address}-user{user_id}")},
            name=f"{coordinator.title} ({user_name})",
            manufacturer="Withings",
            model=SUPPORTED_MODELS[coordinator.model_id][0],
            via_device_id=dr.async_get_device_id_by_identifier(
                coordinator.hass, (DOMAIN, coordinator.address), config_entry_id=coordinator.config_entry_id
            ),
        )

    def _current_value(self) -> float | datetime | None:
        weigh_in = self.coordinator.data.users.get(self.user_id)
        return None if weigh_in is None else self.entity_description.value_fn(weigh_in)
