"""Check live Alpaca account eligibility using one read-only request."""

import json
import warnings
from getpass import GetPassWarning, getpass

from .alpaca_live_orders import LIVE_API, AlpacaLiveHTTPS, _crypto_is_active
from .alpaca_paper_orders import OrderRefused, _account_is_active


def check_live_account(transport):
    account = transport("GET", f"{LIVE_API}/account", None)
    if type(account) is not dict or not isinstance(account.get("id"), str) or not account["id"]:
        raise OrderRefused("Alpaca live account identity unavailable")
    return {
        "live_account_authenticated": True,
        "account_active_and_unblocked": _account_is_active(account),
        "crypto_active": _crypto_is_active(account),
    }


def main():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            key_id = getpass("Alpaca LIVE key ID: ").strip()
            secret = getpass("Alpaca LIVE secret: ").strip()
        result = check_live_account(AlpacaLiveHTTPS(key_id, secret))
    except OrderRefused as error:
        print(f"Live preflight blocked: {error}")
        return 1
    except (EOFError, KeyboardInterrupt, GetPassWarning):
        print("Live preflight stopped before authentication.")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if all(result.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
