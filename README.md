# QLA Polymarket Bot Framework

QLA is an original, clean Polymarket bot framework for BTC 5-minute UP/DOWN markets. It is built to default to dry-run mode and avoid duplicate order placement when order state is uncertain.

## Warning

Real trading can lose money. This project defaults to `DRY_RUN=True`; do not set `DRY_RUN=False` unless you understand the risks and have added and audited real order signing.

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install --upgrade pip
```

This implementation uses Python standard-library HTTP helpers, so no extra package is required for the core dry-run bot.

## Environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Fill in only the environment variables you intend to use:

```env
PRIVATE_KEY=
PUBLIC_KEY=
POLY_API_KEY=
POLY_API_SECRET=
POLY_PASSPHRASE=
DRY_RUN=True
BET_SIZE_USD=5
```

Optional strategy tuning variables:

```env
FAVORITE_THRESHOLD=0.70
MAX_ASK_CAP=0.97
```

## Run in dry-run mode

Keep this in `.env`:

```env
DRY_RUN=True
```

Then run:

```bash
python main_qla.py
```

The terminal dashboard shows:

- Bot name QLA
- Current market question
- Time left
- UP bid/ask
- DOWN bid/ask
- Favored side
- Last action
- Attempts, entries, and skips
- Dry-run status

## Safety notes

- `DRY_RUN` defaults to `True` if missing or invalid.
- Live order signing is intentionally not implemented in this clean framework.
- If order status is unknown or an API call fails after an order attempt, QLA refuses duplicate placement.
- Events are logged to `logs/qla_events.log` with secret-like fields redacted.
