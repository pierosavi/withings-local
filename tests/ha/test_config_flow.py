from unittest.mock import patch

from custom_components.withings_local.withings_wpp import account

from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.withings_local.const import BODY_MODEL_ID, BODY_SMART_MODEL_ID, CONF_KL, CONF_MODEL_ID, DOMAIN

from .conftest import ADDRESS, TEST_KL, WBS06_ADDRESS, WBS06_IDENTITY, WBS06_UUID, service_info

FETCH = "custom_components.withings_local.config_flow.fetch_kl"


async def _to_key_menu(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info())
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "key"
    return result


async def test_bluetooth_discovery_manual_key(hass: HomeAssistant, mock_bluetooth) -> None:
    result = await _to_key_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: "short"})
    assert result["errors"] == {CONF_KL: "invalid_key"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: TEST_KL})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair"
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_SMART_MODEL_ID}
    assert result["result"].unique_id == ADDRESS.lower()


async def test_bluetooth_discovery_login(hass: HomeAssistant, mock_bluetooth) -> None:
    result = await _to_key_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    creds = {"email": "user@example.com", "password": "pw"}
    with patch(FETCH, side_effect=account.InvalidCredentials("bad", 100)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], creds)
    assert result["errors"] == {"base": "invalid_auth"}
    with patch(FETCH, side_effect=account.AccountError("none")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], creds)
    assert result["errors"] == {"base": "no_scale"}
    with patch(FETCH, return_value=(TEST_KL, {"1001": "Alice"})):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], creds)
    assert result["step_id"] == "pair"
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_KL] == TEST_KL
    assert result["data"]["users"] == {"1001": "Alice"}
    assert "email" not in result["data"] and "password" not in result["data"]


async def test_discovery_of_other_device_aborts(hass: HomeAssistant, mock_bluetooth) -> None:
    info = service_info(name="Something else", uuid="0000180f-0000-1000-8000-00805f9b34fb")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_supported"


async def test_wbs06_bluetooth_discovery_and_login(hass: HomeAssistant, mock_bluetooth) -> None:
    """The captured WPP model identifies Body, even though its BLE MAC differs from the account MAC."""
    info = service_info(name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_UUID)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": "Body 5E"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    assert result["step_id"] == "login"
    creds = {"email": "user@example.com", "password": "test-password"}
    with patch(FETCH, return_value=(TEST_KL, {})) as fetch:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], creds)
    fetch.assert_awaited_once_with(
        hass, creds["email"], creds["password"], WBS06_ADDRESS, BODY_MODEL_ID, WBS06_IDENTITY
    )
    assert result["step_id"] == "pair"
    with (
        patch("custom_components.withings_local.async_setup_entry", return_value=True),
        # The brief advertisement can expire while the operator retrieves kl.
        # The selected BlueZ path must still work without another scan.
        patch("custom_components.withings_local.config_flow.async_discovered_service_info", return_value=[]),
        patch("custom_components.withings_local.config_flow.device_path", return_value="/org/bluez/hci0/dev_02_00_00_00_56_5F"),
        patch("custom_components.withings_local.config_flow.ClassicPairingSession", return_value=FakeClassicSession(already_paired=True)),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["data"] == {"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID}


async def test_wbs06_initial_discovery_creates_entry_after_native_classic_pair(hass: HomeAssistant, mock_bluetooth) -> None:
    """Exercise the full first-install flow, not only an existing entry's reconfiguration."""
    info = service_info(name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_UUID)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: TEST_KL})
    assert result["step_id"] == "pair"
    pairer = FakeClassicSession()
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await _run_pair_now(hass, result, _pairing_patches(pairer, wbs06=True))
        if result["type"] is not FlowResultType.CREATE_ENTRY:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert pairer.calls == 1
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID,
    }
    assert result["result"].unique_id == WBS06_ADDRESS.lower()


async def test_wbs06_discovery_without_name(hass: HomeAssistant, mock_bluetooth) -> None:
    info = service_info(name=WBS06_ADDRESS, address=WBS06_ADDRESS, uuid=WBS06_UUID)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info)
    assert result["description_placeholders"] == {"name": "Body"}


async def test_wbs06_manual_selection_uses_advertised_identity(hass: HomeAssistant, mock_bluetooth) -> None:
    info = service_info(name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_UUID)
    with patch("custom_components.withings_local.config_flow.async_discovered_service_info", return_value=[info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": WBS06_ADDRESS})
    assert result["step_id"] == "key"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    with patch(FETCH, return_value=(TEST_KL, {})) as fetch:
        await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "test@example.com", "password": "test"})
    fetch.assert_awaited_once_with(hass, "test@example.com", "test", WBS06_ADDRESS, BODY_MODEL_ID, WBS06_IDENTITY)


async def test_wbs06_fetch_key_selects_account_mac_and_logs_out(hass: HomeAssistant) -> None:
    """A second WBS06 on the account must not supply the key for this scale."""
    from custom_components.withings_local.config_flow import fetch_kl

    class Response:
        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def json(self, **_kwargs):
            return self.data

    class Http:
        def __init__(self):
            self.actions = []

        def post(self, url, data, timeout):
            self.actions.append((url, data["action"]))
            if data["action"] == "login":
                return Response({"status": 0, "body": {"sessionid": "synthetic-session"}})
            if data["action"] == "get":
                return Response({"status": 0, "body": {"account": [
                    {"id": 1, "email": "other@example.com"},
                    {"id": 42, "email": "user@example.com"},
                ]}})
            if data["action"] == "getbyaccountid":
                assert data["accountid"] == 42
                return Response({
                    "status": 0,
                    "body": {
                        "associations": [
                            {"accountid": 1, "kl": "wrong-scale", "deviceproperties": {"model": 7, "macaddress": "00:11:22:33:44:55"}},
                            {"accountid": 2, "kl": "right-scale", "deviceproperties": {"model": 7, "macaddress": WBS06_IDENTITY}},
                        ]
                    },
                })
            if data["action"] == "getuserslist":
                assert data["accountid"] == 2
                return Response({"status": 0, "body": {"users": [{"id": 99, "firstname": "Alice"}]}})
            assert data["action"] == "logout"
            return Response({"status": 0})

    http = Http()
    with patch("custom_components.withings_local.config_flow.async_get_clientsession", return_value=http):
        kl, users = await fetch_kl(hass, "user@example.com", "test", WBS06_ADDRESS, BODY_MODEL_ID, WBS06_IDENTITY)
    assert kl == "right-scale"
    assert users == {"99": "Alice"}
    assert http.actions[1] == (account.ACCOUNT_URL, "get")
    assert http.actions[2] == (account.ASSOCIATION_URL, "getbyaccountid")
    assert http.actions[-1] == (account.LOGIN_URL, "logout")


async def test_empty_submit_goes_back_to_key_options(hass: HomeAssistant, mock_bluetooth) -> None:
    result = await _to_key_menu(hass)
    # Manual key -> empty submit -> back to the menu.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "key"
    # Login -> half-filled is an error, empty goes back.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "login"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "user@example.com"})
    assert result["errors"] == {"base": "missing_credentials"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "key"


async def test_unnamed_advertisement_gets_readable_title(hass: HomeAssistant, mock_bluetooth) -> None:
    # Passive scanners may not receive the local name; HA then reports the address as the name.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(name=ADDRESS)
    )
    assert result["description_placeholders"] == {"name": "Body Smart"}



class FakeSession:
    def __init__(self, accept=True, already_paired=False, start_error=None):
        self.accept, self.already, self.code, self.closed = accept, already_paired, None, False
        self.start_error = start_error

    async def start(self):
        if self.start_error is not None:
            raise self.start_error
        return self.already

    async def wait_for_passkey_request(self):
        return None

    async def submit_passkey(self, code):
        from custom_components.withings_local.pairing import PairingFailed

        self.code = code
        if not self.accept:
            raise PairingFailed("wrong code")

    async def close(self):
        self.closed = True


class FakeClassicSession:
    """Native Classic pairing owns its own scoped agent and cleanup."""

    def __init__(self, *, already_paired=False, error=None):
        self.already_paired = already_paired
        self.error = error
        self.calls = 0

    async def pair(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.already_paired

    async def bonded(self):
        return self.already_paired


async def _to_pair_menu(hass):
    result = await _to_key_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: TEST_KL})


def _pairing_patches(session=None, never_advertise=False, path_error=None, wbs06=False):
    from .conftest import SETUP_UUID, WBS06_SETUP_UUID

    def fake_register(hass, callback, address):
        """Like HA's every-advertisement callback: fires for unchanged data too."""
        if not never_advertise:
            weigh_in = service_info(
                name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_UUID,
            ) if wbs06 else service_info()
            weigh_in.time = 1e12
            setup = service_info(
                name="Body 5E", address=WBS06_ADDRESS, uuid=WBS06_SETUP_UUID, mfr=b"\x00\x00\x00\x00",
            ) if wbs06 else service_info(uuid=SETUP_UUID, mfr=b"\x00\x00\x00\x00")
            setup.time = 1e12
            hass.loop.call_soon(callback, weigh_in)  # ignored: not setup mode
            hass.loop.call_soon(callback, setup)  # accepted
        return lambda: None

    path = patch(
        "custom_components.withings_local.config_flow.device_path",
        side_effect=path_error,
        return_value="/org/bluez/hci0/dev_02_00_00_00_56_5F" if wbs06 else "/org/bluez/hci0/dev_00_11_22_33_44_55",
    )
    return (
        patch("custom_components.withings_local.config_flow.bluetooth.async_register_advertisement_callback", fake_register),
        path,
        patch(
            "custom_components.withings_local.config_flow.ClassicPairingSession"
            if wbs06 else "custom_components.withings_local.config_flow.PairingSession",
            return_value=session or (FakeClassicSession() if wbs06 else FakeSession()),
        ),
    )


async def _run_pair_now(hass, result, patches):
    with patches[0], patches[1], patches[2]:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_now"})
        if result["type"] is FlowResultType.SHOW_PROGRESS:
            await hass.async_block_till_done()
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


async def test_pairing_with_code_creates_entry(hass: HomeAssistant, mock_bluetooth) -> None:
    session = FakeSession()
    result = await _run_pair_now(hass, await _to_pair_menu(hass), _pairing_patches(session))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "passkey"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "12345"})
    assert result["errors"] == {"passkey": "invalid_passkey"}
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert session.code == 123456 and session.closed


async def test_already_paired_skips_code(hass: HomeAssistant, mock_bluetooth) -> None:
    session = FakeSession(already_paired=True)
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await _run_pair_now(hass, await _to_pair_menu(hass), _pairing_patches(session))
        if result["type"] is not FlowResultType.CREATE_ENTRY:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY and session.closed


async def test_wrong_code_offers_retry(hass: HomeAssistant, mock_bluetooth) -> None:
    result = await _run_pair_now(hass, await _to_pair_menu(hass), _pairing_patches(FakeSession(accept=False)))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "000000"})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair_failed"


async def test_unexpected_pairing_error_offers_retry(hass: HomeAssistant, mock_bluetooth) -> None:
    # e.g. no system D-Bus: not a PairingFailed, but the user still gets the retry menu, not "unknown error".
    session = FakeSession(start_error=OSError("no system bus"))
    result = await _run_pair_now(hass, await _to_pair_menu(hass), _pairing_patches(session))
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair_failed"
    assert "no system bus" in result["description_placeholders"]["error"] and session.closed


async def test_scale_not_in_setup_mode_times_out(hass: HomeAssistant, mock_bluetooth) -> None:
    with patch("custom_components.withings_local.config_flow.PAIRING_ADVERTISEMENT_TIMEOUT", 0.05):
        result = await _run_pair_now(hass, await _to_pair_menu(hass), _pairing_patches(never_advertise=True))
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair_timeout"


async def test_unsupported_adapter_continues_without_pairing(hass: HomeAssistant, mock_bluetooth) -> None:
    from custom_components.withings_local.pairing import PairingUnsupported

    patches = _pairing_patches(path_error=PairingUnsupported("pairing_remote_adapter"))
    result = await _run_pair_now(hass, await _to_pair_menu(hass), patches)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair_unsupported"
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def _entry(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS.lower(), title="Body Smart",
                            data={"address": ADDRESS, CONF_KL: TEST_KL})
    entry.add_to_hass(hass)
    return entry


async def test_reconfigure_pairs_again(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = await _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair"
    instructions = result["description_placeholders"]["pairing_instructions"]
    assert instructions.startswith("1. **Hold the button under the scale for 3 seconds**")
    assert "2. Choose *Pair now*" in instructions
    assert "6-digit code" in instructions
    assert "Setup failed" not in instructions
    result = await _run_pair_now(hass, result, _pairing_patches())
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "654321"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"


async def test_wbs06_reconfigure_pairs_without_a_passkey(hass: HomeAssistant, mock_bluetooth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=WBS06_ADDRESS.lower(), title="Body 5E",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "pair"
    assert "without a passkey" in result["description_placeholders"]["pairing_instructions"]
    assert "Setup failed" in result["description_placeholders"]["pairing_instructions"]
    assert result["description_placeholders"]["pairing_instructions"].startswith("1. Choose *Pair now*")
    session = FakeClassicSession()
    patches = _pairing_patches(session=session, wbs06=True)
    with patch("custom_components.withings_local.async_setup_entry", return_value=True), patches[2] as pairing_factory:
        # The flow helper owns its own pairing mock; inspect the constructed agent instead.
        with patches[0], patches[1]:
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_now"})
            if result["type"] is FlowResultType.SHOW_PROGRESS:
                assert result["progress_action"] == "wait_for_scale_wbs06"
                await hass.async_block_till_done()
                result = await hass.config_entries.flow.async_configure(result["flow_id"])
            if result["type"] is not FlowResultType.ABORT:
                result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert session.calls == 1
    pairing_factory.assert_called_once_with(
        "/org/bluez/hci0/dev_02_00_00_00_56_5F",
    )


async def test_wbs06_already_bonded_pair_now_still_trusts(hass: HomeAssistant, mock_bluetooth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=WBS06_ADDRESS.lower(), title="Body 5E",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    session = FakeClassicSession(already_paired=True)
    patches = _pairing_patches(session, wbs06=True)
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await _run_pair_now(hass, result, patches)
        if result["type"] is not FlowResultType.ABORT:
            result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert session.calls == 1


async def test_wbs06_trust_failure_does_not_complete_pair_flow(hass: HomeAssistant, mock_bluetooth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=WBS06_ADDRESS.lower(), title="Body 5E",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    from custom_components.withings_local.pairing import PairingFailed
    session = FakeClassicSession(error=PairingFailed("BlueZ did not accept WBS06 trust"))
    result = await _run_pair_now(hass, result, _pairing_patches(session, wbs06=True))
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair_failed"
    assert session.calls == 1
    assert entry.data[CONF_KL] == TEST_KL


async def test_wbs06_skipped_pairing_does_not_change_trust(hass: HomeAssistant, mock_bluetooth) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=WBS06_ADDRESS.lower(), title="Body 5E",
        data={"address": WBS06_ADDRESS, CONF_KL: TEST_KL, CONF_MODEL_ID: BODY_MODEL_ID},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    session = FakeClassicSession(already_paired=True)
    with (
        patch("custom_components.withings_local.config_flow.async_discovered_service_info", return_value=[
            service_info(name="Body", address=WBS06_ADDRESS, uuid=WBS06_UUID)
        ]),
        patch("custom_components.withings_local.config_flow.device_path", return_value="/org/bluez/hci0/dev_02_00_00_00_56_5F"),
        patch("custom_components.withings_local.config_flow.ClassicPairingSession", return_value=session),
        patch("custom_components.withings_local.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["type"] is FlowResultType.ABORT
    assert session.calls == 0  # bond inspected without issuing a new pairing request


async def test_wbs06_skip_does_not_create_entry_without_classic_bond(hass: HomeAssistant, mock_bluetooth) -> None:
    info = service_info(name="Body", address=WBS06_ADDRESS, uuid=WBS06_UUID)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KL: TEST_KL})
    session = FakeClassicSession(already_paired=False)
    with (
        patch("custom_components.withings_local.config_flow.async_discovered_service_info", return_value=[info]),
        patch("custom_components.withings_local.config_flow.device_path", return_value="/org/bluez/hci0/dev_02_00_00_00_56_5F"),
        patch("custom_components.withings_local.config_flow.ClassicPairingSession", return_value=session),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pair_skip"})
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pair_failed"
    assert "pair_skip" not in result["menu_options"]
    assert session.calls == 0
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_models_do_not_offer_legacy_setup_completion(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = await _entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert "finish_setup" not in result["menu_options"]


async def test_reauth_pairs_again(hass: HomeAssistant, mock_bluetooth) -> None:
    entry = await _entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "pair"
    result = await _run_pair_now(hass, result, _pairing_patches())
    with patch("custom_components.withings_local.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"passkey": "654321"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
