"""
ERA5 wind field: download (CDS API) and fast interpolation.

Mac-lab only — this module is never imported by the Pi cron (keeps cdsapi /
xarray off the Pi). It provides:

  * download_day(day, ...)  -> one netcdf/day of u,v on pressure levels over the
    EU-South box (ERA5T, ~5-day latency; expver 0005). Cached in data/era5/.
  * WindField(nc_paths)     -> vectorised (u,v) lookup at (t, lat, lon, alt_ft).

Altitude handling: ADS-B baro altitude IS pressure altitude (referenced to
1013.25 hPa), so mapping it back through the ISA to a pressure level is exact —
we do not need actual station pressure. alt_ft -> hPa via the standard atmosphere.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# pressure levels from the surface to above airliner cruise (~45 kft).
LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150]
# EU-South box, ERA5 area order is [North, West, South, East]
AREA = [52, -10, 35, 25]
ERA5_DIR = Path(os.environ.get("ADSB_ROOT",
                "/Users/fmavellia/adsb-co2-lab/adsb-co2")) / "data/era5"


def alt_ft_to_hpa(alt_ft):
    """Pressure altitude (ft) -> pressure (hPa) via the ISA. Vectorised."""
    h = np.asarray(alt_ft, dtype=float) * 0.3048  # m
    p = np.where(
        h < 11000.0,
        1013.25 * (1.0 - 2.25577e-5 * h) ** 5.25588,
        226.32 * np.exp(-(h - 11000.0) / 6341.62),
    )
    return p


def download_day(day_iso: str, levels=LEVELS, area=AREA, force=False) -> Path:
    """Download one day of hourly u,v on pressure levels. Returns the netcdf path."""
    ERA5_DIR.mkdir(parents=True, exist_ok=True)
    out = ERA5_DIR / f"{day_iso}.nc"
    if out.exists() and not force:
        return out
    import cdsapi
    y, m, d = day_iso.split("-")
    c = cdsapi.Client(quiet=True)
    tmp = out.with_suffix(".nc.part")
    c.retrieve("reanalysis-era5-pressure-levels", {
        "product_type": "reanalysis",
        "variable": ["u_component_of_wind", "v_component_of_wind"],
        "pressure_level": [str(x) for x in levels],
        "year": y, "month": m, "day": d,
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": area, "grid": [0.25, 0.25],
        "data_format": "netcdf",
    }, str(tmp))
    tmp.replace(out)
    return out


class WindField:
    """
    Vectorised u,v interpolation over (time, pressure, lat, lon).

    Built from one or more daily netcdfs. Uses a 4-D linear interpolation on the
    regular grid (scipy RegularGridInterpolator), extrapolating at the edges
    rather than erroring (aircraft occasionally sit just above the top level).
    """

    def __init__(self, nc_paths):
        import xarray as xr
        from scipy.interpolate import RegularGridInterpolator

        ds = xr.open_mfdataset(sorted(map(str, nc_paths)), combine="by_coords")
        ds = ds.sortby("valid_time").sortby("latitude").sortby("longitude")
        # levels ascending in hPa
        ds = ds.sortby("pressure_level")

        self.times = ds["valid_time"].values.astype("datetime64[s]").astype(np.int64)
        self.levels = ds["pressure_level"].values.astype(float)
        self.lats = ds["latitude"].values.astype(float)
        self.lons = ds["longitude"].values.astype(float)

        u = ds["u"].transpose("valid_time", "pressure_level", "latitude", "longitude").values
        v = ds["v"].transpose("valid_time", "pressure_level", "latitude", "longitude").values
        grid = (self.times.astype(float), self.levels, self.lats, self.lons)
        self._iu = RegularGridInterpolator(grid, np.asarray(u, dtype=np.float32),
                                           bounds_error=False, fill_value=None)
        self._iv = RegularGridInterpolator(grid, np.asarray(v, dtype=np.float32),
                                           bounds_error=False, fill_value=None)
        ds.close()

    def uv(self, t, lat, lon, alt_ft):
        """Return (u, v) in m/s at the given points (all array-like, same length)."""
        t = np.asarray(t, dtype=float)
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        p = alt_ft_to_hpa(alt_ft)
        # clamp to grid range so edge points extrapolate gently in pressure only
        p = np.clip(p, self.levels.min(), self.levels.max())
        lat = np.clip(lat, self.lats.min(), self.lats.max())
        lon = np.clip(lon, self.lons.min(), self.lons.max())
        pts = np.column_stack([t, p, lat, lon])
        return self._iu(pts), self._iv(pts)
