#!/usr/bin/env python3
"""
Coros + Google Sheets → Website Data Pipeline

Downloads Google Sheets CSVs (steps/HR/calories/weight) and enriches
with Coros API data (sleep/HRV/resting HR/training metrics).
Writes a single data.json for the dashboard.
"""

import asyncio
import json
import os
import sys
import csv
import io
from datetime import date, timedelta, datetime, timezone

from dotenv import load_dotenv
import httpx

load_dotenv()

from coros_api import (
    login, get_stored_auth, StoredAuth,
    fetch_sleep, fetch_hrv, fetch_activities,
    ENDPOINTS, _auth_headers, _base_url,
    MOBILE_BASE_URLS, _ensure_mobile_token,
)

AUTH_CACHE_PATH = os.environ.get("COROS_AUTH_CACHE", "")


def _load_auth_cache(path: str) -> StoredAuth | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
        tok = d.get("mobile_login_payload")
        return StoredAuth(
            access_token=d["access_token"],
            user_id=d["user_id"],
            region=d.get("region", "eu"),
            timestamp=d["timestamp"],
            mobile_access_token=d.get("mobile_access_token"),
            mobile_login_payload=tok,
        )
    except (KeyError, json.JSONDecodeError, OSError):
        return None


def _save_auth_cache(auth: StoredAuth, path: str) -> None:
    if not path:
        return
    d = {
        "access_token": auth.access_token,
        "user_id": auth.user_id,
        "region": auth.region,
        "timestamp": auth.timestamp,
        "mobile_access_token": auth.mobile_access_token,
        "mobile_login_payload": auth.mobile_login_payload,
    }
    with open(path, "w") as f:
        json.dump(d, f)

# ── Mobile API dataTypes ────────────────────────────────────────────────

MOBILE_DATA_TYPES = {
    1: "calorie",
    3: "step",
    4: "heartRateData",
    6: "rhr",
}

# ── Google Sheets config (same as in index.html) ──────────────────────────

SHEET_ID = "1xPJ8xBAPsDv2NfZaKNtzcv6g8hYCVuA40A9iPKN--eo"
SHEETS = {
    "activity": "1277227831",
    "sleep": "1078830",
    "vitals": "111422029",
    "body": "1828589559",
    "weather": "0",
}

# ── Moon phase (same algorithm as JS) ──────────────

def calc_moon_phase(date_str: str) -> dict:
    from math import floor, pi, cos
    y, m, d = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    a = (14 - m) // 12
    yy = y + 4800 - a
    mm = m + 12 * a - 3
    jdn = d + (153 * mm + 2) // 5 + 365 * yy + yy // 4 - yy // 100 + yy // 400 - 32045
    days = jdn - 2451550.26
    lunations = days / 29.53058867
    phase_age = lunations - int(lunations)
    illum = (1 - cos(phase_age * 2 * pi)) / 2
    if phase_age < 0.03 or phase_age > 0.97:
        phase_name, label = "new", "Nów"
    elif phase_age < 0.22:
        phase_name, label = "waxing_crescent", "Sierp przybywa"
    elif phase_age < 0.28:
        phase_name, label = "first_quarter", "I kwadra"
    elif phase_age < 0.47:
        phase_name, label = "waxing_gibbous", "Przybywa (garby)"
    elif phase_age < 0.53:
        phase_name, label = "full", "Pełnia"
    elif phase_age < 0.72:
        phase_name, label = "waning_gibbous", "Ubywa (garby)"
    elif phase_age < 0.78:
        phase_name, label = "third_quarter", "III kwadra"
    else:
        phase_name, label = "waning_crescent", "Sierp ubywa"
    return {"moonPhase": phase_name, "moonLabel": label, "moonIllum": round(illum * 100), "moonAge": phase_age}


# ── helpers ────────────────────────────────────────────────────────────────


def to_n(val: str | None) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def key_date(d: date | None) -> str | None:
    if d is None:
        return None
    return d.isoformat()


def norm_date(ds: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD."""
    if len(ds) == 8 and ds.isdigit():
        return f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
    return ds


def parse_date(val: str) -> date | None:
    if not val:
        return None
    s = val.strip().split(" ")[0]
    # DD.MM.YYYY
    parts = s.split(".")
    if len(parts) == 3 and len(parts[2]) == 4:
        try:
            return date(int(parts[2]), int(parts[1]), int(parts[0]))
        except ValueError:
            pass
    # ISO-like fallback
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    return None


def parse_csv(text: str) -> list[dict]:
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return []
    headers = [h.strip().strip('"') for h in lines[0].split(",")]
    result = []
    for line in lines[1:]:
        vals = list(csv.reader([line]))[0] if line.strip() else []
        row = {}
        for i, h in enumerate(headers):
            row[h] = (vals[i].strip().strip('"') if i < len(vals) else "")
        result.append(row)
    return result


def get_val(row: dict, partial: str) -> str | None:
    target = partial.lower().replace(" ", "")
    for k, v in row.items():
        if target in k.lower().replace(" ", ""):
            return v
    return None


# ── Google Sheets data ───────────────────────────────────────────────────


async def fetch_sheet(client: httpx.AsyncClient, gid: str) -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
    resp = await client.get(url)
    resp.raise_for_status()
    return parse_csv(resp.text)


async def load_google_sheets() -> dict[str, dict]:
    """Download all 4 sheets and merge into day_map (same logic as processData in JS)."""
    SRC_PRIORITY = ["com.yf.smart.coros.dist", "nl.appyhapps.healthsync", "android"]

    def src_rank(s: str | None) -> int:
        if not s:
            return 99
        sl = s.lower()
        for i, p in enumerate(SRC_PRIORITY):
            if p in sl:
                return i
        return 50

    map_: dict[str, dict] = {}

    def ensure(d: date | None) -> str | None:
        k = key_date(d)
        if k and k not in map_:
            map_[k] = {"date": k}
        return k

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        act_raw = await fetch_sheet(client, SHEETS["activity"])
        slp_raw = await fetch_sheet(client, SHEETS["sleep"])
        vit_raw = await fetch_sheet(client, SHEETS["vitals"])
        bod_raw = await fetch_sheet(client, SHEETS["body"])
        try:
            wth_raw = await fetch_sheet(client, SHEETS["weather"])
        except Exception:
            wth_raw = []

    # Activity sheet
    act_by_day: dict[str, list[dict]] = {}
    for r in act_raw:
        k = key_date(parse_date(get_val(r, "date")))
        if k:
            act_by_day.setdefault(k, []).append(r)

    for k, rows in act_by_day.items():
        if k not in map_:
            map_[k] = {"date": k}
        steps_vals = [to_n(get_val(r, "steps")) for r in rows if to_n(get_val(r, "steps")) is not None]
        if steps_vals:
            map_[k]["steps"] = max(steps_vals)
        cal_vals = [to_n(get_val(r, "calor")) or to_n(get_val(r, "kcal")) for r in rows if to_n(get_val(r, "calor")) or to_n(get_val(r, "kcal"))]
        if cal_vals:
            map_[k]["cal"] = max(cal_vals)
        rows_with_dist = [r for r in rows if to_n(get_val(r, "distance")) is not None]
        rows_with_dist.sort(key=lambda r: src_rank(get_val(r, "source")))
        if rows_with_dist:
            dist_m = to_n(get_val(rows_with_dist[0], "distance"))
            if dist_m:
                map_[k]["dist"] = round(dist_m / 1000, 2)
                map_[k]["distance_m"] = dist_m
        for r in rows:
            ex = (get_val(r, "Exercise") or get_val(r, "exercise") or "").strip()
            if ex:
                map_[k].setdefault("exerciseNames", []).append(ex.lower())

    # Sleep sheet
    for r in slp_raw:
        src = get_val(r, "source")
        if src and "coros" not in src.lower():
            continue
        k = ensure(parse_date(get_val(r, "date")))
        if not k:
            continue
        deep = to_n(get_val(r, "deep")) or 0
        rem = to_n(get_val(r, "rem")) or 0
        light = to_n(get_val(r, "light")) or 0
        if deep + rem + light > 0:
            map_[k]["deep"] = (map_[k].get("deep") or 0) + deep
            map_[k]["rem"] = (map_[k].get("rem") or 0) + rem
            map_[k]["light"] = (map_[k].get("light") or 0) + light
            sleep_h = ((map_[k].get("deep") or 0) + (map_[k].get("rem") or 0) + (map_[k].get("light") or 0)) / 60
            map_[k]["sleepH"] = round(sleep_h, 1)

    # Vitals sheet (Coros source → hrMin/hrMax/hrAvg)
    for r in vit_raw:
        src = get_val(r, "source")
        if src and "coros" not in src.lower():
            continue
        k = ensure(parse_date(get_val(r, "date")))
        if not k:
            continue
        for field, key in [("min", "hrMin"), ("max", "hrMax"), ("avg", "hrAvg")]:
            v = to_n(get_val(r, field))
            if v is not None:
                map_[k][key] = v

    # Vitals sheet (HealthSync → hrv, restingHr)
    for r in vit_raw:
        src = get_val(r, "source")
        if not src or "healthsync" not in src.lower():
            continue
        k = key_date(parse_date(get_val(r, "date")))
        if not k:
            continue
        if k not in map_:
            map_[k] = {"date": k}
        hrv = to_n(get_val(r, "variabil"))
        if hrv is not None:
            map_[k]["hrv"] = hrv
        rhr = to_n(get_val(r, "resting heart rate avg"))
        if rhr is not None:
            map_[k]["restingHr"] = rhr

    # Weather sheet
    for r in wth_raw:
        ds = key_date(parse_date(get_val(r, "date") or get_val(r, "data") or get_val(r, "Date")))
        if not ds:
            continue
        if ds not in map_:
            map_[ds] = {"date": ds}
        rain_val = (get_val(r, "rain") or get_val(r, "opady") or get_val(r, "deszcz") or "").strip().lower()
        map_[ds]["rain"] = rain_val
        rain_mm = to_n(get_val(r, "rain_mm") or get_val(r, "opady_mm") or get_val(r, "precipitation"))
        if rain_mm is not None:
            map_[ds]["rainMm"] = rain_mm
        temp = to_n(get_val(r, "temp") or get_val(r, "temperature") or get_val(r, "temperatura"))
        if temp is not None:
            map_[ds]["temp"] = temp
        weather_note = get_val(r, "weather") or get_val(r, "pogoda") or get_val(r, "conditions") or ""
        if weather_note:
            map_[ds]["weatherNote"] = weather_note.strip()

    # Body sheet (weight)
    for r in bod_raw:
        k = ensure(parse_date(get_val(r, "date") or get_val(r, "time")))
        if not k:
            continue
        w = to_n(get_val(r, "weight") or get_val(r, "waga"))
        if w:
            map_[k]["weight"] = w

    # Moon phase for every day
    for ds in list(map_.keys()):
        mp = calc_moon_phase(ds)
        map_[ds].update(mp)

    return map_


# ── Coros API data ────────────────────────────────────────────────────────


async def fetch_raw_daily(auth, start_day: str, end_day: str) -> list[dict]:
    headers = _auth_headers(auth)
    base = _base_url(auth.region)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            base + ENDPOINTS["analyse_detail"],
            params={"startDay": start_day, "endDay": end_day},
            headers=headers,
        )
    resp.raise_for_status()
    body = resp.json()
    if body.get("result") != "0000":
        raise ValueError(f"analyse_detail error: {body.get('message', '?')}")
    return body.get("data", {}).get("dayList", [])


def calc_sleep_score(total_h: float | None, deep_min: float | None, rem_min: float | None) -> int | None:
    if total_h is None:
        return None
    dur = min(1, total_h / 7.5) * 40
    deep = min(1, (deep_min or 0) / 75) * 30
    rem = min(1, (rem_min or 0) / 90) * 30
    return round(dur + deep + rem)


# ── main ──────────────────────────────────────────────────────────────────


async def main():
    print("Pobieranie danych z Google Sheets…")
    gs_map = await load_google_sheets()
    print(f"  Google Sheets: {len(gs_map)} dni")

    # Coros API – try cache first to avoid daily re-login
    auth = get_stored_auth()
    if not auth:
        auth = _load_auth_cache(AUTH_CACHE_PATH)
        if auth:
            print("  auth z cache (pomijam login)")

    if not auth:
        email = os.environ.get("COROS_EMAIL")
        password = os.environ.get("COROS_PASSWORD")
        region = os.environ.get("COROS_REGION", "eu")
        if email and password:
            print("Logowanie do Coros…")
            auth = await login(email, password, region, skip_mobile=False)
            _save_auth_cache(auth, AUTH_CACHE_PATH)
        else:
            print("Brak autoryzacji Coros. Użyj: coros-mcp auth")
            print("Dane z Google Sheets zostaną użyte bez wzbogacenia Coros.")
            auth = None

    if auth:
        end = date.today()
        start = end - timedelta(days=120)
        sd, ed = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        print(f"Pobieranie danych z Coros API: {sd} -> {ed}…")

        # Debug log file
        debug_log: list[str] = []

        def dbg(msg: str) -> None:
            print(msg)
            debug_log.append(msg)

        # Ensure mobile token is available before parallel calls
        if not await _ensure_mobile_token(auth):
            dbg("  WARN: cannot acquire mobile token, steps/HR/RHR will be missing")
        mobile_base = MOBILE_BASE_URLS.get(auth.region, MOBILE_BASE_URLS["eu"])

        _login_lock = asyncio.Lock()
        _login_done = False

        async def _mobile_login_full() -> bool:
            """Full mobile login using env credentials. Only one coroutine logs in at a time."""
            nonlocal _login_done
            async with _login_lock:
                if _login_done:
                    return True  # already logged in by another coroutine
                from coros_api import _mobile_login, get_env_credentials
                creds = get_env_credentials()
                if not creds:
                    return False
                email, password, region = creds
                try:
                    dbg("  performing full mobile login…")
                    token, payload = await _mobile_login(email, password, region)
                    auth.mobile_access_token = token
                    auth.mobile_login_payload = payload
                    _save_auth_cache(auth, AUTH_CACHE_PATH)
                    _login_done = True
                    return True
                except Exception as exc:
                    dbg(f"    mobile full login failed: {exc}")
                    return False

        async def fetch_mobile_dt(dt: int, retry: bool = True) -> list[dict]:
            url = mobile_base + ENDPOINTS["sleep"]
            payload = {
                "allDeviceSleep": 0,
                "dataType": [dt],
                "dataVersion": 0,
                "startTime": int(sd),
                "endTime": int(ed),
                "statisticType": 1,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    params={"accessToken": auth.mobile_access_token},
                    json=payload,
                    headers={"Content-Type": "application/json", "accesstoken": auth.mobile_access_token},
                )
                body = None
                if resp.status_code == 200:
                    body = resp.json()
                    if body.get("result") != "0000":
                        msg = body.get("message", "?")
                        dbg(f"  mobile dt={dt} error: {msg}")
                        if retry and "invalid" in msg.lower():
                            dbg(f"  mobile dt={dt} – token invalid, doing full login…")
                            if await _mobile_login_full():
                                return await fetch_mobile_dt(dt, retry=False)
                            dbg(f"  mobile dt={dt}: full login failed, skipping")
                        return []
                elif resp.status_code in (401, 403) and retry:
                    dbg(f"  mobile dt={dt} status {resp.status_code} – doing full login…")
                    if await _mobile_login_full():
                        return await fetch_mobile_dt(dt, retry=False)
                    dbg(f"  mobile dt={dt}: full login failed, skipping")
                    return []
                elif resp.status_code != 200:
                    dbg(f"  WARN: mobile dt={dt} status {resp.status_code}")
                    return []
                data_node = body.get("data")
                if data_node is None:
                    daylist = []
                else:
                    stat_data = data_node.get("statisticData") if isinstance(data_node, dict) else None
                    daylist = stat_data.get("dayDataList", []) if isinstance(stat_data, dict) else []
                if daylist:
                    sample_keys = list(daylist[-1].keys())
                    dbg(f"  mobile dt={dt} ({MOBILE_DATA_TYPES[dt]}): {len(daylist)} dni, last item keys={sample_keys}")
                    if dt in (3, 4):
                        dbg(f"    last item: {json.dumps(daylist[-1], ensure_ascii=False)}")
                else:
                    dbg(f"  mobile dt={dt} ({MOBILE_DATA_TYPES[dt]}): 0 dni – raw body dump:")
                    safe_body = {k: v for k, v in body.items() if k != "data"}
                    dbg(f"    top keys: {list(body.keys())}")
                    dbg(f"    data type: {type(data_node).__name__} value: {json.dumps(data_node, ensure_ascii=False)[:2000] if data_node is not None else 'null'}")
                    for key_path in [["data", "dayDataList"], ["data", "statisticData"], ["data", "list"], ["dayDataList"]]:
                        cursor = body
                        found = True
                        for k in key_path:
                            if isinstance(cursor, dict) and k in cursor:
                                cursor = cursor[k]
                            else:
                                found = False
                                break
                        if found and isinstance(cursor, list):
                            dbg(f"    found list at {' > '.join(key_path)}: {len(cursor)} items")
                return daylist

        mobile_results = await asyncio.gather(*[fetch_mobile_dt(dt) for dt in MOBILE_DATA_TYPES])
        mobile_by_day: dict[str, dict] = {}
        for dt, daylist in zip(MOBILE_DATA_TYPES.keys(), mobile_results):
            key = MOBILE_DATA_TYPES[dt]
            for item in daylist:
                hd = item.get("happenDay")
                if not hd:
                    continue
                d = str(hd)
                ds = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                mobile_by_day.setdefault(ds, {})[key] = item
        dbg(f"  mobile daily: {len(mobile_by_day)} dni (e.g. {list(mobile_by_day.keys())[:3]}...)")
        if mobile_by_day:
            sample_dates = sorted(mobile_by_day.keys())[-3:]
            for ds in sample_dates:
                debug_sample = mobile_by_day[ds]
                dbg(f"    DEBUG mobile[{ds}]: dataTypes={list(debug_sample.keys())}")
                for dt_key, item in debug_sample.items():
                    top_keys = list(item.keys())[:10]
                    dbg(f"      {dt_key} keys={top_keys}")
                    if dt_key == "step":
                        dbg(f"        step={item.get('step')}, total={item.get('total')}")
                    elif dt_key == "heartRateData":
                        inner = item.get("heartRateData", {})
                        if isinstance(inner, dict):
                            dbg(f"        inner keys={list(inner.keys())[:6]}")
                            dbg(f"        avg={inner.get('avgHeartRate')}, max={inner.get('maxHeartRate')}, min={inner.get('minHeartRate')}")
                        else:
                            dbg(f"        inner type={type(inner).__name__} value={inner}")
                    elif dt_key == "rhr":
                        dbg(f"        rhr={item.get('rhr')}")
                    elif dt_key == "calorie":
                        dbg(f"        calorie={item.get('calorie')}")

        raw_days, sleep_recs, hrv_recs, act_result = await asyncio.gather(
            fetch_raw_daily(auth, sd, ed),
            fetch_sleep(auth, sd, ed),
            fetch_hrv(auth),
            fetch_activities(auth, sd, ed, size=200),
        )
        activities, total = act_result
        print(f"  dayList: {len(raw_days)}d, sleep: {len(sleep_recs)}d, hrv: {len(hrv_recs)}d, activities: {total}")

        # Merge Coros data into Google Sheets map
        for item in raw_days:
            d = str(item.get("happenDay") or "")
            if len(d) < 8:
                continue
            ds = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            if ds not in gs_map:
                gs_map[ds] = {"date": ds}
            entry = gs_map[ds]
            if entry.get("hrv") is None and item.get("avgSleepHrv") is not None:
                entry["hrv"] = item.get("avgSleepHrv")
            if entry.get("restingHr") is None and item.get("rhr") is not None:
                entry["restingHr"] = item.get("rhr")
            # Training & recovery metrics
            for k in ("trainingLoad", "trainingLoadRatio", "vo2max", "lthr", "performance", "tiredRateNew"):
                v = item.get(k)
                if v is not None:
                    entry[k] = v
            for k in ("ati", "cti", "tib", "sleepHrvBase"):
                v = item.get(k)
                if v is not None:
                    entry[k] = v

        # Merge mobile daily data (steps, cals, HR, RHR)
        for ds, m in mobile_by_day.items():
            if ds not in gs_map:
                gs_map[ds] = {"date": ds}
            e = gs_map[ds]
            # Steps (dataType 3 → field "step")
            step_item = m.get("step", {})
            step_val = step_item.get("step") or step_item.get("total") or step_item.get("steps")
            if e.get("steps") is None and step_val is not None:
                e["steps"] = step_val
            # Calories (dataType 1 → field "calorie")
            cal_item = m.get("calorie", {})
            cal_val = cal_item.get("calorie") or cal_item.get("total") or cal_item.get("calories")
            if e.get("cal") is None and cal_val is not None:
                e["cal"] = cal_val
            # HR (dataType 4 → field "heartRateData")
            hr_item = m.get("heartRateData", {})
            hr_inner = hr_item.get("heartRateData") or hr_item
            if e.get("hrAvg") is None and hr_inner.get("avgHeartRate") is not None:
                e["hrAvg"] = hr_inner["avgHeartRate"]
            if e.get("hrMax") is None and hr_inner.get("maxHeartRate") is not None:
                e["hrMax"] = hr_inner["maxHeartRate"]
            if e.get("hrMin") is None and hr_inner.get("minHeartRate") is not None:
                e["hrMin"] = hr_inner["minHeartRate"]
            # RHR (dataType 6 → field "rhr")
            rhr_item = m.get("rhr", {})
            rhr_val = rhr_item.get("rhr") or rhr_item.get("restingHr") or rhr_item.get("value")
            if e.get("restingHr") is None and rhr_val is not None:
                e["restingHr"] = rhr_val

        print(f"  mobile merged. sample: ds=2026-07-09 -> {gs_map.get('2026-07-09', {}).get('steps')}, {gs_map.get('2026-07-09', {}).get('hrAvg')}")

        # Sleep data (Coros sleep is more detailed than Google Sheets)
        for rec in sleep_recs:
            ds = norm_date(rec.date)
            if ds not in gs_map:
                gs_map[ds] = {"date": ds}
            entry = gs_map[ds]
            if rec.phases:
                entry["deep"] = rec.phases.deep_minutes
                entry["rem"] = rec.phases.rem_minutes
                entry["light"] = rec.phases.light_minutes
                entry["awake"] = rec.phases.awake_minutes
            if rec.total_duration_minutes:
                entry["sleepH"] = round(rec.total_duration_minutes / 60, 1)
            if rec.min_hr is not None:
                entry["hrMin"] = rec.min_hr
            # Calculate sleep score from phases
            entry["sleepScore"] = calc_sleep_score(entry.get("sleepH"), entry.get("deep"), entry.get("rem"))

        # HRV supplement
        for rec in hrv_recs:
            ds = norm_date(rec.date)
            if ds in gs_map and rec.avg_sleep_hrv is not None:
                if gs_map[ds].get("hrv") is None:
                    gs_map[ds]["hrv"] = rec.avg_sleep_hrv

        # Activities → distance/calories if not in Sheets, plus exercise names
        for act in activities:
            if not act.start_time:
                continue
            try:
                ts = int(act.start_time)
                dt = datetime.fromtimestamp(ts)
                ds = dt.strftime("%Y-%m-%d")
            except (ValueError, OSError):
                continue
            if ds not in gs_map:
                gs_map[ds] = {"date": ds}
            entry = gs_map[ds]
            sport = (act.sport_name or "").lower()
            if sport:
                entry.setdefault("exerciseNames", [])
                if sport not in entry["exerciseNames"]:
                    entry["exerciseNames"].append(sport)
            if act.distance_meters:
                existing = entry.get("distance_m") or 0
                entry["distance_m"] = existing + act.distance_meters
                entry["dist"] = round(entry["distance_m"] / 1000, 2)
            if act.calories is not None:
                existing = entry.get("cal") or 0
                entry["cal"] = existing + act.calories

        # Update cache with refreshed mobile token
        if AUTH_CACHE_PATH:
            _save_auth_cache(auth, AUTH_CACHE_PATH)
    else:
        print("Pomijam Coros API (brak autoryzacji).")

    # 5. Build output
    output = []
    for d in sorted(gs_map.keys()):
        entry = gs_map[d]
        # Normalize exercise names
        enames = entry.get("exerciseNames")
        if isinstance(enames, list):
            entry["exerciseNames"] = list(set(enames))
        elif enames is None:
            entry["exerciseNames"] = []
        # Ensure default fields exist
        for field in ("steps", "cal", "dist", "hrAvg", "hrMin", "hrMax",
                       "deep", "rem", "light", "awake", "sleepH", "sleepScore",
                       "hrv", "restingHr", "weight"):
            if field not in entry:
                entry[field] = None
        # Calculate sleepScore if missing but we have sleepH/deep/rem
        if entry.get("sleepScore") is None and entry.get("sleepH") is not None:
            entry["sleepScore"] = calc_sleep_score(entry["sleepH"], entry.get("deep"), entry.get("rem"))
        # Fix distance_m if only dist exists
        if entry.get("dist") and not entry.get("distance_m"):
            entry["distance_m"] = entry["dist"] * 1000
        output.append(entry)

    out_path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "_syncedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": output,
        }, f, ensure_ascii=False, indent=2)

    # Stats
    stats = {}
    for d in output:
        for k, v in d.items():
            if k in ("date", "exerciseNames", "distance_m"):
                continue
            if v is not None and v != 0 and (not isinstance(v, (int, float)) or v != 0):
                stats[k] = stats.get(k, 0) + 1
    debug_path = os.path.join(os.path.dirname(__file__), "debug.log")
    with open(debug_path, "w", encoding="utf-8") as f:
        f.write("\n".join(debug_log) + "\n")
    print(f"\nOK - written {len(output)} dni -> {out_path}")
    print("Pokrycie:")
    for k in sorted(stats.keys()):
        print(f"  {k}: {stats[k]}/{len(output)}")


if __name__ == "__main__":
    asyncio.run(main())
