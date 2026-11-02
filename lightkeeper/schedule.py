"""Run plan, integration-time ladder, schedule events and planning estimates (shown before Start)."""
from __future__ import annotations

import dataclasses as dc
import datetime as dt
import math
import os
import shutil
from pathlib import Path

import numpy as np

from .storage import LOCAL_TZ

SETUP_FIELDS = ("fiber_core_um", "diffuser_id", "diffuser_thickness_mm", "fiber_diffuser_mm", "view_az_deg", "view_el_deg",
                "window_state", "site_alt_m", "setup_photo")
DEFAULT_LADDER_MS = [8, 10, 15, 20, 30, 50, 80, 100, 150, 200, 300, 500, 800, 1000, 1500, 2000, 3000]



def _env_site():
    try:
        lat, lon = (float(x) for x in os.environ["LIGHTKEEPER_SITE"].split(","))
        return lat, lon
    except (KeyError, ValueError):
        return 0.0, 0.0


DEFAULT_OUT_DIR = os.environ.get("LIGHTKEEPER_DATA_ROOT") or str(Path.home() / "Documents" / "LightKeeper Runs")
DEFAULT_SITE = _env_site()   # "lat,lon" in LIGHTKEEPER_SITE; the window remembers the last site used


@dc.dataclass
class RunPlan:
    label: str = "run"
    notes: str = ""
    out_dir: str = DEFAULT_OUT_DIR
    start_now: bool = True
    start_local: str = ""            # ISO local time when start_now is False and start_event is ""
    stop_local: str = ""             # ISO local time (used when stop_event is "")
    start_event: str = ""            # sun event name (SUN_EVENTS) -> start at its next occurrence + start_offset_min
    start_offset_min: float = 0.0
    stop_event: str = ""             # sun event name -> stop at that event on the start day (or the next one) + stop_offset_min
    stop_offset_min: float = 0.0
    slot_s: float = 1.0
    auto_exposure: bool = True
    int_ms: float = 500.0            # initial (auto) or fixed integration time
    min_int_ms: float = 500.0
    max_int_ms: float = 3000.0
    ladder_ms: list = dc.field(default_factory=lambda: list(DEFAULT_LADDER_MS))
    edc: bool = True
    nlc: bool = True
    store_sd: bool = True
    burst_every_min: float = 60.0    # 0 = no bursts
    burst_offset_min: float = 30.0
    burst_s: float = 120.0
    burst_max_int_ms: float = 50.0
    dark_s_per_int: float = 5.0
    pre_darks: bool = True
    post_darks: bool = True
    rotate_min: int = 60
    flush_s: float = 30.0
    site_lat: float = DEFAULT_SITE[0]
    site_lon: float = DEFAULT_SITE[1]
    geometry: str = ""               # free text: azimuth/elevation, diffuser, window
    # 0.3.1 structured setup metadata; None / "" = not recorded
    fiber_core_um: float | None = None
    diffuser_id: str = ""
    diffuser_thickness_mm: float | None = None
    fiber_diffuser_mm: float | None = None
    view_az_deg: float | None = None
    view_el_deg: float | None = None
    window_state: str = ""           # open / closed / screen
    site_alt_m: float | None = None
    setup_photo: str = ""            # path; copied into the run folder at Start
    auto_resume_logon: bool = True   # 0.2.0: if Windows restarts during the run, resume it at the next logon
    resolved_start_local: str = ""   # 0.2.0: start/stop exactly as confirmed in the window (sun events resolved once, there)
    resolved_stop_local: str = ""

    def to_dict(self):
        return dc.asdict(self)

    @classmethod
    def from_dict(cls, d):
        f = {x.name for x in dc.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in f})

    def start_dt(self) -> dt.datetime:
        now = dt.datetime.now(LOCAL_TZ)
        if self.start_now:
            return now
        if self.resolved_start_local:
            return _aware(self.resolved_start_local)
        if self.start_event:
            return next_sun_event(self.start_event, now - dt.timedelta(minutes=self.start_offset_min), self.site_lat, self.site_lon) \
                + dt.timedelta(minutes=self.start_offset_min)
        return _aware(self.start_local)

    def stop_dt(self) -> dt.datetime:
        if self.resolved_stop_local:
            return _aware(self.resolved_stop_local)
        if self.stop_event:
            t0 = self.start_dt()
            return next_sun_event(self.stop_event, t0 - dt.timedelta(minutes=self.stop_offset_min), self.site_lat, self.site_lon) \
                + dt.timedelta(minutes=self.stop_offset_min)
        return _aware(self.stop_local)


def _aware(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=LOCAL_TZ)


def ladder_us(plan: RunPlan, dev_min_us: int, dev_max_us: int) -> list[int]:
    lo = max(dev_min_us, int(plan.min_int_ms * 1000)); hi = min(dev_max_us, int(plan.max_int_ms * 1000))
    vals = sorted({int(round(x * 1000)) for x in plan.ladder_ms if lo <= x * 1000 <= hi})
    if not plan.auto_exposure:
        vals = sorted(set(vals) | {int(round(plan.int_ms * 1000))})
    return vals or [lo]


def dark_scan_count(int_us: int, dark_s: float) -> int:
    return int(min(30, max(5, round(dark_s * 1e6 / int_us))))


def dark_session_seconds(ladder: list[int], dark_s: float, discard: int = 2) -> float:
    return sum((dark_scan_count(u, dark_s) + discard) * u / 1e6 for u in ladder)


def burst_times(plan: RunPlan, t0: dt.datetime, t1: dt.datetime) -> list[dt.datetime]:
    if plan.burst_every_min <= 0:
        return []
    out = []
    t = t0.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    k = 0
    while True:
        b = t + dt.timedelta(minutes=plan.burst_offset_min + k * plan.burst_every_min)
        if b >= t1:
            return out
        if b > t0:
            out.append(b)
        k += 1


# ---------------------------------------------------------------- solar position (NOAA / Meeus low precision; checked
# against astropy to <0.01 deg in test dates in Oct 2026)
def solar_elevation(t_utc: np.ndarray, lat: float, lon: float) -> np.ndarray:
    jd = np.asarray(t_utc, dtype="datetime64[ns]").astype("int64") / 86400e9 + 2440587.5
    jc = (jd - 2451545.0) / 36525
    L0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    M = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    C = (np.sin(np.radians(M)) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + np.sin(np.radians(2 * M)) * (0.019993 - 0.000101 * jc)
         + np.sin(np.radians(3 * M)) * 0.000289)
    om = 125.04 - 1934.136 * jc
    lam = L0 + C - 0.00569 - 0.00478 * np.sin(np.radians(om))
    eps = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60 + 0.00256 * np.cos(np.radians(om))
    dec = np.degrees(np.arcsin(np.sin(np.radians(eps)) * np.sin(np.radians(lam))))
    y = np.tan(np.radians(eps / 2)) ** 2
    eot = 4 * np.degrees(y * np.sin(2 * np.radians(L0)) - 2 * e * np.sin(np.radians(M)) + 4 * e * y * np.sin(np.radians(M)) * np.cos(2 * np.radians(L0))
                         - 0.5 * y * y * np.sin(4 * np.radians(L0)) - 1.25 * e * e * np.sin(2 * np.radians(M)))
    tod = ((jd + 0.5) % 1) * 1440
    ha = ((tod + eot + 4 * lon) % 1440) / 4 - 180
    cz = np.sin(np.radians(lat)) * np.sin(np.radians(dec)) + np.cos(np.radians(lat)) * np.cos(np.radians(dec)) * np.cos(np.radians(ha))
    return 90 - np.degrees(np.arccos(np.clip(cz, -1, 1)))


# event name -> (solar elevation of the geometric centre, rising?)  Sunrise/sunset use -0.833 deg (refraction + semidiameter)
SUN_EVENTS = {
    "astronomical dawn (-18°)": (-18.0, True), "nautical dawn (-12°)": (-12.0, True), "civil dawn (-6°)": (-6.0, True),
    "sunrise": (-0.833, True), "sunset": (-0.833, False), "civil dusk (-6°)": (-6.0, False),
    "nautical dusk (-12°)": (-12.0, False), "astronomical dusk (-18°)": (-18.0, False),
}


def sun_events(day_local: dt.date, lat: float, lon: float) -> dict:
    """All SUN_EVENTS plus 'solar noon' for one local calendar day; crossing times interpolated on a 1-min grid (~1 s)."""
    t0 = dt.datetime.combine(day_local, dt.time(0, 0), tzinfo=LOCAL_TZ)
    tl = [t0 + dt.timedelta(minutes=i) for i in range(24 * 60 + 1)]
    tu = np.array([t.astimezone(dt.timezone.utc).replace(tzinfo=None) for t in tl], dtype="datetime64[ns]")
    el = solar_elevation(tu, lat, lon)
    out = {"solar noon": tl[int(np.argmax(el))]}
    for name, (h, rising) in SUN_EVENTS.items():
        d = el - h
        idx = np.where((d[:-1] < 0) & (d[1:] >= 0))[0] if rising else np.where((d[:-1] >= 0) & (d[1:] < 0))[0]
        if len(idx):
            i = idx[0]; f = d[i] / (d[i] - d[i + 1])
            out[name] = tl[i] + dt.timedelta(minutes=float(f))
        else:
            out[name] = None
    return out


def next_sun_event(name: str, after: dt.datetime, lat: float, lon: float) -> dt.datetime:
    for k in range(0, 3):
        t = sun_events((after + dt.timedelta(days=k)).date(), lat, lon).get(name)
        if t is not None and t > after:
            return t
    raise ValueError(f"no {name} within 3 days")


def solar_day(day_local: dt.date, lat: float, lon: float):
    """(times local, elevation deg, sunrise, noon, sunset) on a 1-min grid; rise/set at -0.833 deg."""
    t0 = dt.datetime.combine(day_local, dt.time(0, 0), tzinfo=LOCAL_TZ)
    tl = [t0 + dt.timedelta(minutes=i) for i in range(24 * 60 + 1)]
    tu = np.array([t.astimezone(dt.timezone.utc).replace(tzinfo=None) for t in tl], dtype="datetime64[ns]")
    el = solar_elevation(tu, lat, lon)
    up = np.where(el > -0.833)[0]
    rise = tl[up[0]] if len(up) else None; sset = tl[up[-1]] if len(up) else None
    return tl, el, rise, tl[int(np.argmax(el))], sset


def estimates(plan: RunPlan, n_pixels: int, dev_min_us: int, dev_max_us: int,
              t0: dt.datetime | None = None, t1: dt.datetime | None = None) -> dict:
    """Planning estimates. Pass t0/t1 for a run that is already armed: its times were fixed when Start was pressed and must
    not be re-resolved (a sun-event plan re-resolved after today's event returns the next day's event)."""
    t0 = plan.start_dt() if t0 is None else t0
    t1 = plan.stop_dt() if t1 is None else t1
    dur = max(0.0, t1.timestamp() - t0.timestamp())   # real elapsed time; same-zone subtraction ignores a DST change
    lad = ladder_us(plan, dev_min_us, dev_max_us)
    bursts = burst_times(plan, t0, t1)
    b_int = max(dev_min_us, min(lad[-1], int(plan.burst_max_int_ms * 1000)))
    b_int = max([u for u in lad if u <= b_int] or [lad[0]])
    per_spec = n_pixels * 4
    slots = dur / plan.slot_s
    slot_bytes = slots * per_spec * (2 if plan.store_sd else 1)
    burst_scans = len(bursts) * plan.burst_s * 1e6 / b_int
    dark_scans = sum(dark_scan_count(u, plan.dark_s_per_int) for u in lad) * (int(plan.pre_darks) + int(plan.post_darks))
    total = slot_bytes + (burst_scans + dark_scans) * per_spec
    try:
        free = shutil.disk_usage(Path(plan.out_dir) if Path(plan.out_dir).exists() else Path(plan.out_dir).anchor).free
    except Exception:
        free = float("nan")
    return dict(start=t0, stop=t1, duration_h=dur / 3600, slots=int(slots), ladder_ms=[u / 1000 for u in lad],
                bursts=bursts, burst_int_ms=b_int / 1000, burst_scans=int(burst_scans),
                dark_session_s=dark_session_seconds(lad, plan.dark_s_per_int), dark_scans_total=int(dark_scans),
                data_gb_uncompressed=total / 1e9, disk_free_gb=free / 1e9)
