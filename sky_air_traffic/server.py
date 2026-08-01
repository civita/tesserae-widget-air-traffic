"""sky_air_traffic, flights overhead via OpenSky."""

from __future__ import annotations

import contextlib
import json
import math
import time
import urllib.request
from pathlib import Path
from typing import Any

CACHE_TTL_S = 30  # OpenSky updates every ~10s for anon; cache to stay polite
HTTP_TIMEOUT_S = 15
USER_AGENT = "tesserae/0.1 (+sky_air_traffic)"

# OpenSky state-vector tuple positions (0-indexed):
#   0 icao24, 1 callsign, 2 origin_country, 3 time_position, 4 last_contact,
#   5 longitude, 6 latitude, 7 baro_altitude (m), 8 on_ground, 9 velocity (m/s),
#   10 true_track (deg), 11 vertical_rate, 13 geo_altitude, ...
S_CALLSIGN = 1
S_COUNTRY = 2
S_LON = 5
S_LAT = 6
S_BARO_ALT = 7
S_ON_GROUND = 8
S_VELOCITY = 9
S_TRACK = 10
S_VERTICAL = 11
S_GEO_ALT = 13
S_CATEGORY = 17

CAT_MAP = {
    1: "No Info", 2: "Light", 3: "Small", 4: "Large", 5: "High Vortex",
    6: "Heavy", 7: "High Perf", 8: "Rotorcraft", 9: "Glider",
    10: "Lighter-Air", 11: "Skydiver", 12: "Ultralight", 14: "UAV",
    15: "Space", 16: "Emergency", 17: "Service"
}

def _bbox(lat: float, lon: float, mi: float) -> tuple[float, float, float, float]:
    dlat = mi / 69.0 # 69 miles per degree of latitude
    dlon = mi / (69.0 * max(0.1, math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon

def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8 # Radius of Earth in miles
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    lat = float(options.get("latitude") or 0.0)
    lon = float(options.get("longitude") or 0.0)

    radius = max(5.0, float(options.get("radius_mi") or 40))
    max_results = max(1, int(options.get("max_results") or 8))

    # Grab credentials from the widget UI
    client_id = (options.get("client_id") or "").strip()
    client_secret = (options.get("client_secret") or "").strip()

    data_dir = Path(ctx["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache = data_dir / f"at_{lat:.3f}_{lon:.3f}_{int(radius)}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_S:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    la_min, lo_min, la_max, lo_max = _bbox(lat, lon, radius)
    url = (
        "https://opensky-network.org/api/states/all"
        f"?lamin={la_min:.4f}&lomin={lo_min:.4f}&lamax={la_max:.4f}&lomax={lo_max:.4f}&extended=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


        # Authentication Logic
        if client_id and client_secret:
            auth_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
            auth_data = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret
            }).encode("utf-8")
            
            try:
                # Explicitly add the Content-Type header from your curl command
                # Also pass the widget's User-Agent to avoid getting blocked
                auth_req = urllib.request.Request(
                    auth_url, 
                    data=auth_data, 
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": USER_AGENT
                    }
                )
                with urllib.request.urlopen(auth_req, timeout=HTTP_TIMEOUT_S) as auth_resp:
                    auth_payload = json.loads(auth_resp.read().decode("utf-8"))
                    access_token = auth_payload.get("access_token")
                    
                    # If successful, inject the Bearer token into the main request
                    if access_token:
                        req.add_header("Authorization", f"Bearer {access_token}")
                    else:
                        # Catch cases where the request succeeds but returns no token
                        return {"error": "Auth succeeded but no token returned.", "flights": []}

            except urllib.error.HTTPError as auth_err:
                # This will capture specific HTTP errors (like 401 Unauthorized or 400 Bad Request)
                error_body = auth_err.read().decode("utf-8")
                return {"error": f"Auth HTTPError {auth_err.code}: {error_body}", "flights": []}
            except Exception as auth_err:
                # This will catch connectivity or parsing errors, stopping the silent fallback
                return {"error": f"Auth failed: {type(auth_err).__name__}: {auth_err}", "flights": []}


        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))


    except urllib.error.HTTPError as err:
        # Check specifically for the 429 Rate Limit error
        if err.code == 429:
            retry_after = err.headers.get("X-Rate-Limit-Retry-After-Seconds")
            if retry_after:
                return {"error": f"Rate limit exceeded (429). Wait {retry_after} seconds.", "flights": []}
            return {"error": "Rate limit exceeded (429).", "flights": []}
            
        # Handle other HTTP errors (like 500, 404, etc.)
        return {"error": f"API HTTPError {err.code}: {err.reason}", "flights": []}
        
    except Exception as err:
        # Catch network timeouts or JSON parsing errors
        return {"error": f"API Error: {type(err).__name__}: {err}", "flights": []}

    states = payload.get("states") or []
    flights = []
    for s in states:
        try:
            f_lat = s[S_LAT]
            f_lon = s[S_LON]
        except (IndexError, TypeError):
            continue

        is_grounded = bool(s[S_ON_GROUND]) if len(s) > S_ON_GROUND else False
        velocity = s[S_VELOCITY] if len(s) > S_VELOCITY else None

        # Skip if it is on the ground AND not moving (velocity is 0 or None)
        if is_grounded and (velocity is None or velocity < 5):
            continue
        if f_lat is None or f_lon is None:
            continue
        d_mi = _haversine_mi(lat, lon, f_lat, f_lon)
        if d_mi > radius:
            continue
        # Extract the category ID and map it
        cat_id = s[S_CATEGORY] if len(s) > S_CATEGORY and s[S_CATEGORY] is not None else 0
        category_text = CAT_MAP.get(cat_id, "") if cat_id != 0 else ""

        flights.append(
            {
                "callsign": (s[S_CALLSIGN] or "").strip() if len(s) > S_CALLSIGN else "",
                "category_text": category_text,
                "country": (s[S_COUNTRY] or "").strip() if len(s) > S_COUNTRY else "",
                "altitude_ft": s[S_GEO_ALT] * 3.28084 if len(s) > S_GEO_ALT and s[S_GEO_ALT] is not None else None,
                "velocity_mph": velocity * 2.23694 if len(s) > S_VELOCITY and velocity is not None else None,
                "track": s[S_TRACK] if len(s) > S_TRACK else None,
                "vertical_rate": s[S_VERTICAL] if len(s) > S_VERTICAL else None,
                "on_ground": is_grounded,
                "lat": f_lat,
                "lon": f_lon,
                "distance_mi": round(d_mi, 1),
            }
        )
    flights.sort(key=lambda f: f["distance_mi"])
    flights = flights[:max_results]

    result = {
        "lat": lat,
        "lon": lon,
        "radius": radius,
        "count": len(states),  # total in bounding box (incl out-of-radius)
        "shown": len(flights),
        "flights": flights,
    }
    with contextlib.suppress(OSError):
        cache.write_text(json.dumps(result), encoding="utf-8")
    return result
