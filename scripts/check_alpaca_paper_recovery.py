"""Read Alpaca paper recovery state before restarting a stopped runner.

Run on CAL with Python 3.12. Use the paper credentials at the hidden prompts or
supply APCA_PAPER_KEY_ID and APCA_PAPER_SECRET_KEY via a trusted launcher.
Exit codes: 0 = paper snapshot clear; 1 = check failed; 2 = needs reconciliation.
A clear snapshot is an account observation, not authorization to start a runner.
"""

import getpass
import http.client
import json
import os
import warnings
from datetime import datetime, timezone

PAPER_HOST = "paper-api.alpaca.markets"
ORDER_LIMIT = 500
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class CheckError(Exception):
    """An incomplete or unsuccessful paper read."""


def get_json(path, headers):
    connection = http.client.HTTPSConnection(PAPER_HOST, timeout=15)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise CheckError(f"Alpaca paper API returned HTTP {response.status}.")
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CheckError("Alpaca paper response exceeded the read limit.")
        return json.loads(body)
    except (OSError, http.client.HTTPException, ValueError):
        raise CheckError("Alpaca paper response could not be read.") from None
    finally:
        connection.close()


def read_snapshot(headers):
    account = get_json("/v2/account", headers)
    positions = get_json("/v2/positions", headers)
    orders = get_json(f"/v2/orders?status=open&limit={ORDER_LIMIT}", headers)
    if not isinstance(account, dict) or any(
        not isinstance(rows, list)
        or any(not isinstance(row, dict) or not row.get("symbol") for row in rows)
        for rows in (positions, orders)
    ):
        raise CheckError("Alpaca paper response had an unexpected shape.")

    unblocked = (
        account.get("status") == "ACTIVE"
        and account.get("account_blocked") is False
        and account.get("trading_blocked") is False
    )
    return unblocked, len(positions), len(orders)


def read_credentials():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        key = os.environ.get("APCA_PAPER_KEY_ID") or getpass.getpass("Alpaca PAPER key ID: ")
        secret = os.environ.get("APCA_PAPER_SECRET_KEY") or getpass.getpass(
            "Alpaca PAPER secret: "
        )
    if not key.strip() or not secret.strip():
        raise CheckError("Both paper credentials are required.")
    return {"APCA-API-KEY-ID": key.strip(), "APCA-API-SECRET-KEY": secret.strip()}


def main():
    try:
        unblocked, positions, orders = read_snapshot(read_credentials())
    except CheckError as error:
        print(f"Paper check failed. {error}")
        return 1
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("Paper check stopped. Use a terminal with hidden input or protected credentials.")
        return 1

    print(f"Checked at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"Paper account active and unblocked: {'yes' if unblocked else 'no or unknown'}")
    print(f"Open paper positions: {positions}")
    suffix = " or more (response limit reached)" if orders >= ORDER_LIMIT else ""
    print(f"Open paper orders: {orders}{suffix}")
    if not unblocked or positions or orders:
        print("Result: RECONCILIATION REQUIRED")
        return 2
    print("Result: PAPER SNAPSHOT CLEAR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
