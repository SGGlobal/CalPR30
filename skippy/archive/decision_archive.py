import json
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_DIR = Path("skippy/archive/decisions")
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def save_decision(decision: dict) -> Path:
    decision["timestamp"] = datetime.now(timezone.utc).isoformat()

    market_id = decision.get("market_id", "unknown")
    filename = f"{decision['timestamp'].replace(':', '-')}_{market_id}.json"
    path = ARCHIVE_DIR / filename

    with path.open("w", encoding="utf-8") as f:
        json.dump(decision, f, indent=2)

    return path


if __name__ == "__main__":
    sample = {
        "project": "Skippy",
        "phase": 6,
        "market_id": "test_market",
        "question": "Should Skippy act or watch?",
        "confidence": 0.0,
        "edge": 0.0,
        "decision": "WATCH",
        "dry_run": True,
        "reasoning": {
            "summary": "Phase 6 archive test.",
            "risk": "No real action taken."
        }
    }

    saved = save_decision(sample)
    print(f"Decision saved: {saved}")