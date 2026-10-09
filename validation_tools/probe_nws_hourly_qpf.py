from __future__ import annotations

import datetime as dt
import json
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

LAT = 35.745
LON = -81.684
URL = (
    "https://forecast.weather.gov/MapClick.php"
    f"?lat={LAT}&lon={LON}&FcstType=digitalDWML"
)
OUT = Path("validation_outputs/morg-qpf-probe.json")

req = urllib.request.Request(
    URL,
    headers={"User-Agent": "MorgantonElectricOperationalReadiness/1.0"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read()

root = ET.fromstring(raw)

creation = root.find(".//creation-date")
creation_time = creation.text if creation is not None else None

layouts: dict[str, list[dict[str, str | None]]] = {}
for layout in root.findall(".//time-layout"):
    key_el = layout.find("layout-key")
    if key_el is None or not key_el.text:
        continue
    starts = layout.findall("start-valid-time")
    ends = layout.findall("end-valid-time")
    rows = []
    for i, start in enumerate(starts):
        rows.append(
            {
                "start": start.text,
                "end": ends[i].text if i < len(ends) else None,
            }
        )
    layouts[key_el.text] = rows

qpf = root.find(".//hourly-qpf")
if qpf is None:
    raise SystemExit("hourly-qpf element not found")

layout_key = qpf.attrib.get("time-layout")
units = qpf.attrib.get("units")
times = layouts.get(layout_key or "", [])
values = qpf.findall("value")

entries = []
for i, value_el in enumerate(values):
    nil = value_el.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil")
    value = None if nil == "true" or value_el.text is None else float(value_el.text)
    row = times[i] if i < len(times) else {"start": None, "end": None}
    entries.append(
        {
            "start": row["start"],
            "end": row["end"],
            "qpf": value,
        }
    )

def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value)

durations = []
for row in entries:
    s = parse_iso(row["start"])
    e = parse_iso(row["end"])
    if s is not None and e is not None:
        durations.append((e - s).total_seconds() / 3600.0)

payload = {
    "source_url": URL,
    "retrieved_at": dt.datetime.now(dt.UTC).isoformat(),
    "source_creation_time": creation_time,
    "latitude": LAT,
    "longitude": LON,
    "hourly_qpf_units": units,
    "time_layout_key": layout_key,
    "entry_count": len(entries),
    "all_intervals_one_hour": bool(durations) and all(abs(x - 1.0) < 1e-9 for x in durations),
    "unique_interval_hours": sorted(set(round(x, 6) for x in durations)),
    "entries": entries,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, indent=2))
