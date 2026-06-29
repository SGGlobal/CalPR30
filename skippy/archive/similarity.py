from index import load_all


def similarity_score(current, previous):
    score = 0

    if current.get("decision") == previous.get("decision"):
        score += 1

    if current.get("market_id") == previous.get("market_id"):
        score += 1

    confidence_diff = abs(
        current.get("confidence", 0)
        - previous.get("confidence", 0)
    )

    score += max(0, 1 - confidence_diff)

    return score


def find_similar(current):
    records = load_all()

    ranked = []

    for record in records:
        ranked.append(
            (
                similarity_score(current, record),
                record
            )
        )

    ranked.sort(reverse=True, key=lambda x: x[0])

    return ranked[:5]


if __name__ == "__main__":

    sample = {
        "market_id": "test_market",
        "decision": "WATCH",
        "confidence": 0.0
    }

    results = find_similar(sample)

    for score, record in results:
        print(
            f"{score:.2f} | "
            f"{record['market_id']} | "
            f"{record['decision']}"
        )
