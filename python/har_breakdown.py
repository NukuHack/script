#!/usr/bin/env python3
"""Run locally: python3 har_breakdown.py canvas.elte.hu_Archive.har
Prints where the HAR's size and entry count actually went — by mime category,
plus the most-repeated URLs (polling) and the biggest single entries."""
import json, sys
from collections import defaultdict

path = sys.argv[1]
entries = json.load(open(path, encoding="utf-8"))["log"]["entries"]

CATS = {"video": "video", "audio": "audio", "image": "image", "font": "font",
        "javascript": "script", "css": "stylesheet", "html": "html", "json": "json"}

by_cat_count, by_cat_bytes = defaultdict(int), defaultdict(int)
url_count, url_bytes = defaultdict(int), defaultdict(int)
total_bytes = 0

for e in entries:
    mime = (e.get("response", {}).get("content", {}).get("mimeType", "") or "").lower()
    size = e.get("response", {}).get("content", {}).get("size", 0) or 0
    total_bytes += size
    cat = next((v for k, v in CATS.items() if k in mime), mime.split("/")[0] or "(none)")
    by_cat_count[cat] += 1
    by_cat_bytes[cat] += size
    u = e["request"]["url"].split("?")[0]
    url_count[u] += 1
    url_bytes[u] += size

print(f"{len(entries)} entries, {total_bytes/1_048_576:.1f} MB of response content\n")
print(f"{'category':<12}{'entries':>10}{'MB':>12}")
for cat in sorted(by_cat_bytes, key=lambda c: -by_cat_bytes[c]):
    print(f"{cat:<12}{by_cat_count[cat]:>10}{by_cat_bytes[cat]/1_048_576:>12.1f}")

print("\nTop 10 most-repeated URLs (polling / re-fetching):")
for u in sorted(url_count, key=lambda u: -url_count[u])[:10]:
    print(f"  {url_count[u]:>4}x  {url_bytes[u]/1_048_576:>7.1f} MB  {u}")

print("\nTop 10 single biggest responses:")
singles = sorted(((e.get("response", {}).get("content", {}).get("size", 0) or 0), e["request"]["url"])
                  for e in entries)[-10:]
for size, u in reversed(singles):
    print(f"  {size/1_048_576:>7.1f} MB  {u}")
