"""Config flow: discover the scale over Bluetooth, then obtain its kl secret."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from bluetooth_data_tools import monotonic_time_coarse
import voluptuous as vol
from .withings_wpp import account
from .withings_wpp.advertisement import WithingsUuid

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import BODY_MODEL_ID, BODY_SMART_MODEL_ID, CONF_KL, CONF_MODEL_ID, CONF_USERS, DOMAIN, LOCAL_NAME_PREFIX, SUPPORTED_MODELS
from .classic_pairing import ClassicPairingSession
from .coordinator import is_setup_mode
from .pairing import PairingFailed, PairingSession, PairingUnsupported, device_path

# How long to wait for the scale to advertise after the user holds its button.
PAIRING_ADVERTISEMENT_TIMEOUT = 90

_LOGGER = logging.getLogger(__name__)


def device_name(info: BluetoothServiceInfoBleak) -> str:
    """The advertised name, or the model's name when the scanner missed it."""
    name = info.name or ""
    if not name or format_mac(name) == format_mac(info.address) or name.replace("-", ":").upper() == info.address.upper():
        model = scale_model_id(info)
        return SUPPORTED_MODELS.get(model, (LOCAL_NAME_PREFIX, ""))[0]
    return name


def scale_model_id(info: BluetoothServiceInfoBleak) -> int | None:
    """Recognize supported models by their WPP UUID, even without a local name."""
    for uuid in info.service_uuids:
        w = WithingsUuid.parse(uuid)
        if w and w.model_id in SUPPORTED_MODELS:
            return w.model_id
    if info.name and info.name.startswith(LOCAL_NAME_PREFIX):
        return BODY_SMART_MODEL_ID
    return None


def scale_identity_mac(info: BluetoothServiceInfoBleak, model_id: int) -> str | None:
    """The WPP advertisement may contain the account MAC instead of the BLE address."""
    for uuid in info.service_uuids:
        w = WithingsUuid.parse(uuid)
        if w and w.model_id == model_id and len(w.tail) == 12 and w.tail.strip("0"):
            return ":".join(w.tail[i : i + 2] for i in range(0, 12, 2)).upper()
    return None


async def fetch_kl(
    hass, email: str, password: str, address: str, model_id: int, identity_mac: str | None
) -> tuple[str, dict[str, str]]:
    """Log in to the Withings account; return the scale's kl and {user id: first name}.

    Credentials are used once and not stored. User names are optional: failure to list them
    is not an error (users then show as "User 1", "User 2", ...).
    """
    http = async_get_clientsession(hass)

    async def post(url: str, params: dict) -> dict:
        async with http.post(url, data=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    session = account.parse_login(await post(account.LOGIN_URL, account.login_params(email, password)))
    users: dict[str, str] = {}
    try:
        try:
            account_id = account.parse_account_id(
                await post(account.ACCOUNT_URL, account.account_params(session)), email
            )
        except account.AccountError as err:
            # An error code or a missing/ambiguous account ID is safe to log.
            # Never log the response, session, email, or account ID.
            _LOGGER.warning("Withings account ID lookup failed: %s", err)
            raise
        association_response = await post(account.ASSOCIATION_URL, account.association_params(session, account_id))
        try:
            scales = account.parse_scales(association_response, model=None)
            selected = account.select_scale(scales, model=model_id, address=address, identity_mac=identity_mac)
        except account.AccountError as err:
            _LOGGER.warning(
                "Withings account key lookup failed (%s); sanitized association structure: %s",
                err,
                account.association_diagnostics(
                    association_response, address=address, identity_mac=identity_mac
                ),
            )
            raise
        if selected.accountid:
            try:
                names = account.parse_users(await post(account.ACCOUNT_URL, account.users_params(session, selected.accountid)))
                users = {str(uid): name for uid, name in names.items()}
            except (account.AccountError, aiohttp.ClientError, TimeoutError):
                _LOGGER.debug("Could not list Withings users; names will be generic", exc_info=True)
    finally:
        try:
            await post(account.LOGIN_URL, account.logout_params(session))
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Withings logout failed", exc_info=True)
    return selected.kl, users


class WithingsLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Withings Local."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._selected_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}
        self._discovered_info: dict[str, BluetoothServiceInfoBleak] = {}
        self._model_id = BODY_SMART_MODEL_ID
        self._identity_mac: str | None = None
        self._kl: str | None = None
        self._users: dict[str, str] | None = None
        self._pair_task: asyncio.Task | None = None
        self._pairing: PairingSession | None = None
        self._pair_error: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        if (model_id := scale_model_id(discovery_info)) is None:
            # Enough to tell which device this is, without its address; users paste it into issues.
            _LOGGER.debug(
                "Unsupported Withings device: name=%r model_ids=%s",
                discovery_info.name,
                sorted({w.model_id for u in discovery_info.service_uuids if (w := WithingsUuid.parse(u))}),
            )
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._selected_info = discovery_info
        self._name = device_name(discovery_info)
        self._model_id = model_id
        self._identity_mac = scale_identity_mac(discovery_info, model_id)
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_key()
        self._set_confirm_only()
        return self.async_show_form(step_id="bluetooth_confirm", description_placeholders={"name": self._name})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._address = user_input[CONF_ADDRESS]
            self._name = self._discovered[self._address].rsplit(" (", 1)[0]
            info = self._discovered_info[self._address]
            self._selected_info = info
            self._model_id = scale_model_id(info) or BODY_SMART_MODEL_ID
            self._identity_mac = scale_identity_mac(info, self._model_id)
            await self.async_set_unique_id(format_mac(self._address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_key()

        current = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if format_mac(info.address) not in current and scale_model_id(info) is not None:
                self._discovered[info.address] = f"{device_name(info)} ({info.address})"
                self._discovered_info[info.address] = info
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._discovered)})
        )

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="key", menu_options=["login", "manual"])

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = (user_input.get(CONF_EMAIL) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""
            if not email and not password:
                # Config flows have no back button: an empty submit returns to the key options.
                return await self.async_step_key()
            if not email or not password:
                errors["base"] = "missing_credentials"
        if user_input is not None and not errors:
            try:
                kl, users = await fetch_kl(
                    self.hass, email, password, self._address, self._model_id, self._identity_mac
                )
            except account.InvalidCredentials:
                errors["base"] = "invalid_auth"
            except account.AccountError:
                errors["base"] = "no_scale"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error fetching the scale key")
                errors["base"] = "unknown"
            else:
                return await self._create(kl, users)
        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                    vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            kl = (user_input.get(CONF_KL) or "").strip()
            if not kl:
                # Config flows have no back button: an empty submit returns to the key options.
                return await self.async_step_key()
            if len(kl) < 16:
                errors[CONF_KL] = "invalid_key"
            else:
                return await self._create(kl)
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Optional(CONF_KL): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}),
            errors=errors,
        )

    async def _create(self, kl: str, users: dict[str, str] | None = None) -> ConfigFlowResult:
        """Key obtained: continue with pairing."""
        self._kl, self._users = kl, users
        return await self.async_step_pair()

    # --- Pairing -------------------------------------------------------------------------------

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explain pairing and let the user start it (or skip if already paired)."""
        instructions = (
            "1. Choose *Pair now*.\n"
            "2. **Hold the button under the scale for 3 seconds** (setup mode). "
            "Home Assistant pairs without a passkey.\n\n"
            "The scale may show 'Setup failed' afterward even when pairing succeeds. "
            "Check for a new weigh-in in Home Assistant to confirm synchronization."
            if self._model_id == BODY_MODEL_ID
            else (
                "1. **Hold the button under the scale for 3 seconds** (setup mode).\n"
                "2. Choose *Pair now*. The scale shows a 6-digit code, which you'll enter next."
            )
        )
        return self.async_show_menu(
            step_id="pair", menu_options=["pair_now", "pair_skip"],
            description_placeholders={"pairing_instructions": instructions},
        )

    # Both models advertise in setup mode after a button hold. WBS06 uses Just Works,
    # while WBS13 requires the six-digit passkey displayed by the scale.

    async def async_step_pair_now(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Wait for the scale in setup mode, request pairing, wait until it shows its code."""
        if self._pair_task is None:
            self._pair_error = None
            self._pair_task = self.hass.async_create_task(self._async_start_pairing())
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair_now",
                progress_action="wait_for_scale_wbs06" if self._model_id == BODY_MODEL_ID else "wait_for_scale",
                progress_task=self._pair_task,
            )
        task, self._pair_task = self._pair_task, None
        try:
            already_paired = task.result()
        except TimeoutError:
            await self._async_close_pairing()
            return self.async_show_progress_done(next_step_id="pair_timeout")
        except PairingUnsupported as err:
            _LOGGER.info("Pairing not possible here (%s); continuing without it", err.reason)
            self._pair_error = err.reason
            await self._async_close_pairing()
            return self.async_show_progress_done(next_step_id="pair_unsupported")
        except PairingFailed as err:
            _LOGGER.warning("Pairing with the scale failed: %s", err)
            self._pair_error = str(err)
            await self._async_close_pairing()
            return self.async_show_progress_done(next_step_id="pair_failed")
        except Exception as err:  # noqa: BLE001 - e.g. the system D-Bus is unreachable; offer a retry, not "unknown error"
            _LOGGER.exception("Pairing with the scale failed unexpectedly")
            self._pair_error = f"{type(err).__name__}: {err}"
            await self._async_close_pairing()
            return self.async_show_progress_done(next_step_id="pair_failed")
        if already_paired:
            await self._async_close_pairing()
            return self.async_show_progress_done(next_step_id="pair_finish")
        return self.async_show_progress_done(next_step_id="passkey")

    async def _async_start_pairing(self) -> bool:
        """Return True when the scale is already paired (no code needed)."""
        service_info = await self._async_wait_for_setup_mode()
        _LOGGER.info("%s: setup-mode signal received via %s, requesting pairing", self._name, service_info.source)
        if self._model_id == BODY_MODEL_ID:
            # WBS06's actual weigh-in transport is incoming Classic RFCOMM,
            # not GATT. Pair the bearer we need, without touching the LE bond.
            # This method handles adapter state and agent cleanup itself.
            await ClassicPairingSession(device_path(self.hass, service_info)).pair()
            _LOGGER.info("%s: Classic Bluetooth bond confirmed", self._name)
            return True
        self._pairing = PairingSession(
            device_path(self.hass, service_info)
        )
        if await self._pairing.start():
            _LOGGER.info("%s: Bluetooth already paired with Home Assistant", self._name)
            return True
        await self._pairing.wait_for_passkey_request()
        _LOGGER.info("%s: the scale shows its code; waiting for the user to enter it", self._name)
        return False

    async def _async_wait_for_setup_mode(self) -> BluetoothServiceInfoBleak:
        """Wait for a fresh setup-mode advertisement (button held 3 s).

        Uses the every-advertisement callback: the setup-mode advertisement is identical each
        time, and the change-only callbacks would never report it again.
        """
        assert self._address is not None
        _LOGGER.info("%s: waiting up to %ss for the setup-mode signal", self._name, PAIRING_ADVERTISEMENT_TIMEOUT)
        found: asyncio.Future[BluetoothServiceInfoBleak] = self.hass.loop.create_future()
        started = monotonic_time_coarse()

        @callback
        def _on_advertisement(info: BluetoothServiceInfoBleak) -> None:
            if not found.done() and info.connectable and info.time >= started and is_setup_mode(info, self._model_id):
                found.set_result(info)

        cancel = bluetooth.async_register_advertisement_callback(self.hass, _on_advertisement, self._address)
        try:
            async with asyncio.timeout(PAIRING_ADVERTISEMENT_TIMEOUT):
                return await found
        except TimeoutError:
            _LOGGER.info("%s: no setup-mode signal within %ss", self._name, PAIRING_ADVERTISEMENT_TIMEOUT)
            raise
        finally:
            cancel()

    async def async_step_passkey(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the 6-digit code shown on the scale."""
        errors: dict[str, str] = {}
        if user_input is not None:
            code = str(user_input.get("passkey", "")).strip()
            if not (code.isdigit() and len(code) == 6):
                errors["passkey"] = "invalid_passkey"
            else:
                assert self._pairing is not None
                try:
                    await self._pairing.submit_passkey(int(code))
                except PairingFailed as err:
                    _LOGGER.warning("Pairing with the scale failed: %s", err)
                    self._pair_error = str(err)
                    await self._async_close_pairing()
                    return await self.async_step_pair_failed()
                _LOGGER.info("%s: paired", self._name)
                await self._async_close_pairing()
                return await self.async_step_pair_finish()
        return self.async_show_form(
            step_id="passkey",
            data_schema=vol.Schema({vol.Required("passkey"): TextSelector(TextSelectorConfig(type=TextSelectorType.NUMBER))}),
            errors=errors,
        )

    async def async_step_pair_timeout(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The scale never showed up in setup mode."""
        options = ["pair_now"] if self._model_id == BODY_MODEL_ID else ["pair_now", "pair_skip"]
        return self.async_show_menu(step_id="pair_timeout", menu_options=options)

    async def async_step_pair_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["pair_now"] if self._model_id == BODY_MODEL_ID else ["pair_now", "pair_skip"]
        return self.async_show_menu(
            step_id="pair_failed",
            menu_options=options,
            description_placeholders={"error": self._pair_error or "unknown"},
        )

    async def async_step_pair_unsupported(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pairing can't be driven here (ESPHome proxy, or not Linux)."""
        if self._model_id == BODY_MODEL_ID:
            return self.async_abort(reason="pairing_not_available")
        return self.async_show_menu(step_id="pair_unsupported", menu_options=["pair_skip"])

    async def async_step_pair_skip(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._model_id == BODY_MODEL_ID:
            # A BLE bond is not enough for scale-initiated Classic RFCOMM.
            info = self._selected_info
            if info is None or info.address != self._address:
                info = next(
                    (item for item in async_discovered_service_info(self.hass, connectable=True)
                     if item.address == self._address),
                    None,
                )
            if info is None:
                self._pair_error = "No local Bluetooth advertisement is available to verify the Classic bond"
                return await self.async_step_pair_failed()
            try:
                if not await ClassicPairingSession(device_path(self.hass, info)).bonded():
                    self._pair_error = "No stored Classic Bluetooth bond; choose Pair now"
                    return await self.async_step_pair_failed()
            except (PairingFailed, PairingUnsupported) as err:
                self._pair_error = str(err)
                return await self.async_step_pair_failed()
            except Exception:
                _LOGGER.warning("Cannot inspect existing WBS06 Classic Bluetooth bond", exc_info=True)
                self._pair_error = "Cannot verify the existing Classic Bluetooth bond"
                return await self.async_step_pair_failed()
        return await self.async_step_pair_finish()

    async def async_step_pair_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._address is not None and self._kl is not None
        data = {CONF_ADDRESS: self._address, CONF_KL: self._kl, CONF_MODEL_ID: self._model_id}
        if self._users:
            data[CONF_USERS] = self._users
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        return self.async_create_entry(title=self._name or LOCAL_NAME_PREFIX, data=data)

    async def _async_close_pairing(self) -> None:
        if self._pairing is not None:
            try:
                await self._pairing.close()
            except Exception:  # noqa: BLE001 - cleanup must not break the flow
                _LOGGER.debug("Closing the pairing session failed", exc_info=True)
            self._pairing = None

    def async_remove(self) -> None:
        """The user closed the dialog: stop waiting and release the agent."""
        if self._pair_task is not None and not self._pair_task.done():
            self._pair_task.cancel()
        if self._pairing is not None:
            self.hass.async_create_task(self._async_close_pairing())

    # --- Reauth: the scale stopped accepting the link (pairing lost) ---------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._address, self._name = entry.data[CONF_ADDRESS], entry.title
        self._model_id = entry.data.get(CONF_MODEL_ID, BODY_SMART_MODEL_ID)
        self._kl, self._users = entry.data[CONF_KL], entry.data.get(CONF_USERS)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair()
        self._set_confirm_only()
        return self.async_show_form(step_id="reauth_confirm", description_placeholders={"name": self._name or ""})

    # --- Reconfigure: pair again (e.g. after a battery change) ---------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        self._address, self._name = entry.data[CONF_ADDRESS], entry.title
        self._model_id = entry.data.get(CONF_MODEL_ID, BODY_SMART_MODEL_ID)
        self._kl, self._users = entry.data[CONF_KL], entry.data.get(CONF_USERS)
        return await self.async_step_pair()
