#!/usr/bin/env python3
"""
Merge Strava archive export (activities.json/csv) into data.json.
Adds bike & e-bike activity names, distance, and HR data (if missing from Coros).

Usage:
    python merge_strava.py <path_to_strava_archive_dir>
    
The Strava archive dir is the folder extracted from your Strava data export ZIP.
It should contain activities.csv (or activities.json).
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

STRAVA_TYPES = {
    "Ride", "EBikeRide", "MountainBikeRide", "GravelRide",
    "RoadBikeRide", "VirtualRide", "Workout",
    "Run", "TrailRun", "VirtualRun",
    "Walk", "Hike",
}


def load_strava_csv(path: str) -> list[dict]:
    """Load Strava activities.csv (semicolon or comma separated)."""
    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        f.seek(0)
        reader = csv.DictReader(f, dialect=dialect)
        return list(reader)


def parse_date_local(row: dict) -> str | None:
    """Extract YYYY-MM-DD from various date fields (including 'Jul 13, 2026, 9:28:25 AM')."""
    for key in ("Activity Date", "Activity Day", "start_date_local", "Start Time"):
        val = row.get(key)
        if val:
            val = val.strip()
            # Try ISO format first
            try:
                return datetime.fromisoformat(val).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                pass
            # Try "Mon DD, YYYY, HH:MM:SS AM/PM" format
            try:
                # Remove day name prefix, split by comma
                # "Jul 13, 2026, 9:28:25 AM" → "Jul 13 2026" part
                parts = val.split(",")
                # parts[0] = "Jul 13", parts[1] = " 2026", parts[2] = " 9:28:25 AM"
                if len(parts) >= 2:
                    date_part = parts[0].strip() + " " + parts[1].strip().split(" ")[0]
                    return datetime.strptime(date_part, "%b %d %Y").strftime("%Y-%m-%d")
            except (ValueError, IndexError):
                pass
            # Try DD/MM/YYYY or MM/DD/YYYY
            for sep in ("/", "-", "."):
                parts = val.split(sep)
                if len(parts) >= 3:
                    # Could be YYYY-MM-DD
                    if len(parts[0]) == 4 and parts[0].isdigit():
                        return f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
                    # Could be DD/MM/YYYY or MM/DD/YYYY
                    if len(parts[2]) == 4 and parts[2].isdigit():
                        if int(parts[1]) > 12:
                            return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                        return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return None


def get_val(row: dict, *keys: str) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is not None and v.strip():
            return v.strip()
    return None


def to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python merge_strava.py <strava_archive_dir_or_file>")
        sys.exit(1)

    src = sys.argv[1]
    if os.path.isdir(src):
        csv_path = os.path.join(src, "activities.csv")
        json_path = os.path.join(src, "activities.json")
    elif os.path.isfile(src):
        csv_path = src if src.endswith(".csv") else None
        json_path = src if src.endswith(".json") else None
    else:
        print(f"Nie znaleziono: {src}")
        sys.exit(1)

    activities: list[dict] = []
    if csv_path and os.path.exists(csv_path):
        print(f"Wczytuję {csv_path}…")
        activities = load_strava_csv(csv_path)
        print(f"  {len(activities)} wierszy")
    elif json_path and os.path.exists(json_path):
        print(f"Wczytuję {json_path}…")
        with open(json_path, "r", encoding="utf-8") as f:
            activities = json.load(f)
        print(f"  {len(activities)} aktywności")
    else:
        print("Nie znaleziono pliku activities.csv ani activities.json w podanej ścieżce.")
        print(f"Szukałem: {csv_path} lub {json_path}")
        sys.exit(1)

    # Load current data.json
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(script_dir, "data.json")
    if not os.path.exists(data_path):
        print(f"Nie znaleziono data.json w {script_dir}")
        print("Uruchom najpierw fetch_coros.py")
        sys.exit(1)

    with open(data_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        days = raw["days"]
    else:
        days = raw
    day_map: dict[str, dict] = {d["date"]: d for d in days}

    stats = {"added_names": 0, "added_dist": 0, "added_hr": 0}
    bike_activities = 0

    for act in activities:
        # Get activity type
        act_type = get_val(act, "Activity Type", "type", "activity_type", "sport_type")
        if not act_type:
            continue
        act_type = act_type.strip().lower()

        # Only process bike/e-bike/ride activities
        is_bike = any(kw in act_type for kw in ["ebike", "e-bike", "e_bike", "ride", "cycling", "bike", "gravel", "mtb", "mountain"])
        if not is_bike:
            continue

        # Get date
        ds = parse_date_local(act)
        if not ds:
            continue

        bike_activities += 1
        if ds not in day_map:
            day_map[ds] = {"date": ds, "exerciseNames": []}
            days.append(day_map[ds])

        entry = day_map[ds]

        # Add activity name
        if "exerciseNames" not in entry or entry["exerciseNames"] is None:
            entry["exerciseNames"] = []
        name = act_type
        if name not in entry["exerciseNames"]:
            entry["exerciseNames"].append(name)
            stats["added_names"] += 1

        # Add distance
        dist_m = (
            to_float(get_val(act, "Distance", "distance"))
            or (act.get("distance") if isinstance(act.get("distance"), (int, float)) else None)
        )
        if dist_m and dist_m > 0:
            existing = entry.get("distance_m") or 0
            if not existing:
                # First distance
                entry["distance_m"] = dist_m
                entry["dist"] = round(dist_m / 1000, 2)
                stats["added_dist"] += 1
            else:
                # Multiple activities same day – sum distances
                entry["distance_m"] = existing + dist_m
                entry["dist"] = round(entry["distance_m"] / 1000, 2)
                stats["added_dist"] += 1

        # Add HR only if Coros didn't capture it that day
        avg_hr = to_float(get_val(act, "Average Heart Rate", "average_heartrate", "average_heart_rate"))
        if avg_hr and avg_hr > 30 and entry.get("hrAvg") is None:
            entry["hrAvg"] = avg_hr
            stats["added_hr"] += 1

        max_hr = to_float(get_val(act, "Max Heart Rate", "max_heartrate", "max_heart_rate"))
        if max_hr and max_hr > 30 and entry.get("hrMax") is None:
            entry["hrMax"] = max_hr
            stats["added_hr"] += 1

    # Stats
    print(f"\nZnaleziono {bike_activities} aktywności rowerowych/e-bike w danych Strava.")
    print(f"Dodano/uzupełniono:")
    print(f"  nazwy aktywności: {stats['added_names']} dni")
    print(f"  dystans:          {stats['added_dist']} dni")
    print(f"  tętno:            {stats['added_hr']} dni")

    # Deduplicate exercise names
    for d in days:
        if d.get("exerciseNames"):
            d["exerciseNames"] = list(set(d["exerciseNames"]))

    # Sort days and write
    days.sort(key=lambda d: d["date"])
    output = {"_syncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "days": days}

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Coverage
    print(f"\nZapisano {len(days)} dni -> {data_path}")


if __name__ == "__main__":
    main()
