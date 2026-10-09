from __future__ import annotations

import json
import urllib.request
from pathlib import Path

LAT = 35.745
LON = -81.684
UA = "MorgantonElectricOperationalReadiness/1.0 (tmckee@morgantonnc.gov)"

def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/geo+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

points = fetch_json(f"https://api.weather.gov/points/{LAT},{LON}")
grid_url = points["properties"]["forecastGridData"]
grid = fetch_json(grid_url)
props = grid.get("properties", {})

wanted = [
    "updateTime",
    "validTimes",
    "temperature",
    "maxTemperature",
    "minTemperature",
    "windGust",
    "quantitativePrecipitation",
    "snowfallAmount",
    "iceAccumulation",
    "probabilityOfPrecipitation",
]
out = {
    "points_grid_url": grid_url,
    "grid_id": points["properties"].get("gridId"),
    "grid_x": points["properties"].get("gridX"),
    "grid_y": points["properties"].get("gridY"),
    "properties": {k: props.get(k) for k in wanted},
}
p = Path("validation_outputs/morg-nws-grid-probe.json")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
print(json.dumps(out, indent=2))
