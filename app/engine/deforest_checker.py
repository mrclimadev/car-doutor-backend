"""
Verificação de desmatamento — PRODES e DETER via WFS (TerraBrasilis/INPE)
Consulta on-the-fly: sem download, sem pré-processamento.
"""

import logging

import geopandas as gpd
import requests
from ..models import DeforestResult, Pendencia, StatusCode
from .geom_utils import gdf_to_metric, to_metric

log = logging.getLogger(__name__)

_TIMEOUT = 15  # segundos

# Endpoints WFS do TerraBrasilis
_PRODES_WFS = (
    "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-amazon-nb/wfs"
    "?service=WFS&version=2.0.0&request=GetFeature"
    "&typeNames=prodes-amazon-nb:yearly_deforestation_biome"
    "&outputFormat=application/json"
    "&bbox={bbox}"
)

_DETER_WFS = (
    "https://terrabrasilis.dpi.inpe.br/geoserver/deter-amz/wfs"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=deter-amz:deter_amz"
    "&outputFormat=application/json"
    "&bbox={bbox}"
)

_CRS_METRIC = "EPSG:31981"


def _bbox_str(geom) -> str:
    b = geom.bounds
    return f"{b[0]},{b[1]},{b[2]},{b[3]},EPSG:4326"


def _fetch_wfs(url_template: str, geom, source_name: str) -> gpd.GeoDataFrame:
    url = url_template.format(bbox=_bbox_str(geom))
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        gdf = gpd.read_file(resp.text)
        if gdf.empty:
            return gdf
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        # filtrar estritamente ao polígono (WFS só filtra pelo bbox)
        mask = gdf.geometry.intersects(geom)
        return gdf[mask].copy()
    except requests.Timeout:
        log.warning("%s WFS timeout — retornando vazio", source_name)
        return gpd.GeoDataFrame()
    except Exception as exc:
        log.warning("%s WFS erro: %s", source_name, exc)
        return gpd.GeoDataFrame()


def _area_ha(gdf: gpd.GeoDataFrame, property_geom) -> float:
    if gdf.empty:
        return 0.0
    metric      = gdf_to_metric(gdf)
    prop_metric = to_metric(property_geom)
    clipped = metric.geometry.intersection(prop_metric)
    return clipped.area.sum() / 10_000


def check_deforestation(property_geom) -> DeforestResult:
    pendencias: list[Pendencia] = []

    prodes = _fetch_wfs(_PRODES_WFS, property_geom, "PRODES")
    deter = _fetch_wfs(_DETER_WFS, property_geom, "DETER")

    prodes_count = len(prodes)
    deter_count = len(deter)
    area_ha = _area_ha(prodes, property_geom) + _area_ha(deter, property_geom)

    if prodes_count > 0:
        anos = sorted(prodes["year"].dropna().unique().tolist()) if "year" in prodes.columns else []
        pendencias.append(Pendencia(
            codigo="DESFLORESTAMENTO_PRODES",
            status=StatusCode.CRITICO,
            titulo="Desmatamento detectado (PRODES/INPE)",
            detalhe=f"{prodes_count} polígono(s) PRODES dentro do imóvel. Anos: {anos}. Área: {_area_ha(prodes, property_geom):.1f} ha",
            orientacao="O sistema do INPE registrou desmatamento dentro da sua propriedade. Isso pode gerar embargo e multa. Procure a SEMA-MT ou um técnico ambiental.",
            area_ha=_area_ha(prodes, property_geom),
        ))

    if deter_count > 0:
        pendencias.append(Pendencia(
            codigo="ALERTA_DETER",
            status=StatusCode.CRITICO,
            titulo="Alerta de desmatamento recente (DETER/INPE)",
            detalhe=f"{deter_count} alerta(s) DETER recentes dentro do imóvel. Área: {_area_ha(deter, property_geom):.1f} ha",
            orientacao="Há alertas recentes de desmatamento na sua propriedade. Caso não reconheça a atividade, pode ser invasão — registre um boletim de ocorrência.",
            area_ha=_area_ha(deter, property_geom),
        ))

    if prodes_count == 0 and deter_count == 0:
        status = StatusCode.OK
    else:
        status = StatusCode.CRITICO

    return DeforestResult(
        alertas_prodes=prodes_count,
        alertas_deter=deter_count,
        area_desmatada_ha=round(area_ha, 2),
        status=status,
        pendencias=pendencias,
    )
