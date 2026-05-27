"""
Orquestrador — coordena todos os módulos do motor geoespacial.
Chamado pelo endpoint POST /analyze.
"""

import logging

import geopandas as gpd

from ..data.loader import (
    find_property_by_car,
    geometry_from_geojson,
    load_by_polygon,
)
from ..models import AnalyseRequest, CadastroPerfil, LaudoResult, SoloResult, StatusCode
from .app_calculator import calculate_app
from .deforest_checker import check_deforestation
from .geom_utils import gdf_to_metric, to_metric
from .llm import generate_summaries
from .restriction_checker import check_restrictions
from .rl_checker import check_rl
from .soil_checker import analyze_soil

log = logging.getLogger(__name__)

_CRS_METRIC = "EPSG:31981"


def _property_area_ha(geom) -> float:
    series = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(_CRS_METRIC)
    return series.area.iloc[0] / 10_000


def _municipio_from_row(row) -> str | None:
    for col in ["municipio", "nm_municip", "municipio_", "nm_mun"]:
        if col in row.index and row[col]:
            return str(row[col])
    return None


def analyze(request: AnalyseRequest) -> LaudoResult:
    # ── 1. Resolver geometria do imóvel ───────────────────────────────────────
    property_geom = None
    car_code = request.car_code
    municipio = None
    rows = None

    if request.car_code:
        rows = find_property_by_car(request.car_code)
        if rows is not None and not rows.empty:
            row = rows.iloc[0]
            property_geom = row.geometry
            municipio = _municipio_from_row(row)
            log.info("Imóvel encontrado: %s (%.1f ha)", car_code, _property_area_ha(property_geom))
        else:
            log.warning("CAR não encontrado: %s — usando geometria fornecida", car_code)

    if property_geom is None and request.geometry:
        property_geom = geometry_from_geojson(request.geometry)

    if property_geom is None:
        raise ValueError("Forneça car_code válido ou geometry GeoJSON.")

    from shapely.validation import make_valid
    if not property_geom.is_valid:
        log.warning("Geometria inválida para %s — aplicando make_valid()", car_code)
        property_geom = make_valid(property_geom)

    area_ha = _property_area_ha(property_geom)

    # ── 2. Carregar APP declarada ──────────────────────────────────────────────
    apps_gdf = load_by_polygon("sicar_apps", property_geom)
    if not apps_gdf.empty:
        apps_metric = gdf_to_metric(apps_gdf)
        prop_metric  = to_metric(property_geom)
        from shapely.ops import unary_union
        declared_app_geom_metric = unary_union(
            apps_metric.geometry.intersection(prop_metric).values
        )
        declared_app_geom = (
            gpd.GeoSeries([declared_app_geom_metric], crs=_CRS_METRIC)
            .to_crs("EPSG:4326")
            .iloc[0]
        )
    else:
        declared_app_geom = None

    # ── 3. Rodar todos os checks ───────────────────────────────────────────────
    log.info("Calculando APP ...")
    app_result = calculate_app(property_geom, declared_app_geom)

    log.info("Verificando RL ...")
    rl_result = check_rl(property_geom, area_ha, car_code=car_code)

    log.info("Consultando PRODES/DETER ...")
    deforest_result = check_deforestation(property_geom)

    log.info("Consultando TI/UC ...")
    restrict_result = check_restrictions(property_geom)

    log.info("Analisando solo ...")
    solo_raw = analyze_soil(property_geom)
    solo_result = SoloResult(**solo_raw)

    # ── 3b. Perfil cadastral do imóvel ────────────────────────────────────────
    cadastro_result = CadastroPerfil()
    if request.car_code and rows is not None and not rows.empty:
        r0 = rows.iloc[0]
        def _col(c): return str(r0[c]) if c in r0.index and r0[c] is not None else None
        mf = float(r0["mod_fiscal"]) if "mod_fiscal" in r0.index and r0["mod_fiscal"] else None
        if mf is not None:
            if mf < 1:      porte = "Minifúndio"
            elif mf <= 4:   porte = "Pequena propriedade"
            elif mf <= 15:  porte = "Média propriedade"
            else:           porte = "Grande propriedade"
        else:
            porte = None
        cadastro_result = CadastroPerfil(
            ind_status=_col("ind_status"),
            des_condic=_col("des_condic"),
            ind_tipo=_col("ind_tipo"),
            mod_fiscal=mf,
            dat_criacao=_col("dat_criaca"),
            dat_atualizacao=_col("dat_atuali"),
            classificacao_porte=porte,
        )

    # ── 4. Status geral ───────────────────────────────────────────────────────
    all_statuses = [
        app_result.status,
        rl_result.status,
        deforest_result.status,
        restrict_result.status,
    ]
    if StatusCode.CRITICO in all_statuses:
        status_geral = StatusCode.CRITICO
    elif StatusCode.ATENCAO in all_statuses:
        status_geral = StatusCode.ATENCAO
    else:
        status_geral = StatusCode.OK

    all_pendencias = (
        app_result.pendencias
        + rl_result.pendencias
        + deforest_result.pendencias
        + restrict_result.pendencias
    )
    criticas = sum(1 for p in all_pendencias if p.status == StatusCode.CRITICO)

    # ── 5. Montar laudo parcial (sem resumos) para passar ao LLM ─────────────
    from shapely.geometry import mapping
    geojson_geom = mapping(property_geom)

    laudo = LaudoResult(
        car_code=car_code,
        status_geral=status_geral,
        area_imovel_ha=round(area_ha, 2),
        municipio=municipio,
        geometry=geojson_geom,
        app=app_result,
        rl=rl_result,
        desmatamento=deforest_result,
        restricoes=restrict_result,
        solo=solo_result,
        cadastro=cadastro_result,
        resumo_simples="",
        resumo_tecnico="",
        total_pendencias=len(all_pendencias),
        pendencias_criticas=criticas,
    )

    # ── 6. Gerar resumos (LLM ou fallback) ───────────────────────────────────
    log.info("Gerando resumos (use_ai=%s) ...", request.use_ai)
    resumo_simples, resumo_tecnico, gerado_por = generate_summaries(laudo, use_ai=request.use_ai)
    laudo.resumo_simples = resumo_simples
    laudo.resumo_tecnico = resumo_tecnico
    laudo.resumo_gerado_por = gerado_por

    return laudo
