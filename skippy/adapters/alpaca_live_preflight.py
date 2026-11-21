"""Check live Alpaca account eligibility using one read-only request."""

import json
import warnings
from getpass import GetPassWarning, getpass

from .alpaca_live_orders import LIVE_API, AlpacaLiveHTTPS, _crypto_is_active
from .alpaca_paper_orders import OrderRefused, _account_is_active

CRYPTO_STATUSES = frozenset({
    "INACTIVE", "ONBOARDING", "SUBMITTED", "SUBMISSION_FAILED", "ACTION_REQUIRED",
    "APPROVAL_PENDING", "APPROVED", "REJECTED", "ACTIVE", "ACCOUNT_UPDATED",
    "ACCOUNT_CLOSED", "ACCOUNT_CLOSED_PENDING", "LIMITED", "PAPER_ONLY",
})


def check_live_account(transport):
    account = transport("GET", f"{LIVE_API}/account", None)
    if type(account) is not dict or not isinstance(account.get("id"), str) or not account["id"]:
        raise OrderRefused("Alpaca live account identity unavailable")
    crypto_status = account.get("crypto_status")
    if crypto_status is None:
        crypto_status = "MISSING"
    elif not isinstance(crypto_status, str) or crypto_status not in CRYPTO_STATUSES:
        crypto_status = "UNRECOGNIZED"
    return {
        "live_account_authenticated": True,
        "account_active_and_unblocked": _account_is_active(account),
        "crypto_active": _crypto_is_active(account),
        "crypto_status": crypto_status,
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
    return 0 if result["account_active_and_unblocked"] and result["crypto_active"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
