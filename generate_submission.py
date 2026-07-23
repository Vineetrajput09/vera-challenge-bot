"""
Generate submission.jsonl: one composed message per (merchant, trigger[, customer])
pair in dataset_expanded/test_pairs.json, by actually calling composer.compose().

Usage:
    python dataset/generate_dataset.py --seed-dir dataset --out dataset_expanded
    python generate_submission.py
"""
from __future__ import annotations

import json
from pathlib import Path

import composer

DATASET_DIR = Path(__file__).parent / "dataset_expanded"
OUT_PATH = Path(__file__).parent / "submission.jsonl"


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    categories = {c["slug"]: c for c in (load(p) for p in (DATASET_DIR / "categories").glob("*.json"))}
    merchants = {m["merchant_id"]: m for m in (load(p) for p in (DATASET_DIR / "merchants").glob("*.json"))}
    customers = {c["customer_id"]: c for c in (load(p) for p in (DATASET_DIR / "customers").glob("*.json"))}
    triggers = {t["id"]: t for t in (load(p) for p in (DATASET_DIR / "triggers").glob("*.json"))}

    test_pairs = load(DATASET_DIR / "test_pairs.json")["pairs"]

    lines = []
    for pair in test_pairs:
        test_id = pair["test_id"]
        trigger = triggers[pair["trigger_id"]]
        merchant = merchants[pair["merchant_id"]]
        category = categories[merchant["category_slug"]]
        customer = customers.get(pair["customer_id"]) if pair.get("customer_id") else None

        composed = composer.compose(category, merchant, trigger, customer)
        lines.append({
            "test_id": test_id,
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    print(f"Wrote {len(lines)} lines to {OUT_PATH}")


if __name__ == "__main__":
    main()
