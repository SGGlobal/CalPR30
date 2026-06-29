from index import load_all


def search(decision=None, min_confidence=None, market_id=None):
    records = load_all()

    results = []

    for record in records:
        if decision and record.get("decision") != decision:
            continue

        if min_confidence is not None:
            if record.get("confidence", 0) < min_confidence:
                continue

        if market_id and record.get("market_id") != market_id:
            continue

        results.append(record)

    return results


if __name__ == "__main__":
    matches = search(decision="WATCH")

    print(f"Found {len(matches)} matching decisions\n")

    for m in matches:
        print(
            f"{m['timestamp']} | "
            f"{m['market_id']} | "
            f"{m['decision']} | "
            f"{m['confidence']}"
        )