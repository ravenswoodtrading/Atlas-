from pathlib import Path

FILES = [
    "app/sp_api/client.py",
    "app/services/oa_lookup_service.py",
    "app/services/oa_source_discovery_service.py",
    "app/services/review_queue_service.py",
    "app/routes/review_queue.py",
]

KEYWORDS = [
    "offer",
    "featured",
    "buy box",
    "buy_box",
    "availability",
    "stock",
    "price",
    "fulfillment",
    "condition",
    "in_stock",
    "out_of_stock",
]

for filename in FILES:
    path = Path(filename)

    print("\n" + "=" * 80)
    print(filename)
    print("=" * 80)

    if not path.exists():
        print("FILE NOT FOUND")
        continue

    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    matches = set()

    for i, line in enumerate(lines):
        lower = line.lower()

        if any(keyword in lower for keyword in KEYWORDS):
            start = max(0, i - 8)
            end = min(len(lines), i + 12)

            for n in range(start, end):
                matches.add(n)

    if not matches:
        print("No matching offer/availability logic found.")
        continue

    for n in sorted(matches):
        print(f"{n + 1:5}: {lines[n]}")

print("\n" + "=" * 80)
print("DONE")
print("=" * 80)