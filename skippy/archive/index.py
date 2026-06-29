import json
from pathlib import Path

ARCHIVE = Path("skippy/archive/decisions")


def load_all():
    records = []

    if not ARCHIVE.exists():
        return records

    for file in ARCHIVE.glob("*.json"):
        try:
            with open(file, encoding="utf-8") as f:
                records.append(json.load(f))
        except Exception:
            pass

    return records


if __name__ == "__main__":
    decisions = load_all()

    print(f"Loaded {len(decisions)} decisions")

    for d in decisions:
        print(
            f"{d['timestamp']} | "
            f"{d.get('market_id')} | "
            f"{d.get('decision')} | "
            f"{d.get('confidence')}"
        )