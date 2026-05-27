"""Utilitários de geometria compartilhados entre checkers."""

import geopandas as gpd
from shapely.validation import make_valid

_CRS_METRIC = "EPSG:31981"


def to_metric(geom):
    """Reprojet shapely geometry para EPSG:31981 e corrige topologia."""
    series = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(_CRS_METRIC)
    g = series.iloc[0]
    return g if g.is_valid else make_valid(g)


def gdf_to_metric(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reprojet GeoDataFrame para EPSG:31981 e corrige geometrias inválidas."""
    result = gdf.to_crs(_CRS_METRIC).copy()
    invalid = ~result.geometry.is_valid
    if invalid.any():
        result.loc[invalid, "geometry"] = result.loc[invalid, "geometry"].apply(make_valid)
    return result
