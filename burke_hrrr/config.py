from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float

    def validate(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("invalid west/east bounds")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("invalid south/north bounds")


# Operational envelope around Burke County. Values are intentionally a little
# wider than the county so storms approaching from the Blue Ridge are retained.
BURKE_BOUNDS = Bounds(west=-82.10, south=35.50, east=-81.30, north=36.05)

# The current HRRR 2D NOMADS filter exposes these pressure levels. The V2
# extractor intentionally uses the supported 2D fields rather than downloading
# the several-hundred-MB full-CONUS pressure file.
PRESSURE_LEVELS_HPA = (1000, 925, 850, 700, 500, 300, 250)

# Standard/direct HRRR severe-weather fields that can be returned together.
# MAXUVV, MAXDVV, and MXUPHL are requested separately because some ecCodes
# builds identify these HRRR local-table parameters as "unknown". A dedicated
# request lets the extractor retain an unambiguous semantic label regardless of
# the local ecCodes parameter table installed on the runner.
SURFACE_VARIABLES = (
    "CAPE",
    "CIN",
    "PWAT",
    "REFC",
    "RETOP",
    "VIL",
    "MAXREF",
    "GUST",
    "WIND",
    "TMP",
    "DPT",
    "UGRD",
    "VGRD",
    "HGT",
    "HLCY",
    "VUCSH",
    "VVCSH",
    "LTNG",
)

# Dewpoint is used instead of pressure-level RH because the HRRR 2D file
# reliably supplies DPT at the lower pressure levels needed for the 700-mb
# dryness diagnostic.
PRESSURE_VARIABLES = ("TMP", "DPT", "HGT", "UGRD", "VGRD")

# Selecting multiple levels in NOMADS can return legitimate extra variable-
# level combinations. The decoder and operational selector filter those groups
# after download.
SURFACE_LEVELS = (
    "surface",
    "2 m above ground",
    "10 m above ground",
    "90-0 mb above ground",
    "180-0 mb above ground",
    "255-0 mb above ground",
    "entire atmosphere",
    "entire atmosphere (considered as a single layer)",
    "cloud top",
    "1000 m above ground",
    "3000-0 m above ground",
    "1000-0 m above ground",
    "0-1000 m above ground",
    "0-6000 m above ground",
)

# Small, dedicated requests for local-table storm diagnostics whose shortName
# may be unavailable in a particular ecCodes build.
SPECIAL_PRODUCTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "max_downdraft": ("MAXDVV", ("100-1000 mb above ground",)),
    "max_updraft": ("MAXUVV", ("100-1000 mb above ground",)),
    "updraft_helicity": ("MXUPHL", ("5000-2000 m above ground",)),
}
