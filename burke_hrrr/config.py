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

PRESSURE_LEVELS_HPA = (1000, 975, 950, 925, 900, 875, 850, 825, 800, 775,
                       750, 725, 700, 675, 650, 625, 600, 575, 550, 525, 500,
                       475, 450, 425, 400)

SURFACE_VARIABLES = (
    "CAPE", "PWAT", "REFC", "MAXREF", "GUST", "WIND", "TMP", "DPT",
    "UGRD", "VGRD", "HGT",
)

PRESSURE_VARIABLES = ("TMP", "RH", "HGT", "UGRD", "VGRD")

# Selecting multiple levels in NOMADS can return legitimate extra variable-
# level combinations. The decoder filters those groups again after download.
SURFACE_LEVELS = (
    "surface",
    "2 m above ground",
    "10 m above ground",
    "90-0 mb above ground",
    "180-0 mb above ground",
    "entire atmosphere (considered as a single layer)",
    "1000 m above ground",
)

