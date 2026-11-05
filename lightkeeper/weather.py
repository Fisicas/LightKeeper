"""Public weather for a recorded run, written beside it as weather_vNN.json. Context for comparing days, not an explanation of
single events: the airport is several km away and the model grid cannot resolve sunflecks.

Runs after recording, from the window or from  LightKeeper.py --fetch-weather <run folder> ...  and is never called by the
engine or the recorder, so a network failure cannot touch an acquisition. It reads run_metadata.json and never opens the HDF5
files. Each source succeeds or fails on its own. A new fetch adds a new file; it does not overwrite an earlier one.

What leaves this computer: the site rounded to two decimals (about 1 km, finer than any of the sources) and the dates. It goes
to the US National Weather Service (to find the nearest station), the Iowa Environmental Mesonet (station id and dates only)
and Open-Meteo. Coordinates are stored only in the run's own files.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import VERSION

ATTRIBUTION = {
    "open_meteo": "Weather data by Open-Meteo.com, CC BY 4.0, https://open-meteo.com/",
    "open_meteo_air_quality": "Air quality data by Open-Meteo.com (CAMS), CC BY 4.0, https://open-meteo.com/",
    "airport_observations": "ASOS observations via Iowa Environmental Mesonet, Iowa State University, https://mesonet.agron.iastate.edu/",
}
HOURLY = ("temperature_2m,relative_humidity_2m,dew_point_2m,surface_pressure,precipitation,cloud_cover,cloud_cover_low,"
          "cloud_cover_mid,cloud_cover_high,visibility,wind_speed_10m,shortwave_radiation,direct_radiation,diffuse_radiation")
MINUTELY_15 = "shortwave_radiation,direct_radiation,diffuse_radiation"
ASOS_FIELDS = ("tmpf", "dwpf", "relh", "sknt", "mslp", "vsby", "skyc1", "skyl1", "skyc2", "skyl2", "skyc3", "skyl3", "wxcodes")
TIMEOUT_S = 40
BUSY_WAIT_S = 15        # a public service asks us to slow down (HTTP 429): wait this long, up to BUSY_TRIES times in all
BUSY_TRIES = 3


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"LightKeeper/{VERSION}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read()


def _asos_rows(text: str) -> list[dict]:
    import csv
    import io
    return [{k: v for k, v in row.items() if v not in ("", None)} for row in csv.DictReader(io.StringIO(text))]


def next_path(run_dir: Path) -> Path:
    nums = [int(m.group(1)) for f in run_dir.glob("weather_v*.json") if (m := re.fullmatch(r"weather_v(\d+)\.json", f.name))]
    return run_dir / f"weather_v{max(nums, default=0) + 1:02d}.json"


def run_window(meta: dict) -> tuple[dt.datetime, dt.datetime]:
    """(start, end) of the recording in UTC, from the run's metadata."""
    plan = meta.get("plan") or {}
    start = meta.get("planned_start_local") or plan.get("resolved_start_local")
    end = meta.get("recording_end_local") or meta.get("planned_stop_local") or plan.get("resolved_stop_local")
    if not start or not end:
        raise ValueError("run_metadata.json has no start and end times")
    return tuple(dt.datetime.fromisoformat(t).astimezone(dt.timezone.utc) for t in (start, end))


def fetch_for_run(run_dir, get=_get) -> Path:
    """Fetch every source for one run and write the next weather_vNN.json. Raises ValueError when the run has no usable site or
    times, and RuntimeError when no source answered (nothing is written then)."""
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    plan = meta.get("plan") or {}
    lat, lon = plan.get("site_lat"), plan.get("site_lon")
    if lat is None or lon is None or (float(lat), float(lon)) == (0.0, 0.0):
        raise ValueError(f"{run_dir.name}: no site in its metadata")
    lat, lon = round(float(lat), 2), round(float(lon), 2)
    t0, t1 = run_window(meta)
    d0, d1 = t0.date().isoformat(), t1.date().isoformat()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sources: dict[str, dict] = {}

    def source(name, url, parse):
        for attempt in range(BUSY_TRIES):
            try:
                sources[name] = dict(ok=True, url=url, data=parse(get(url)))
                return
            except urllib.error.HTTPError as e:
                sources[name] = dict(ok=False, url=url, error=repr(e))
                if e.code != 429 or attempt == BUSY_TRIES - 1:
                    return
                time.sleep(BUSY_WAIT_S)
            except Exception as e:
                sources[name] = dict(ok=False, url=url, error=repr(e))
                return

    source("open_meteo", "https://historical-forecast-api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(dict(
        latitude=lat, longitude=lon, start_date=d0, end_date=d1, timezone="UTC", hourly=HOURLY, minutely_15=MINUTELY_15), safe=","), json.loads)
    source("open_meteo_air_quality", "https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode(dict(
        latitude=lat, longitude=lon, start_date=d0, end_date=d1, timezone="UTC", hourly="aerosol_optical_depth")), json.loads)

    def station(raw):
        stations = json.loads(get(json.loads(raw)["properties"]["observationStations"]))["features"]
        first = next(s for s in stations if re.fullmatch(r"K[A-Z0-9]{3}", s["properties"]["stationIdentifier"]))
        return dict(id=first["properties"]["stationIdentifier"], name=first["properties"].get("name", ""),
                    lon_lat=first["geometry"]["coordinates"])
    source("nearest_station", f"https://api.weather.gov/points/{lat},{lon}", station)
    if sources["nearest_station"]["ok"]:
        st = sources["nearest_station"]["data"]["id"][1:]        # the Mesonet names K stations without the K
        end = t1 + dt.timedelta(hours=1)
        q = [("station", st)] + [("data", f) for f in ASOS_FIELDS] + [
            ("year1", t0.year), ("month1", t0.month), ("day1", t0.day), ("hour1", t0.hour),
            ("year2", end.year), ("month2", end.month), ("day2", end.day), ("hour2", end.hour),
            ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"), ("missing", "empty"), ("trace", "empty")]
        source("airport_observations", "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?" + urllib.parse.urlencode(q),
               lambda raw: _asos_rows(raw.decode("utf-8")))
    if not any(s["ok"] for k, s in sources.items() if k != "nearest_station"):
        raise RuntimeError(f"{run_dir.name}: no weather source answered: " + "; ".join(f"{k}: {s.get('error')}" for k, s in sources.items()))
    out = dict(app=f"LightKeeper {VERSION}", run=run_dir.name, retrieved_utc=now, recording_start_utc=t0.isoformat(timespec="seconds"),
               recording_end_utc=t1.isoformat(timespec="seconds"), position_sent=dict(lat=lat, lon=lon),
               times="UTC", attribution=ATTRIBUTION, caution="Context for a day, not an explanation of single events: an airport "
               "several km away and a model grid do not see this sensor's local sky, canopy or sunflecks.", sources=sources)
    path = next_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(out, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def missing_runs(root, limit: int = 3) -> list[Path]:
    """The newest closed runs in a folder that have no weather file yet (newest first, at most limit of the newest runs)."""
    runs = []
    try:
        for d in Path(root).iterdir():
            if d.is_dir() and (d / "run_metadata.json").exists():
                runs.append(d)
    except OSError:
        return []
    runs.sort(key=lambda d: d.name, reverse=True)
    out = []
    for d in runs[:limit]:
        try:
            closed = json.loads((d / "run_state.json").read_text(encoding="utf-8")).get("status") == "closed"
        except Exception:
            closed = False
        if closed and not any(d.glob("weather_v*.json")):
            out.append(d)
    return out


def main(argv) -> int:
    """LightKeeper.py --fetch-weather <run folder> [<run folder> ...]"""
    if not argv:
        print("usage: LightKeeper.py --fetch-weather <run folder> [<run folder> ...]")
        return 2
    code = 0
    for d in argv:
        try:
            p = fetch_for_run(d)
            s = json.loads(p.read_text(encoding="utf-8"))["sources"]
            print(f"{p}: " + ", ".join(f"{k} {'ok' if v['ok'] else 'FAILED'}" for k, v in s.items()))
        except Exception as e:
            print(f"{d}: {e}")
            code = 1
    return code
