"""Withings account API: fetch the scale's `kl` secret (one-time setup).

Transport-agnostic: build the form parameters with these functions and parse the JSON responses,
using any HTTP client (urllib in the CLI tool, aiohttp in Home Assistant). Credentials go only to
Withings over HTTPS and are never stored.
"""

from __future__ import annotations

from dataclasses import dataclass

API = "https://scalews.withings.net/cgi-bin"
LOGIN_URL = f"{API}/auth"
ASSOCIATION_URL = f"{API}/association"
ACCOUNT_URL = f"{API}/account"
COMMON = {"appname": "wiscale", "apppfm": "android", "appliver": "202609240"}
BODY_SMART_MODEL = 16


class AccountError(Exception):
    """Login or lookup failed. `status` is the API status code when there is one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message if status is None else f"{message} (status {status})")
        self.status = status


class InvalidCredentials(AccountError):
    pass


@dataclass(frozen=True)
class ScaleSecret:
    deviceid: int | None
    model: int | None
    mac: str | None
    kl: str
    accountid: int | None = None


def login_params(email: str, password: str) -> dict:
    return {
        **COMMON,
        "action": "login",
        "email": email,
        "authprovider": "withings",
        "password": password,
        "duration": 900,
        "typename": "mobile",
        "os": "android",
    }


def logout_params(session: str) -> dict:
    return {**COMMON, "action": "logout", "sessionid": session}


def account_params(session: str) -> dict:
    """Retrieve the account ID needed by the legacy association endpoint."""
    return {**COMMON, "action": "get", "sessionid": session, "enrich": "t"}


def association_params(session: str, account_id: int) -> dict:
    return {
        **COMMON, "action": "getbyaccountid", "sessionid": session,
        "accountid": account_id, "type": -1, "enrich": "t",
    }


def users_params(session: str, accountid: int) -> dict:
    return {
        **COMMON,
        "action": "getuserslist",
        "sessionid": session,
        "accountid": accountid,
        "recurse_use": 1,
        "recurse_devtype": 1,
        "listmask": 5,
        "allusers": "t",
    }


def _find(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _find(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find(v, key)


def parse_login(resp: dict) -> str:
    status = resp.get("status")
    if status != 0:
        raise InvalidCredentials("Withings login failed", status)
    session = next(_find(resp, "sessionid"), None)
    if not session:
        raise AccountError("login returned no session (two-factor authentication?)")
    return session


def parse_account_id(resp: dict, email: str) -> int:
    """Find the logged-in account without ever exposing other account details."""
    if resp.get("status") != 0:
        raise AccountError("could not identify Withings account", resp.get("status"))
    body = resp.get("body")
    body = body if isinstance(body, dict) else {}
    accounts = body.get("account") or body.get("accounts") or []
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not isinstance(accounts, list):
        raise AccountError("unexpected Withings account response")
    candidates = [
        candidate for candidate in accounts
        if isinstance(candidate, dict) and candidate.get("id") is not None
    ]
    matches = [
        candidate for candidate in candidates
        if isinstance(candidate.get("email"), str)
        and candidate["email"].strip().casefold() == email.strip().casefold()
    ]
    # Some versions omit email for a single-account response.
    if not matches and len(candidates) == 1 and not candidates[0].get("email"):
        matches = candidates
    if len(matches) != 1:
        raise AccountError("could not uniquely identify the Withings account")
    account_id = matches[0]["id"]
    if isinstance(account_id, bool) or not str(account_id).isdigit():
        raise AccountError("invalid Withings account ID")
    return int(account_id)


def parse_scales(resp: dict, model: int | None = BODY_SMART_MODEL) -> list[ScaleSecret]:
    if resp.get("status") != 0:
        raise AccountError("could not list devices", resp.get("status"))
    entries = [e for found in _find(resp, "associations") for e in (found if isinstance(found, list) else [found])]
    scales = []
    for e in entries:
        props = e.get("deviceproperties") or {}
        m = props.get("model", props.get("modelid"))
        m = int(m) if m is not None and str(m).isdigit() else None
        if e.get("kl") and (model is None or m == model):
            scales.append(
                ScaleSecret(e.get("deviceid"), m, props.get("macaddress") or props.get("mac"), e["kl"], e.get("accountid"))
            )
    return scales


def select_scale(
    scales: list[ScaleSecret], *, model: int, address: str, identity_mac: str | None = None
) -> ScaleSecret:
    """Match the account's scale to its advertisement without guessing among devices.

    The scale's advertised WPP UUID can carry a different identity MAC from the
    address of its Bluetooth radio (WBS06 uses adjacent addresses).
    """
    def normalize(mac: str | None) -> str:
        return "".join(c for c in (mac or "").lower() if c in "0123456789abcdef")

    candidates = [scale for scale in scales if scale.model == model]
    for mac in (identity_mac, address):
        if mac:
            exact = [scale for scale in candidates if scale.mac and normalize(scale.mac) == normalize(mac)]
            if len(exact) == 1:
                return exact[0]
            if len(exact) > 1:
                raise AccountError("multiple account devices share the scale's MAC")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AccountError("no matching scale found on this Withings account")
    raise AccountError("multiple scales match this model; cannot identify the correct key")


def association_diagnostics(
    response: dict, *, address: str, identity_mac: str | None
) -> dict:
    """Summarize a failed key lookup without logging credentials, keys, or identifiers.

    Include only recognized schema field names, numeric model IDs, and boolean
    address matches. Never include account IDs, MACs, session tokens, or raw data.
    """
    def normalize(mac: str | None) -> str:
        return "".join(char for char in (mac or "").lower() if char in "0123456789abcdef")

    allowed_fields = {
        "accountid", "associations", "deviceid", "deviceproperties", "devices",
        "kl", "mac", "macaddress", "mac_address", "model", "modelid", "model_id",
        "type",
    }
    entries = [
        entry
        for found in _find(response, "associations")
        for entry in (found if isinstance(found, list) else [found])
    ]
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            result.append({"association_is_dict": False})
            continue
        props = entry.get("deviceproperties")
        props = props if isinstance(props, dict) else {}
        model = props.get("model", props.get("modelid"))
        api_model = int(model) if model is not None and str(model).isdigit() else None
        mac_values = [
            container.get(field)
            for container in (entry, props)
            for field in ("mac", "macaddress", "mac_address")
            if isinstance(container.get(field), str)
        ]
        result.append(
            {
                "association_fields": sorted(allowed_fields & entry.keys()),
                "device_property_fields": sorted(allowed_fields & props.keys()),
                "api_model": api_model,
                "key_present": bool(entry.get("kl")),
                "key_nested": bool(props.get("kl")),
                "identity_match": bool(identity_mac)
                and any(normalize(value) == normalize(identity_mac) for value in mac_values),
                "ble_address_match": any(normalize(value) == normalize(address) for value in mac_values),
            }
        )
    body = response.get("body")
    return {
        "api_status": response.get("status") if type(response.get("status")) is int else None,
        "body_fields": sorted(allowed_fields & body.keys()) if isinstance(body, dict) else [],
        "association_count": len(entries),
        "associations": result,
    }


def parse_users(resp: dict) -> dict[int, str]:
    """Map Withings user IDs to first names (the IDs the scale tags weigh-ins with)."""
    if resp.get("status") != 0:
        raise AccountError("could not list users", resp.get("status"))
    users = {}
    for found in _find(resp, "users"):
        for u in found if isinstance(found, list) else [found]:
            if isinstance(u, dict) and u.get("id") is not None:
                users[int(u["id"])] = (u.get("firstname") or "").strip() or f"User {u['id']}"
    return users
