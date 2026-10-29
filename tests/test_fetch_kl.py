"""Offline checks for the optional account-key helper."""

import json
import os
import sys

import pytest

from tools import fetch_kl


def fake_account_api(associations):
    actions = []

    def post(_url, params):
        action = params["action"]
        actions.append(action)
        if action == "login":
            return {"status": 0, "body": {"sessionid": "synthetic-session"}}
        if action == "get":
            return {"status": 0, "body": {"account": [{"id": 42, "email": "user@example.com"}]}}
        if action == "getbyaccountid":
            return {"status": 0, "body": {"associations": associations}}
        if action == "logout":
            return {"status": 0}
        raise AssertionError(f"Unexpected account API action: {action}")

    return post, actions


def prepare_cli(monkeypatch, tmp_path, associations, args):
    post, actions = fake_account_api(associations)
    path = tmp_path / "secrets" / "scale.json"
    monkeypatch.setattr(fetch_kl, "post", post)
    monkeypatch.setattr(fetch_kl, "OUT", path)
    monkeypatch.setattr(fetch_kl.getpass, "getpass", lambda _prompt: "synthetic-password")
    monkeypatch.setattr("builtins.input", lambda _prompt: "user@example.com")
    monkeypatch.setattr(sys, "argv", ["fetch_kl.py", *args])
    return path, actions


def test_fetch_key_for_body_without_exposing_secrets(monkeypatch, tmp_path, capsys):
    path, actions = prepare_cli(monkeypatch, tmp_path, [
        {"kl": "wbs13-private", "deviceproperties": {"model": 16}},
        {"kl": "wbs06-private", "deviceproperties": {"model": 7, "macaddress": "00:11:22:33:44:55"}},
    ], ["--model", "7"])

    fetch_kl.main()

    assert json.loads(path.read_text()) == {
        "model": 7, "deviceid": None, "mac": "00:11:22:33:44:55", "kl": "wbs06-private",
    }
    assert os.stat(path).st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "wbs06-private" not in output
    assert "00:11:22:33:44:55" not in output
    assert actions[-1] == "logout"


def test_fetch_key_refuses_ambiguous_model_without_overwriting_saved_key(monkeypatch, tmp_path, capsys):
    path, actions = prepare_cli(monkeypatch, tmp_path, [
        {"kl": "first-private", "deviceproperties": {"model": 7}},
        {"kl": "second-private", "deviceproperties": {"model": 7}},
    ], ["--model", "7"])
    path.parent.mkdir()
    path.write_text("existing-secret")

    with pytest.raises(SystemExit) as exc:
        fetch_kl.main()

    assert exc.value.code == 1
    assert path.read_text() == "existing-secret"
    assert "multiple" in capsys.readouterr().out.lower()
    assert actions[-1] == "logout"


def test_fetch_key_defaults_to_existing_body_smart_model(monkeypatch, tmp_path):
    path, _ = prepare_cli(monkeypatch, tmp_path, [
        {"kl": "body-smart-private", "deviceproperties": {"model": 16}},
        {"kl": "body-private", "deviceproperties": {"model": 7}},
    ], [])

    fetch_kl.main()

    assert json.loads(path.read_text())["model"] == 16
