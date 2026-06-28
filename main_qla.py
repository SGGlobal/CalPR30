"""QLA entrypoint."""

from __future__ import annotations

import argparse
import sys
import urllib.error

from polymarket.client import PolymarketClient, build_client

REQUIRED_ENV_VARS = (
    "PRIVATE_KEY",
    "PUBLIC_KEY",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_PASSPHRASE",
)


class ConnectionCheckError(Exception):
    """Raised when the connection test cannot complete successfully."""


def _format_http_error(error: urllib.error.HTTPError) -> str:
    detail = error.reason
    try:
        body = error.read().decode("utf-8").strip()
    except Exception:
        body = ""
    if body:
        detail = f"{detail}: {body}"
    return f"HTTP {error.code} {detail}"


def _require_configuration(client: PolymarketClient) -> None:
    settings = client.settings
    values = {
        "PRIVATE_KEY": settings.private_key,
        "PUBLIC_KEY": settings.public_key,
        "POLY_API_KEY": settings.poly_api_key,
        "POLY_API_SECRET": settings.poly_api_secret,
        "POLY_PASSPHRASE": settings.poly_passphrase,
    }
    missing = [name for name in REQUIRED_ENV_VARS if not values[name]]
    if missing:
        raise ConnectionCheckError(f"Missing required environment variables: {', '.join(missing)}")
    if not settings.environment_loaded:
        raise ConnectionCheckError("Environment file .env was not found")
    if not settings.dry_run:
        raise ConnectionCheckError("DRY_RUN must remain True")


def _verify_api_reachable(client: PolymarketClient) -> None:
    try:
        client.get_json(f"{client.gamma_base_url}/markets", {"limit": 1})
    except urllib.error.HTTPError as error:
        raise ConnectionCheckError(f"API reachability check failed: {_format_http_error(error)}") from error
    except urllib.error.URLError as error:
        raise ConnectionCheckError(f"API reachability check failed: {error.reason}") from error
    except TimeoutError as error:
        raise ConnectionCheckError("API reachability check timed out") from error


def _verify_authentication(client: PolymarketClient) -> None:
    try:
        client.get_json(f"{client.clob_base_url}/auth/api-key")
    except urllib.error.HTTPError as error:
        raise ConnectionCheckError(f"Authentication check failed: {_format_http_error(error)}") from error
    except urllib.error.URLError as error:
        raise ConnectionCheckError(f"Authentication check failed: {error.reason}") from error
    except TimeoutError as error:
        raise ConnectionCheckError("Authentication check timed out") from error


def _run_connection_test() -> None:
    client = build_client()
    _require_configuration(client)
    _verify_api_reachable(client)
    _verify_authentication(client)

    print("=====================================")
    print("QLA v0.1")
    print("=====================================")
    print()
    print("Configuration:")
    print("✓ Environment Loaded")
    print("✓ API Keys Loaded")
    print("✓ Wallet Loaded")
    print()
    print("Connection:")
    print("✓ API Reachable")
    print("✓ Authentication Successful")
    print()
    print("Status:")
    print("READY")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QLA Polymarket bot framework")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("test-connection", help="Validate configuration and Polymarket connectivity")
    subparsers.add_parser("discover-market", help="Display the active BTC 5-minute market dashboard")
    args = parser.parse_args(argv)

    if args.command in (None, "test-connection"):
        try:
            _run_connection_test()
        except ConnectionCheckError as error:
            print(f"Connection test failed: {error}", file=sys.stderr)
            return 1
        return 0

    if args.command == "discover-market":
        from strategies.qla_buzzer import run_qla_buzzer

        run_qla_buzzer()
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
