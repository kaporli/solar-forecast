"""Write the forecast lattice of `solar-data local` into config/local.json ("grid").

    uv run tools/make_grid.py NUTS_RG_01M_2021_4326_LEVL_2.geojson

The input is Eurostat GISCO's NUTS 2021 level 2 regions (1:1 million, EPSG:4326), whose eleven Belgian regions are
the ten provinces and Brussels, the regions Elia publishes. A lattice point is kept when it lies inside one of them
and carries that province's Elia name. The spacing is close to one cell of the weather model (ECMWF IFS HRES on
Open-Meteo, about 8 x 11 km over Belgium), so a finer lattice would repeat cells. Brussels is smaller than a cell:
the lattice is shifted so that a point falls inside it, and the run fails if any province has none."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUTS = {"BE10": "Brussel", "BE21": "Antwerpen", "BE22": "Limburg", "BE23": "OostVlaanderen", "BE24": "VlaamsBrabant",
        "BE25": "WestVlaanderen", "BE31": "BrabantWallon", "BE32": "Hainaut", "BE33": "Liege", "BE34": "Luxembourg",
        "BE35": "Namur"}
LAT_STEP, LON_STEP = 0.08, 0.125  # about 8.9 x 8.8 km at 50.5 N
LAT0, LON0 = 50.85 - 17 * LAT_STEP, 4.35 - 15 * LON_STEP  # Brussels (50.85 N, 4.35 E) is a lattice point


def inside(lon: float, lat: float, polygon: list) -> bool:
    """Even-odd rule over the polygon's rings (outer ring and holes), coordinates as (lon, lat)."""
    hit = False
    for ring in polygon:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def main(path: str) -> None:
    features = [f for f in json.loads(Path(path).read_text())["features"] if f["properties"]["NUTS_ID"] in NUTS]
    assert len(features) == len(NUTS), "expected the eleven Belgian NUTS 2 regions"
    shapes = []
    for f in features:
        g = f["geometry"]
        shapes.append((NUTS[f["properties"]["NUTS_ID"]], g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]))
    points = []
    for i in range(27):
        for j in range(33):
            lat, lon = round(LAT0 + i * LAT_STEP, 3), round(LON0 + j * LON_STEP, 3)
            for name, polygons in shapes:
                if any(inside(lon, lat, poly) for poly in polygons):
                    points.append([lat, lon, name])
                    break
    missing = set(NUTS.values()) - {p[2] for p in points}
    assert not missing, f"no lattice point in {sorted(missing)}"
    out = ROOT / "config/local.json"
    cfg = json.loads(out.read_text())
    cfg["grid"] = {"lat_step": LAT_STEP, "lon_step": LON_STEP,
                   "source": "Eurostat GISCO NUTS 2021 level 2, 1:1 million, (c) EuroGeographics for the administrative boundaries",
                   "points": points}
    head = json.dumps({k: v for k, v in cfg.items() if k != "grid"}, indent=1)[:-2]
    grid = {k: v for k, v in cfg["grid"].items() if k != "points"}
    rows = ",\n".join("   " + json.dumps(p) for p in points)
    out.write_text(f'{head},\n "grid": {json.dumps(grid)[:-1]}, "points": [\n{rows}\n  ]}}\n}}\n')
    by = {n: sum(p[2] == n for p in points) for n in NUTS.values()}
    print(len(points), "points;", by)


if __name__ == "__main__":
    main(sys.argv[1])
