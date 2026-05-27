"""
Verificação de restrições legais — Terras Indígenas e Unidades de Conservação
Consulta via WFS on-the-fly (FUNAI + MMA/ICMBio).
"""

import logging

import geopandas as gpd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from ..models import Pendencia, RestricaoResult, StatusCode
from .geom_utils import gdf_to_metric, to_metric

log = logging.getLogger(__name__)

_TIMEOUT = 15
_UA = "Mozilla/5.0 (compatible; CAR-Doutor/1.0)"

_FUNAI_WFS = "https://geoserver.funai.gov.br/geoserver/Funai/wfs"
_FUNAI_PARAMS = {
    "service": "WFS",
    "version": "1.0.0",
    "request": "GetFeature",
    "typeName": "Funai:tis_poligonais",
    "outputFormat": "application/json",
    "maxFeatures": "500",
}

_TERRABRASILIS_WFS = "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-amazon-nb/wfs"
_UC_PARAMS = {
    "service": "WFS",
    "version": "2.0.0",
    "request": "GetFeature",
    "typeNames": "prodes-amazon-nb:conservation_units_amazon_biome",
    "outputFormat": "application/json",
    "count": "500",
}

_CRS_METRIC = "EPSG:31981"


def _bbox_str(geom) -> str:
    b = geom.bounds
    return f"{b[0]},{b[1]},{b[2]},{b[3]}"


def _fetch_wfs(base_url: str, params: dict, geom, source_name: str) -> gpd.GeoDataFrame:
    p = dict(params)
    p["bbox"] = f"{_bbox_str(geom)},EPSG:4326"
    try:
        resp = requests.get(
            base_url,
            params=p,
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
            verify=False,
        )
        resp.raise_for_status()
        gdf = gpd.read_file(resp.text)
        if gdf.empty:
            return gdf
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        mask = gdf.geometry.intersects(geom)
        return gdf[mask].copy()
    except requests.Timeout:
        log.warning("%s WFS timeout", source_name)
        return gpd.GeoDataFrame()
    except Exception as exc:
        log.warning("%s WFS erro: %s", source_name, exc)
        return gpd.GeoDataFrame()


def _intersection_ha(gdf: gpd.GeoDataFrame, property_geom) -> float:
    if gdf.empty:
        return 0.0
    metric      = gdf_to_metric(gdf)
    prop_metric = to_metric(property_geom)
    clipped = metric.geometry.intersection(prop_metric)
    return clipped.area.sum() / 10_000


def check_restrictions(property_geom) -> RestricaoResult:
    pendencias: list[Pendencia] = []

    ti = _fetch_wfs(_FUNAI_WFS, _FUNAI_PARAMS, property_geom, "FUNAI-TI")
    uc = _fetch_wfs(_TERRABRASILIS_WFS, _UC_PARAMS, property_geom, "TerraBrasilis-UC")

    ti_ha = _intersection_ha(ti, property_geom)
    uc_ha = _intersection_ha(uc, property_geom)

    if ti_ha > 0.1:
        ti_names = ti["terrai_nom"].dropna().tolist() if "terrai_nom" in ti.columns else []
        pendencias.append(Pendencia(
            codigo="SOBREPOSICAO_TI",
            status=StatusCode.CRITICO,
            titulo="Sobreposição com Terra Indígena",
            detalhe=f"Área sobreposta: {ti_ha:.1f} ha. TIs: {ti_names}",
            orientacao=f"Parte da sua propriedade ({ti_ha:.1f} hectares) está registrada como Terra Indígena homologada pela FUNAI. Isso exige regularização imediata com assessoria jurídica.",
            area_ha=ti_ha,
        ))

    if uc_ha > 0.1:
        uc_names = uc["nome_uc"].dropna().tolist() if "nome_uc" in uc.columns else []
        pendencias.append(Pendencia(
            codigo="SOBREPOSICAO_UC",
            status=StatusCode.CRITICO,
            titulo="Sobreposição com Unidade de Conservação",
            detalhe=f"Área sobreposta: {uc_ha:.1f} ha. UCs: {uc_names}",
            orientacao=f"Parte da sua propriedade ({uc_ha:.1f} hectares) sobrepõe uma Unidade de Conservação. Atividades agropecuárias nessa área podem ser proibidas.",
            area_ha=uc_ha,
        ))

    if not pendencias:
        status = StatusCode.OK
    else:
        status = StatusCode.CRITICO

    return RestricaoResult(
        sobreposicao_ti=ti_ha > 0.1,
        sobreposicao_uc=uc_ha > 0.1,
        area_ti_ha=round(ti_ha, 2),
        area_uc_ha=round(uc_ha, 2),
        status=status,
        pendencias=pendencias,
    )
