"""Configuration and client helpers for QLA."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    private_key: str
    public_key: str
    poly_api_key: str
    poly_api_secret: str
    poly_passphrase: str
    dry_run: bool
    bet_size_usd: float
    environment_loaded: bool


def _load_dotenv(path: str = ".env") -> bool:
    """Load simple KEY=VALUE pairs without requiring python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
    return True


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def get_settings() -> Settings:
    environment_loaded = _load_dotenv()
    return Settings(
        private_key=os.getenv("PRIVATE_KEY", ""),
        public_key=os.getenv("PUBLIC_KEY", ""),
        poly_api_key=os.getenv("POLY_API_KEY", ""),
        poly_api_secret=os.getenv("POLY_API_SECRET", ""),
        poly_passphrase=os.getenv("POLY_PASSPHRASE", ""),
        dry_run=True,
        bet_size_usd=float(os.getenv("BET_SIZE_USD", "5") or 5),
        environment_loaded=environment_loaded,
    )


class PolymarketClient:
    """Small HTTP client wrapper for Polymarket Gamma and CLOB APIs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.gamma_base_url = "https://gamma-api.polymarket.com"
        self.clob_base_url = "https://clob.polymarket.com"

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 QLA/2.0",
        }
        if self.settings.poly_api_key:
            headers.update({
                "POLY-API-KEY": self.settings.poly_api_key,
                "POLY-API-SECRET": self.settings.poly_api_secret,
                "POLY-API-PASSPHRASE": self.settings.poly_passphrase,
            })
        return headers

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        import urllib.parse
        import urllib.request

        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"
        request = urllib.request.Request(url, headers=self.headers(), method="GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            import json

            return json.loads(response.read().decode("utf-8"))

    def post_json(self, url: str, payload: dict[str, Any]) -> Any:
        import json
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self.headers(), method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def delete_json(self, url: str, payload: dict[str, Any]) -> Any:
        import json
        import urllib.request

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self.headers(), method="DELETE")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


def build_client() -> PolymarketClient:
    """Build a client from environment variables. DRY_RUN defaults to True."""
    return PolymarketClient(get_settings())
