import json
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from http.cookiejar import CookieJar
from urllib.request import build_opener, HTTPCookieProcessor, Request
from urllib.error import HTTPError

API_BASE = "https://fioletowe.live/api/v1"
STATE_FILE = "data/state.json"
STATS_FILE = "data/daily_stats.json"
TZ_PL = ZoneInfo("Europe/Warsaw")

cj = CookieJar()
opener = build_opener(HTTPCookieProcessor(cj))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://fioletowe.live/",
    "Origin": "https://fioletowe.live"
}


def init_session():
    req = Request("https://fioletowe.live/", headers=HEADERS)
    with opener.open(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
        print(f"Sesja zainicjowana. Ciasteczka: {[c.name for c in cj]}")
        return html


def fetch_json(url, extra_headers=None):
    hdrs = dict(HEADERS)
    if extra_headers:
        hdrs.update(extra_headers)
    req = Request(url, headers=hdrs)
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_cars_data():
    # 1. Próba standardowa z ciasteczkami sesyjnymi
    try:
        return fetch_json(f"{API_BASE}/cars")
    except HTTPError as e:
        print(f"/api/v1/cars zwróciło {e.code}, sprawdzam specyfikację autoryzacji i /cars/nearby...")

    # 2. Sprawdź schemat autoryzacji w openapi.json
    try:
        spec = fetch_json("https://fioletowe.live/api/openapi.json")
        sec = spec.get("components", {}).get("securitySchemes", {})
        cars_path = spec.get("paths", {}).get("/api/v1/cars", {})
        nearby_path = spec.get("paths", {}).get("/api/v1/cars/nearby", {})
        print(f"SecuritySchemes: {json.dumps(sec)}")
        print(f"Params /cars: {json.dumps(cars_path.get('get', {}).get('parameters', []))}")
        print(f"Params /cars/nearby: {json.dumps(nearby_path.get('get', {}).get('parameters', []))}")
    except Exception as ex:
        print(f"Nie udało się pobrać openapi.json: {ex}")

    # 3. Próba przez /api/v1/cars/nearby dla głównych aglomeracji w Polsce
    cities = [
        ("Warszawa", 52.2297, 21.0122),
        ("Kraków", 50.0647, 19.9450),
        ("Trójmiasto", 54.3722, 18.6383),
        ("Wrocław", 51.1079, 17.0385),
        ("Poznań", 52.4064, 16.9252),
        ("Katowice", 50.2649, 19.0238),
        ("Łódź", 51.7592, 19.4560),
    ]
    all_cars = {}
    for name, lat, lon in cities:
        for query in [
            f"lat={lat}&lon={lon}&radius=35000",
            f"latitude={lat}&longitude={lon}&radius=35000",
            f"lat={lat}&lng={lon}&radius=35000"
        ]:
            try:
                res = fetch_json(f"{API_BASE}/cars/nearby?{query}")
                items = res if isinstance(res, list) else res.get("items", res.get("cars", res.get("data", [])))
                for c in items:
                    cid = str(c.get("id") or c.get("sideNumber") or c.get("plate"))
                    all_cars[cid] = c
                print(f"Pobrano {len(items)} aut dla {name} ({query})")
                break
            except HTTPError:
                continue

    if all_cars:
        return list(all_cars.values())

    raise RuntimeError("Wszystkie metody pobrania aut zwróciły błąd — sprawdź logi powyżej.")


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    now_pl = datetime.now(TZ_PL)
    now_iso = now_pl.isoformat()
    today_str = now_pl.strftime("%Y-%m-%d")

    init_session()
    models_raw = fetch_json(f"{API_BASE}/car-models")
    zones_raw = fetch_json(f"{API_BASE}/zones")
    cars_raw = get_cars_data()

    models_list = models_raw if isinstance(models_raw, list) else models_raw.get("items", models_raw.get("data", []))
    zones_list = zones_raw if isinstance(zones_raw, list) else zones_raw.get("items", zones_raw.get("data", []))
    cars_list = cars_raw if isinstance(cars_raw, list) else cars_raw.get("items", cars_raw.get("cars", cars_raw.get("data", [])))

    models_map = {str(m.get("id")): m.get("name", f"Model {m.get('id')}") for m in models_list}
    zones_map = {str(z.get("id")): z.get("name", f"Strefa {z.get('id')}") for z in zones_list}

    state = load_json(STATE_FILE, {"available": {}, "missing": {}, "seen_by_day": {}})
    stats = load_json(STATS_FILE, {"days": {}, "last_updated": None, "timezone": "Europe/Warsaw"})

    if today_str not in stats["days"]:
        stats["days"][today_str] = {
            "total_orders": 0,
            "by_model": {},
            "by_zone": {},
            "unique_cars_seen": 0
        }

    seen_today = set(state.get("seen_by_day", {}).get(today_str, []))

    current_cars = {}
    for c in cars_list:
        car_id = str(c.get("id") or c.get("sideNumber") or c.get("plate"))
        lat = c.get("lat") or c.get("latitude") or (c.get("location") or {}).get("lat")
        lon = c.get("lon") or c.get("lng") or c.get("longitude") or (c.get("location") or {}).get("lng")
        fuel = c.get("fuelLevel") or c.get("fuel") or c.get("range") or 0
        model_id = str(c.get("modelId") or c.get("carModelId") or "unknown")
        zone_id = str(c.get("zoneId") or "unknown")

        if lat is None or lon is None:
            continue

        current_cars[car_id] = {
            "lat": float(lat),
            "lon": float(lon),
            "fuel": float(fuel),
            "model": models_map.get(model_id, model_id),
            "zone": zones_map.get(zone_id, zone_id),
            "ts": now_iso
        }
        seen_today.add(car_id)

    for car_id, prev in state["available"].items():
        if car_id not in current_cars and car_id not in state["missing"]:
            state["missing"][car_id] = prev

    returned_ids = []
    for car_id, miss_info in state["missing"].items():
        if car_id in current_cars:
            curr = current_cars[car_id]
            dist = haversine_m(miss_info["lat"], miss_info["lon"], curr["lat"], curr["lon"])
            fuel_drop = miss_info["fuel"] - curr["fuel"]
            duration_min = (now_pl - datetime.fromisoformat(miss_info["ts"])).total_seconds() / 60.0

            if duration_min >= 4.5 and (dist > 250 or fuel_drop > 0):
                start_day = miss_info["ts"][:10]
                if start_day not in stats["days"]:
                    stats["days"][start_day] = {
                        "total_orders": 0,
                        "by_model": {},
                        "by_zone": {},
                        "unique_cars_seen": 0
                    }

                day_bucket = stats["days"][start_day]
                day_bucket["total_orders"] += 1
                day_bucket["by_model"][curr["model"]] = day_bucket["by_model"].get(curr["model"], 0) + 1
                day_bucket["by_zone"][curr["zone"]] = day_bucket["by_zone"].get(curr["zone"], 0) + 1

            returned_ids.append(car_id)

    for cid in returned_ids:
        del state["missing"][cid]

    recent_days = sorted(list(state.get("seen_by_day", {}).keys()) + [today_str])[-7:]
    new_seen = {d: state.get("seen_by_day", {}).get(d, []) for d in recent_days}
    new_seen[today_str] = list(seen_today)

    state["available"] = current_cars
    state["seen_by_day"] = new_seen
    stats["days"][today_str]["unique_cars_seen"] = len(seen_today)
    stats["last_updated"] = now_iso

    save_json(STATE_FILE, state)
    save_json(STATS_FILE, stats)
    print(f"[{now_iso}] SUKCES! Wolne auta: {len(current_cars)}, W trasie: {len(state['missing'])}")


if __name__ == "__main__":
    main()
