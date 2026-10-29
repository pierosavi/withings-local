from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.withings_local.const import BODY_MODEL_ID, CONF_KL, CONF_MODEL_ID, CONF_USERS, DOMAIN, OPT_KNOWN_USERS
from custom_components.withings_local.coordinator import ScaleData, WeighIn

from .conftest import ADDRESS, TEST_KL, service_info

ALICE, BOB = 1001, 1002  # synthetic user IDs


async def _setup(hass: HomeAssistant, **entry_kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS.lower(),
        title="Body Smart",
        data={"address": ADDRESS, CONF_KL: TEST_KL, **entry_kwargs.pop("data", {})},
        **entry_kwargs,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _sync(entry, users: dict[int, WeighIn], battery=80) -> None:
    coordinator = entry.runtime_data
    coordinator.data = ScaleData(users=users, battery=battery, last_sync=datetime(2026, 1, 2, 8, tzinfo=UTC))
    coordinator._register_users(list(users))  # noqa: SLF001 - what a real sync does
    coordinator.async_update_listeners()


async def test_per_user_sensors_appear_and_keep_values(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = await _setup(hass, data={CONF_USERS: {str(ALICE): "Alice"}})
    assert hass.states.get("sensor.body_smart_battery").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.body_smart_alice_weight") is None  # no users seen yet

    when = datetime(2026, 1, 2, 7, 30, tzinfo=UTC)
    _sync(entry, {ALICE: WeighIn(when, {"weight": 70.12, "fat_ratio": 20.0}), BOB: WeighIn(when, {"weight": 90.0})})
    await hass.async_block_till_done()

    alice = hass.states.get("sensor.body_smart_alice_weight")
    assert float(alice.state) == 70.12 and alice.attributes["unit_of_measurement"] == "kg"
    assert hass.states.get("sensor.body_smart_alice_fat_ratio").state == "20.0"
    assert hass.states.get("sensor.body_smart_alice_last_weigh_in").state == "2026-01-02T07:30:00+00:00"
    assert float(hass.states.get("sensor.body_smart_user_2_weight").state) == 90.0  # no name known
    assert hass.states.get("sensor.body_smart_user_2_muscle_mass").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.body_smart_battery").state == "80"
    assert entry.options[OPT_KNOWN_USERS] == [ALICE, BOB]

    from custom_components.withings_local.diagnostics import async_get_config_entry_diagnostics

    entry.runtime_data._last_service_info = service_info()  # noqa: SLF001 - exercise the advertised-device diagnostics path
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["last_advertisement"]["new_data"] is True
    assert TEST_KL not in str(diag) and ADDRESS not in str(diag) and "Alice" not in str(diag)
    assert str(ALICE) not in str(diag)
    assert [u["measurement_types"] for u in diag["users"]] == [["fat_ratio", "weight"], ["weight"]]

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_guest_gets_weight_only_and_isnt_numbered(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = await _setup(hass)
    when = datetime(2026, 1, 2, tzinfo=UTC)
    _sync(entry, {ALICE: WeighIn(when, {"weight": 70.0}), 0: WeighIn(when, {"weight": 55.0}), BOB: WeighIn(when, {"weight": 90.0})})
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.body_smart_guest_weight").state) == 55.0
    assert hass.states.get("sensor.body_smart_guest_muscle_mass") is None
    # Guest doesn't take a number: the second real user is "User 2", not "User 3".
    assert float(hass.states.get("sensor.body_smart_user_2_weight").state) == 90.0
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_body_only_creates_supported_weight_sensors(hass: HomeAssistant, mock_bluetooth) -> None:
    with patch("custom_components.withings_local.coordinator.Wbs06RfcommServer") as factory:
        factory.return_value.start = AsyncMock()
        factory.return_value.stop = AsyncMock()
        factory.return_value.remove = AsyncMock()
        factory.return_value.addresses = set()
        entry = await _setup(hass, data={CONF_MODEL_ID: BODY_MODEL_ID})
        when = datetime(2026, 1, 2, tzinfo=UTC)
        _sync(entry, {ALICE: WeighIn(when, {"weight": 71.5})}, battery=96)
        await hass.async_block_till_done()

        assert float(hass.states.get("sensor.body_smart_user_1_weight").state) == 71.5
        assert hass.states.get("sensor.body_smart_user_1_last_weigh_in") is not None
        for unsupported in (
            "fat_mass", "fat_free_mass", "muscle_mass", "bone_mass", "water_mass",
            "fat_ratio", "visceral_fat", "basal_metabolic_rate", "metabolic_age", "heart_rate",
        ):
            assert hass.states.get(f"sensor.body_smart_user_1_{unsupported}") is None
        assert hass.states.get("sensor.body_smart_battery").state == "96"
        assert await hass.config_entries.async_unload(entry.entry_id)
        factory.return_value.remove.assert_awaited_once()
        factory.return_value.stop.assert_awaited_once()


async def test_known_users_restored_and_old_entities_removed(hass: HomeAssistant, mock_bluetooth) -> None:
    registry = er.async_get(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS.lower(), title="Body Smart",
        data={"address": ADDRESS, CONF_KL: TEST_KL}, options={OPT_KNOWN_USERS: [ALICE]},
    )
    entry.add_to_hass(hass)
    old = registry.async_get_or_create("sensor", DOMAIN, f"{ADDRESS}-weight", config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    # Users seen before a restart get their entities straight away.
    assert hass.states.get("sensor.body_smart_user_1_weight") is not None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_address_title_is_renamed(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS.lower(), title=ADDRESS, data={"address": ADDRESS, CONF_KL: TEST_KL})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.title == "Body Smart"
    assert await hass.config_entries.async_unload(entry.entry_id)
