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
from datetime import date, timedelta
from pathlib import Path

import numpy as np

# pressure levels from the surface to above airliner cruise (~45 kft).
LEVELS = [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150]
VARIABLES = ("u", "v")
GRID = (0.25, 0.25)

# ERA5 area order is [North, West, South, East]. Default = the EU-South box.
# A wider box (e.g. full ECAC) is selected with ERA5_AREA="72,-32,27,45".
_DEFAULT_AREA = [52, -10, 35, 25]
AREA = ([float(x) for x in os.environ["ERA5_AREA"].split(",")]
        if os.environ.get("ERA5_AREA") else _DEFAULT_AREA)

# Cached files are named YYYY-MM-DD.nc with NO box in the name, so two different
# boxes MUST NOT share a directory: the second would silently overwrite the
# first and WindField would then interpolate the wrong geography without any
# error. Override the directory whenever the area is overridden.
# La radice si deriva dalla posizione del file, mai da un percorso assoluto:
# quello esporrebbe la home di chi l'ha scritto e romperebbe su ogni altra
# macchina — ed e' la classe di bug che in questo progetto e' gia' costata caro.
ERA5_DIR = Path(os.environ.get("ERA5_DIR") or
                (Path(os.environ.get("ADSB_ROOT",
                                     str(Path(__file__).resolve().parents[1])))
                 / "data/era5"))


def required_wind_days(days) -> list[str]:
    """Return each flight day plus the following UTC day.

    A baseline departing late in a flight day can be sampled after midnight.
    Loading only ``day.nc`` made the interpolator extrapolate the 23:00 field
    into the following day. The adjacent day is therefore an input, not an
    optional look-ahead cache.
    """
    out = set()
    for day_iso in days:
        day = date.fromisoformat(str(day_iso))
        out.add(day.isoformat())
        out.add((day + timedelta(days=1)).isoformat())
    return sorted(out)


def alt_ft_to_hpa(alt_ft):
    """Pressure altitude (ft) -> pressure (hPa) via the ISA. Vectorised."""
    h = np.asarray(alt_ft, dtype=float) * 0.3048  # m
    p = np.where(
        h < 11000.0,
        1013.25 * (1.0 - 2.25577e-5 * h) ** 5.25588,
        226.32 * np.exp(-(h - 11000.0) / 6341.62),
    )
    return p


class ERA5ValidationError(ValueError):
    """A NetCDF is readable but does not contain the requested ERA5 field."""


def _expected_axis(start: float, stop: float, step: float) -> np.ndarray:
    intervals = (stop - start) / step
    rounded = round(intervals)
    if not np.isclose(intervals, rounded, rtol=0.0, atol=1e-9):
        raise ERA5ValidationError(
            f"ERA5 area boundary {start}..{stop} is not divisible by grid {step}"
        )
    return np.linspace(start, stop, rounded + 1, dtype=float)


def validate_era5_dataset(ds, day_iso: str, *, levels=LEVELS, area=AREA,
                          grid=GRID, variables=VARIABLES) -> None:
    """Require the exact hourly wind cube requested for one UTC day."""
    expected_vars = set(variables)
    actual_vars = set(ds.data_vars)
    if actual_vars != expected_vars:
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 variables {sorted(actual_vars)!r}, expected "
            f"{sorted(expected_vars)!r}"
        )

    required_coords = {"valid_time", "pressure_level", "latitude", "longitude"}
    missing_coords = sorted(required_coords - set(ds.coords))
    if missing_coords:
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 coordinate(s) missing: {', '.join(missing_coords)}"
        )

    start = np.datetime64(day_iso, "h")
    expected_times = start + np.arange(24).astype("timedelta64[h]")
    actual_seconds = ds["valid_time"].values.astype("datetime64[s]")
    expected_seconds = expected_times.astype("datetime64[s]")
    if not np.array_equal(np.sort(actual_seconds), expected_seconds):
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 valid_time must be exactly 00:00..23:00 UTC "
            f"({len(actual_seconds)} timestamp(s) found)"
        )

    actual_levels = ds["pressure_level"].values.astype(float)
    if not np.array_equal(np.sort(actual_levels),
                          np.sort(np.asarray(levels, dtype=float))):
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 pressure levels {actual_levels.tolist()!r}, "
            f"expected {list(levels)!r}"
        )

    north, west, south, east = map(float, area)
    lat_step, lon_step = map(float, grid)
    expected_lats = _expected_axis(north, south, -lat_step)
    expected_lons = _expected_axis(west, east, lon_step)
    actual_lats = ds["latitude"].values.astype(float)
    actual_lons = ds["longitude"].values.astype(float)
    if (actual_lats.shape != expected_lats.shape
            or not np.allclose(np.sort(actual_lats), np.sort(expected_lats),
                               rtol=0.0, atol=1e-9)):
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 latitude grid/area differs from "
            f"{north}..{south} by {lat_step} degrees"
        )
    if (actual_lons.shape != expected_lons.shape
            or not np.allclose(np.sort(actual_lons), np.sort(expected_lons),
                               rtol=0.0, atol=1e-9)):
        raise ERA5ValidationError(
            f"{day_iso}: ERA5 longitude grid/area differs from "
            f"{west}..{east} by {lon_step} degrees"
        )

    expected_dims = ("valid_time", "pressure_level", "latitude", "longitude")
    for variable in variables:
        if set(ds[variable].dims) != set(expected_dims):
            raise ERA5ValidationError(
                f"{day_iso}: ERA5 {variable} dimensions {ds[variable].dims!r}, "
                f"expected {expected_dims!r}"
            )


def validate_era5_file(path: Path, day_iso: str | None = None, **kwargs) -> None:
    """Open and validate one daily NetCDF without loading its wind arrays."""
    import xarray as xr

    path = Path(path)
    expected_day = day_iso or path.stem
    try:
        with xr.open_dataset(str(path)) as ds:
            validate_era5_dataset(ds, expected_day, **kwargs)
    except ERA5ValidationError:
        raise
    except Exception as exc:
        raise ERA5ValidationError(f"{expected_day}: cannot read ERA5 file {path}: {exc}") from exc


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
        "area": area, "grid": list(GRID),
        "data_format": "netcdf",
    }, str(tmp))
    tmp.replace(out)
    return out


class WindField:
    """
    Vectorised u,v interpolation over (time, pressure, lat, lon).

    Built from one or more daily netcdfs. Uses a 4-D linear interpolation on the
    regular grid (scipy RegularGridInterpolator). Pressure and geographic edge
    points are clamped because aircraft can sit just outside the requested box;
    time is never extrapolated and temporal holes are rejected.
    """

    def __init__(self, nc_paths):
        import xarray as xr
        from scipy.interpolate import RegularGridInterpolator

        # open each day and concat on time (no dask / open_mfdataset needed)
        parts = []
        for raw_path in sorted(map(Path, nc_paths)):
            part = xr.open_dataset(str(raw_path))
            try:
                validate_era5_dataset(part, raw_path.stem)
            except Exception:
                part.close()
                for opened in parts:
                    opened.close()
                raise
            parts.append(part)
        if not parts:
            raise ERA5ValidationError("cannot build an ERA5 field from no files")
        ds = xr.concat(parts, dim="valid_time") if len(parts) > 1 else parts[0]
        ds = ds.sortby("valid_time").sortby("latitude").sortby("longitude")
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

    def _require_time_coverage(self, t: np.ndarray) -> None:
        """Reject samples outside the hourly field or inside a temporal hole."""
        if t.size == 0:
            return
        if not np.all(np.isfinite(t)):
            raise ValueError("ERA5 sample time is not finite")

        times = self.times
        pos = np.searchsorted(times, t, side="left")
        inside = pos < len(times)
        exact = np.zeros(t.shape, dtype=bool)
        exact[inside] = times[pos[inside]] == t[inside]
        between = (~exact) & (pos > 0) & (pos < len(times))
        covered = exact.copy()
        covered[between] = (
            times[pos[between]] - times[pos[between] - 1] <= 3600
        )
        if np.all(covered):
            return

        bad = float(t[np.flatnonzero(~covered)[0]])
        stamp = np.datetime64(int(bad), "s")
        lo = np.datetime64(int(times.min()), "s")
        hi = np.datetime64(int(times.max()), "s")
        raise ValueError(
            f"ERA5 sample time {stamp} is outside available hourly coverage "
            f"({lo} to {hi})"
        )

    def uv(self, t, lat, lon, alt_ft):
        """Return (u, v) in m/s at the given points (all array-like, same length)."""
        t = np.asarray(t, dtype=float)
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        p = alt_ft_to_hpa(alt_ft)
        self._require_time_coverage(t)
        # clamp to grid range so edge points extrapolate gently in pressure only
        p = np.clip(p, self.levels.min(), self.levels.max())
        lat = np.clip(lat, self.lats.min(), self.lats.max())
        lon = np.clip(lon, self.lons.min(), self.lons.max())
        pts = np.column_stack([t, p, lat, lon])
        return self._iu(pts), self._iv(pts)
