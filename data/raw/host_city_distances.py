"""Pre-compute 16x16 Haversine distance matrix between host cities. One-shot."""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
cfg = json.loads((ROOT / "data" / "raw" / "wc2026_config.json").read_text())


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Local time zone offsets (UTC-X) for each host city
TZ_OFFSETS = {
    "Mexico City": -6, "Guadalajara": -6, "Monterrey": -6,
    "Toronto": -4, "Vancouver": -7,
    "Atlanta": -4, "Boston": -4, "Dallas": -5, "Houston": -5,
    "Kansas City": -5, "Los Angeles": -7, "Miami": -4,
    "New York": -4, "Philadelphia": -4, "San Francisco": -7, "Seattle": -7,
}

cities = cfg["host_cities"]
matrix = {}
tz_matrix = {}
for a in cities:
    matrix[a["city"]] = {}
    tz_matrix[a["city"]] = {}
    for b in cities:
        if a["city"] == b["city"]:
            matrix[a["city"]][b["city"]] = 0
            tz_matrix[a["city"]][b["city"]] = 0
        else:
            matrix[a["city"]][b["city"]] = round(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]), 1)
            tz_matrix[a["city"]][b["city"]] = abs(TZ_OFFSETS.get(a["city"], 0) - TZ_OFFSETS.get(b["city"], 0))

out = {
    "schema": "host_city → {host_city: km}",
    "distance_km": matrix,
    "timezone_hours_diff": tz_matrix,
    "tz_offsets_utc": TZ_OFFSETS,
}
out_path = ROOT / "data" / "raw" / "host_city_distance_matrix.json"
out_path.write_text(json.dumps(out, indent=2))
print(f"Wrote {out_path}")
print(f"Sample distances:")
print(f"  Vancouver → Miami: {matrix['Vancouver']['Miami']:.0f} km")
print(f"  Mexico City → Toronto: {matrix['Mexico City']['Toronto']:.0f} km")
print(f"  Seattle → Atlanta: {matrix['Seattle']['Atlanta']:.0f} km")
