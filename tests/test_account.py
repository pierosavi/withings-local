import pytest

from withings_wpp import account


def test_login_params_never_hash_password():
    p = account.login_params("user@example.com", "pw")
    assert p["password"] == "pw" and p["authprovider"] == "withings" and "hash" not in p


def test_parse_login():
    assert account.parse_login({"status": 0, "body": {"sessionid": "abc"}}) == "abc"
    with pytest.raises(account.InvalidCredentials):
        account.parse_login({"status": 100})
    with pytest.raises(account.AccountError):
        account.parse_login({"status": 0, "body": {}})


def test_account_lookup_supplies_account_id_to_associations():
    params = account.account_params("synthetic-session")
    assert params["action"] == "get"
    assert params["sessionid"] == "synthetic-session"
    assert "accountid" not in params
    account_id = account.parse_account_id(
        {
            "status": 0,
            "body": {
                "account": [
                    {"id": 2, "email": "other@example.com"},
                    {"id": 42, "email": "user@example.com"},
                ]
            },
        },
        "User@Example.com",
    )
    assert account_id == 42
    association = account.association_params("synthetic-session", account_id)
    assert association["action"] == "getbyaccountid"
    assert association["accountid"] == 42
    assert association["type"] == -1


def test_parse_account_id_rejects_ambiguous_or_failed_account_lookup():
    with pytest.raises(account.AccountError, match="could not uniquely identify"):
        account.parse_account_id({"status": 0, "body": {"account": []}}, "user@example.com")
    with pytest.raises(account.AccountError, match="could not uniquely identify"):
        account.parse_account_id(
            {"status": 0, "body": {"account": [{"id": 1, "email": "somebody@example.com"}]}},
            "user@example.com",
        )
    with pytest.raises(account.AccountError, match="status 239"):
        account.parse_account_id({"status": 239, "body": {}}, "user@example.com")
    with pytest.raises(account.AccountError, match="uniquely identify"):
        account.parse_account_id(
            {"status": 0, "body": {"account": [
                {"id": 1, "email": "user@example.com"},
                {"id": 2, "email": "user@example.com"},
            ]}},
            "user@example.com",
        )


def test_parse_account_id_accepts_anonymous_single_account_only():
    assert account.parse_account_id(
        {"status": 0, "body": {"account": {"id": "42"}}}, "user@example.com"
    ) == 42
    with pytest.raises(account.AccountError, match="could not uniquely"):
        account.parse_account_id(
            {"status": 0, "body": {"account": [{"id": 1}, {"id": 2}]}}, "user@example.com"
        )


def test_parse_scales_filters_model_and_kl():
    resp = {
        "status": 0,
        "body": {
            "associations": [
                {"deviceid": 1, "kl": "k1", "deviceproperties": {"model": 16, "macaddress": "00:11:22:33:44:55"}},
                {"deviceid": 2, "kl": "k2", "deviceproperties": {"model": 93}},
                {"deviceid": 3, "deviceproperties": {"model": 16}},
            ]
        },
    }
    (s,) = account.parse_scales(resp)
    assert (s.deviceid, s.model, s.mac, s.kl) == (1, 16, "00:11:22:33:44:55", "k1")
    assert len(account.parse_scales(resp, model=None)) == 2


def test_parse_users_and_accountid():
    resp = {"status": 0, "body": {"users": [{"id": 1001, "firstname": "Alice"}, {"id": 1002, "firstname": ""}]}}
    assert account.parse_users(resp) == {1001: "Alice", 1002: "User 1002"}
    scales = account.parse_scales(
        {"status": 0, "body": {"associations": [{"accountid": 77, "kl": "k", "deviceproperties": {"model": 16}}]}}
    )
    assert scales[0].accountid == 77


def test_select_scale_matches_advertised_identity_instead_of_ble_address():
    scales = [
        account.ScaleSecret(1, 7, "00:11:22:33:44:55", "another-scale"),
        account.ScaleSecret(2, 7, "02:00:00:00:56:5E", "correct-scale"),
    ]
    selected = account.select_scale(
        scales, model=7, address="02:00:00:00:56:5F", identity_mac="02:00:00:00:56:5e"
    )
    assert selected.deviceid == 2
    assert selected.kl == "correct-scale"


def test_select_scale_does_not_use_a_different_models_key_on_address_match():
    scales = [
        account.ScaleSecret(1, 16, "02:00:00:00:56:5E", "wrong-model"),
        account.ScaleSecret(2, 7, "02:00:00:00:56:5F", "correct-model"),
    ]
    selected = account.select_scale(
        scales, model=7, address="02:00:00:00:56:5F", identity_mac="02:00:00:00:56:5E"
    )
    assert selected.kl == "correct-model"


def test_select_scale_uses_unique_model_only_when_address_is_unavailable():
    scales = [account.ScaleSecret(1, 16, None, "body-smart"), account.ScaleSecret(2, 7, None, "body")]
    assert account.select_scale(scales, model=7, address="00:00:00:00:00:00").kl == "body"


def test_select_scale_rejects_ambiguous_account():
    scales = [account.ScaleSecret(1, 7, None, "first"), account.ScaleSecret(2, 7, None, "second")]
    with pytest.raises(account.AccountError, match="multiple scales"):
        account.select_scale(scales, model=7, address="02:00:00:00:56:5F")


def test_select_scale_rejects_wrong_account():
    scales = [account.ScaleSecret(1, 16, "00:11:22:33:44:55", "body-smart")]
    with pytest.raises(account.AccountError, match="no matching scale"):
        account.select_scale(scales, model=7, address="02:00:00:00:56:5F")


def test_association_diagnostics_redacts_secrets_and_identifiers():
    response = {
        "status": 0,
        "body": {
            "sessionid": "SECRET_SESSION",
            "associations": [
                {
                    "deviceid": 123456789,
                    "accountid": 987654321,
                    "kl": "SECRET_SCALE_KEY",
                    "email": "private@example.com",
                    "deviceproperties": {
                        "model": 99,
                        "macaddress": "02:00:00:00:56:5E",
                        "serial": "SECRET_SERIAL",
                    },
                }
            ],
        },
    }
    diagnostics = account.association_diagnostics(
        response, address="02:00:00:00:56:5F", identity_mac="02:00:00:00:56:5E"
    )
    assert diagnostics["association_count"] == 1
    assert diagnostics["associations"][0]["api_model"] == 99
    assert diagnostics["associations"][0]["key_present"]
    assert diagnostics["associations"][0]["identity_match"]
    assert not diagnostics["associations"][0]["ble_address_match"]
    representation = repr(diagnostics)
    for private in (
        "SECRET_SESSION", "SECRET_SCALE_KEY", "private@example.com",
        "SECRET_SERIAL", "123456789", "987654321",
        "02:00:00:00:56:5E", "02:00:00:00:56:5F",
    ):
        assert private not in representation


def test_association_diagnostics_handles_missing_associations():
    result = account.association_diagnostics(
        {"status": 0, "body": {"devices": [{"token": "SECRET"}]}},
        address="02:00:00:00:56:5F",
        identity_mac=None,
    )
    assert result == {
        "api_status": 0,
        "body_fields": ["devices"],
        "association_count": 0,
        "associations": [],
    }
