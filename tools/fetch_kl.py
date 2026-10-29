"""Fetch your scale's `kl` secret from your own Withings account (one-time setup).

Usage: uv run tools/fetch_kl.py [--model 7|16]

The scale asks for proof that the client knows `kl`, a per-scale secret that Withings creates
when the scale is added to an account. The official app downloads it from the account API, and
this script does the same:
1. Log in with your Withings email and password. They are typed here, never stored, and sent
   only to Withings over HTTPS.
2. Select one supported scale by model and save its `kl` to
   `secrets/scale.json` (git-ignored, readable only by you). If the
   account has multiple scales of that model, use the Home Assistant
   setup flow instead, which matches the advertised device identity.
3. Log out.

Accounts that sign in with Google or Apple need a Withings password first (set one in the app).
Nothing secret is printed.
"""

import argparse
import getpass
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from withings_wpp import account

OUT = Path(__file__).resolve().parent.parent / "secrets" / "scale.json"


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # noqa: PLC0415 - optional, helps Pythons without system CAs

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def post(url: str, params: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers={"User-Agent": "withings-local"})
    with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", type=int, choices=(7, account.BODY_SMART_MODEL),
        default=account.BODY_SMART_MODEL,
        help="Withings model ID: 7 = Body (WBS06), 16 = Body Smart (WBS13; default)",
    )
    args = parser.parse_args()
    print("Withings login (sent only to Withings over HTTPS; nothing is stored).")
    email = input("Email: ").strip()
    password = getpass.getpass("Password (hidden): ")
    try:
        session = account.parse_login(post(account.LOGIN_URL, account.login_params(email, password)))
    except account.AccountError as e:
        print(f"\n✗ {e}. Google/Apple accounts need a Withings password.")
        sys.exit(1)
    finally:
        del password
    print("✓ Logged in")
    try:
        account_id = account.parse_account_id(post(account.ACCOUNT_URL, account.account_params(session)), email)
        scales = account.parse_scales(
            post(account.ASSOCIATION_URL, account.association_params(session, account_id)),
            model=args.model,
        )
        if not scales:
            print(f"✗ No model {args.model} scale with a kl secret found on this account.")
            sys.exit(1)
        if len(scales) > 1:
            print("✗ Multiple scales of this model found. Use Home Assistant setup to match the correct device.")
            sys.exit(1)
        s = scales[0]
        OUT.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"model": s.model, "deviceid": s.deviceid, "mac": s.mac, "kl": s.kl}, f, indent=2)
        print(f"✓ Saved kl ({len(s.kl)} characters) to secrets/scale.json (mode 600, git-ignored)")
    except account.AccountError as e:
        print(f"✗ {e}")
        sys.exit(1)
    finally:
        try:
            post(account.LOGIN_URL, account.logout_params(session))
            print("✓ Logged out")
        except Exception:  # noqa: BLE001 - logout is best effort
            pass


if __name__ == "__main__":
    main()
