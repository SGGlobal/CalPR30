from pathlib import Path
import json


class Memory:
    def __init__(self):
        self.archive = Path("skippy/archive/decisions")

    def all(self):
        records = []

        if not self.archive.exists():
            return records

        for file in self.archive.glob("*.json"):
            with open(file, encoding="utf-8") as f:
                records.append(json.load(f))

        return records

    def search(self, decision=None):
        records = self.all()

        if decision is None:
            return records

        return [
            r
            for r in records
            if r.get("decision") == decision
        ]


if __name__ == "__main__":
    memory = Memory()

    print(f"Loaded {len(memory.all())} decisions")