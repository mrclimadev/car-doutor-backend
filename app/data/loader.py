"""
Carregamento de dados geoespaciais.
Lê de local (desenvolvimento) ou S3 (Lambda produção).
Controlado por DATA_SOURCE=local|s3 no ambiente.
"""

import os
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
from shapely.geometry import shape, box
from shapely.validation import make_valid

# Caminho local dos parquets gerados pelo data-pipeline
_LOCAL_VECTORS = (
    Path(__file__).parent.parent.parent.parent
    / "data-pipeline" / "processed" / "vectors"
)

_S3_BUCKET = os.getenv("S3_BUCKET", "car-doutor-geodata")
_S3_PREFIX = os.getenv("S3_PREFIX", "bases/mt/vectors")
_DATA_SOURCE = os.getenv("DATA_SOURCE", "local")  # "local" | "s3"


def _parquet_path(layer: str) -> str:
    if _DATA_SOURCE == "s3":
        return f"s3://{_S3_BUCKET}/{_S3_PREFIX}/{layer}.parquet"
    return str(_LOCAL_VECTORS / f"{layer}.parquet")


@lru_cache(maxsize=16)
def _load_full(layer: str) -> gpd.GeoDataFrame:
    """Carrega camada completa com cache em memória."""
    path = _parquet_path(layer)
    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def load_by_bbox(layer: str, bbox: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """
    Retorna geometrias que intersectam o bounding box.
    bbox: (min_lon, min_lat, max_lon, max_lat)
    """
    gdf = _load_full(layer)
    return gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]].copy()


def load_by_polygon(layer: str, polygon) -> gpd.GeoDataFrame:
    """
    Retorna geometrias que intersectam o polígono informado.
    polygon: shapely geometry
    """
    bbox = polygon.bounds  # (minx, miny, maxx, maxy)
    candidates = load_by_bbox(layer, bbox)
    if candidates.empty:
        return candidates
    # Sanitize invalid geometries from SICAR before spatial predicate
    candidates = candidates.copy()
    invalid = ~candidates.geometry.is_valid
    if invalid.any():
        candidates.loc[invalid, "geometry"] = candidates.loc[invalid, "geometry"].apply(make_valid)
    mask = candidates.geometry.intersects(polygon)
    return candidates[mask].copy()


def find_property_by_car(car_code: str) -> gpd.GeoDataFrame | None:
    """Busca imóvel pelo código CAR no SICAR."""
    gdf = _load_full("sicar_area_imovel")

    # tentar coluna cod_imovel ou similar
    code_cols = [c for c in gdf.columns if "cod" in c.lower() or "car" in c.lower()]
    for col in code_cols:
        match = gdf[gdf[col].astype(str).str.upper() == car_code.upper()]
        if not match.empty:
            return match

    return None


def geometry_from_geojson(geojson: dict):
    """Converte dict GeoJSON para shapely geometry."""
    return shape(geojson)


def available_layers() -> list[str]:
    """Lista camadas disponíveis localmente."""
    if _DATA_SOURCE == "local":
        return [p.stem for p in _LOCAL_VECTORS.glob("*.parquet")]
    return []
