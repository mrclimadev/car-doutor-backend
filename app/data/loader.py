"""
Carregamento de dados geoespaciais por estado (UF).
Lê de local (desenvolvimento) ou S3 (produção).
Controlado por DATA_SOURCE=local|s3 no ambiente.

Estrutura local:  data-pipeline/processed/<uf>/vectors/<layer>.parquet
Estrutura S3:     s3://<bucket>/bases/<uf>/vectors/<layer>.parquet
"""

import os
import re
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
from shapely.geometry import shape
from shapely.validation import make_valid

_LOCAL_BASE   = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data-pipeline" / "processed"
)
_S3_BUCKET    = os.getenv("S3_BUCKET", "car-doutor-geodata")
_DATA_SOURCE  = os.getenv("DATA_SOURCE", "local")   # "local" | "s3"

# Mapeamento prefixo CAR → UF
_CAR_PREFIX_TO_UF: dict[str, str] = {
    "AC": "AC", "AL": "AL", "AM": "AM", "AP": "AP", "BA": "BA",
    "CE": "CE", "DF": "DF", "ES": "ES", "GO": "GO", "MA": "MA",
    "MG": "MG", "MS": "MS", "MT": "MT", "PA": "PA", "PB": "PB",
    "PE": "PE", "PI": "PI", "PR": "PR", "RJ": "RJ", "RN": "RN",
    "RO": "RO", "RR": "RR", "RS": "RS", "SC": "SC", "SE": "SE",
    "SP": "SP", "TO": "TO",
}

# UF padrão quando não há código CAR (análise por geometria desenhada)
_DEFAULT_UF = os.getenv("DEFAULT_UF", "MT")


def uf_from_car_code(car_code: str) -> str:
    """Extrai a UF do código CAR (ex: 'MT-5105150-...' → 'MT')."""
    m = re.match(r"^([A-Z]{2})[^A-Z]", car_code.strip().upper())
    if m and m.group(1) in _CAR_PREFIX_TO_UF:
        return _CAR_PREFIX_TO_UF[m.group(1)]
    return _DEFAULT_UF


def _parquet_path(layer: str, uf: str) -> str:
    uf = uf.lower()
    if _DATA_SOURCE == "s3":
        return f"s3://{_S3_BUCKET}/bases/{uf}/vectors/{layer}.parquet"
    return str(_LOCAL_BASE / uf / "vectors" / f"{layer}.parquet")


@lru_cache(maxsize=32)
def _load_full(layer: str, uf: str) -> gpd.GeoDataFrame:
    """Carrega camada completa com cache por (layer, uf)."""
    path = _parquet_path(layer, uf)
    gdf = gpd.read_parquet(path)
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def load_by_bbox(layer: str, bbox: tuple[float, float, float, float], uf: str) -> gpd.GeoDataFrame:
    """Retorna geometrias que intersectam o bounding box."""
    gdf = _load_full(layer, uf)
    return gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]].copy()


def load_by_polygon(layer: str, polygon, uf: str) -> gpd.GeoDataFrame:
    """Retorna geometrias que intersectam o polígono."""
    bbox = polygon.bounds
    candidates = load_by_bbox(layer, bbox, uf)
    if candidates.empty:
        return candidates
    candidates = candidates.copy()
    invalid = ~candidates.geometry.is_valid
    if invalid.any():
        candidates.loc[invalid, "geometry"] = (
            candidates.loc[invalid, "geometry"].apply(make_valid)
        )
    return candidates[candidates.geometry.intersects(polygon)].copy()


def find_property_by_car(car_code: str) -> gpd.GeoDataFrame | None:
    """Busca imóvel pelo código CAR, inferindo a UF automaticamente."""
    uf  = uf_from_car_code(car_code)
    gdf = _load_full("sicar_area_imovel", uf)
    code_cols = [c for c in gdf.columns if "cod" in c.lower() or "car" in c.lower()]
    for col in code_cols:
        match = gdf[gdf[col].astype(str).str.upper() == car_code.upper()]
        if not match.empty:
            return match
    return None


def geometry_from_geojson(geojson: dict):
    return shape(geojson)


def available_layers(uf: str | None = None) -> list[str]:
    """Lista camadas disponíveis localmente para um estado (ou todos)."""
    if _DATA_SOURCE != "local":
        return []
    if uf:
        d = _LOCAL_BASE / uf.lower() / "vectors"
        return [p.stem for p in d.glob("*.parquet")] if d.exists() else []
    # todos os estados
    layers = []
    for state_dir in _LOCAL_BASE.iterdir():
        vec = state_dir / "vectors"
        if vec.is_dir():
            layers += [f"{state_dir.name}/{p.stem}" for p in vec.glob("*.parquet")]
    return layers
