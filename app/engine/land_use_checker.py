"""
Análise de uso do solo — camadas SICAR adicionais:
  - sicar_reserva_legal          → RL poligonal real vs estimativa por vegetação
  - sicar_area_consolidada       → elegibilidade ao PRA (regularização gradual)
  - sicar_uso_restrito           → várzeas, veredas, manguezais
  - sicar_area_pousio            → área de pousio (potencial recuperação de RL)
  - sicar_servidao_administrativa → faixas de servidão
"""
import logging

from ..data.loader import load_by_polygon
from ..models import LandUseResult, Pendencia, StatusCode
from .geom_utils import gdf_to_metric, to_metric

log = logging.getLogger(__name__)
_CRS_METRIC = "EPSG:31981"


def _intersect_ha(gdf, prop_metric) -> float:
    if gdf.empty:
        return 0.0
    try:
        metric = gdf_to_metric(gdf)
        clipped = metric.geometry.intersection(prop_metric)
        return clipped.area.sum() / 10_000
    except Exception as exc:
        log.warning("Erro ao calcular interseção: %s", exc)
        return 0.0


def _own_filter(gdf, car_code: str | None):
    """Filtra pelos registros do próprio imóvel quando cod_imovel disponível."""
    if not car_code or gdf.empty:
        return gdf
    if "cod_imovel" in gdf.columns:
        own = gdf[gdf["cod_imovel"].astype(str).str.upper() == car_code.upper()]
        return own if not own.empty else gdf
    return gdf


def _distinct_types(gdf, *cols) -> list[str]:
    tipos: set[str] = set()
    for col in cols:
        if col in gdf.columns:
            tipos.update(gdf[col].dropna().astype(str).str.strip().unique())
    return sorted(t for t in tipos if t and t.lower() not in {"nan", "none", ""})


def _safe_load(layer: str, geom):
    try:
        return load_by_polygon(layer, geom)
    except Exception as exc:
        log.warning("Camada %s indisponível: %s", layer, exc)
        import geopandas as gpd
        return gpd.GeoDataFrame()


def check_land_use(
    property_geom,
    property_area_ha: float,
    declared_rl_ha: float = 0.0,
    app_deficit_ha: float = 0.0,
    car_code: str | None = None,
) -> LandUseResult:
    pendencias: list[Pendencia] = []
    prop_metric = to_metric(property_geom)

    # ── 1. Reserva Legal poligonal ─────────────────────────────────────────────
    rl_gdf = _safe_load("sicar_reserva_legal", property_geom)
    rl_gdf = _own_filter(rl_gdf, car_code)
    rl_polygon_ha = _intersect_ha(rl_gdf, prop_metric)
    rl_polygon_encontrado = rl_polygon_ha > 0.1

    if rl_polygon_encontrado and declared_rl_ha > 0.1:
        discrepancia = abs(rl_polygon_ha - declared_rl_ha)
        if discrepancia > 1.0 and discrepancia / max(declared_rl_ha, rl_polygon_ha) > 0.10:
            pendencias.append(Pendencia(
                codigo="RL_DISCREPANCIA_POLIGONO",
                status=StatusCode.ATENCAO,
                titulo="Discrepância entre polígono RL e vegetação declarada",
                detalhe=(
                    f"Polígono RL cadastrado: {rl_polygon_ha:.1f} ha | "
                    f"Vegetação nativa estimada: {declared_rl_ha:.1f} ha | "
                    f"Diferença: {discrepancia:.1f} ha"
                ),
                orientacao=(
                    f"O polígono de Reserva Legal cadastrado ({rl_polygon_ha:.1f} ha) "
                    f"difere da vegetação nativa encontrada ({declared_rl_ha:.1f} ha). "
                    "Verifique se o georreferenciamento do polígono de RL está correto no SICAR."
                ),
                area_ha=discrepancia,
            ))

    # ── 2. Área Consolidada → elegibilidade ao PRA ────────────────────────────
    consol_gdf = _safe_load("sicar_area_consolidada", property_geom)
    consol_gdf = _own_filter(consol_gdf, car_code)
    area_consolidada_ha = _intersect_ha(consol_gdf, prop_metric)

    pra_elegivel_ha = (
        min(app_deficit_ha, area_consolidada_ha)
        if app_deficit_ha > 0 and area_consolidada_ha > 0
        else 0.0
    )
    if pra_elegivel_ha > 0.5:
        pendencias.append(Pendencia(
            codigo="PRA_ELEGIVEL",
            status=StatusCode.ATENCAO,
            titulo="APP em Área Consolidada — elegível ao PRA",
            detalhe=(
                f"Área consolidada no imóvel: {area_consolidada_ha:.1f} ha | "
                f"Déficit de APP elegível ao PRA: ~{pra_elegivel_ha:.1f} ha. "
                "O PRA permite recuperação gradual de APPs em áreas consolidadas até 2030."
            ),
            orientacao=(
                f"Sua propriedade tem {area_consolidada_ha:.1f} ha de área consolidada "
                "(uso agropecuário anterior a 22/07/2008). "
                f"O déficit de APP nessa área (~{pra_elegivel_ha:.1f} ha) pode ser regularizado "
                "pelo Programa de Regularização Ambiental (PRA) com cronograma gradual. "
                "Consulte a SEMA-MT para aderir ao PRA."
            ),
            area_ha=pra_elegivel_ha,
        ))

    # ── 3. Uso Restrito — várzeas, veredas, manguezais ───────────────────────
    ur_gdf = _safe_load("sicar_uso_restrito", property_geom)
    ur_gdf = _own_filter(ur_gdf, car_code)
    uso_restrito_ha = _intersect_ha(ur_gdf, prop_metric)
    uso_restrito_tipos = _distinct_types(
        ur_gdf, "des_tipo", "tipo", "tipo_uso", "class_uso", "classifica", "tipo_area"
    )

    if uso_restrito_ha > 0.5:
        tipo_str = f" ({', '.join(uso_restrito_tipos)})" if uso_restrito_tipos else ""
        pendencias.append(Pendencia(
            codigo="USO_RESTRITO",
            status=StatusCode.ATENCAO,
            titulo=f"Área de Uso Restrito{tipo_str} — art. 9° do Código Florestal",
            detalhe=(
                f"Uso restrito identificado: {uso_restrito_ha:.1f} ha. "
                "Admite uso agropecuário apenas com técnicas de baixo impacto e condicionantes."
            ),
            orientacao=(
                f"Sua propriedade tem {uso_restrito_ha:.1f} ha de área de uso restrito "
                "(várzeas, veredas, pantanais ou outras fisionomias do art. 9°). "
                "Atividades agropecuárias são permitidas com baixo impacto, "
                "mantendo as características ecológicas. Consulte a SEMA-MT."
            ),
            area_ha=uso_restrito_ha,
        ))

    # ── 4. Área de Pousio ─────────────────────────────────────────────────────
    pousio_gdf = _safe_load("sicar_area_pousio", property_geom)
    pousio_gdf = _own_filter(pousio_gdf, car_code)
    area_pousio_ha = _intersect_ha(pousio_gdf, prop_metric)

    if area_pousio_ha > 0.5:
        pendencias.append(Pendencia(
            codigo="AREA_POUSIO",
            status=StatusCode.ATENCAO,
            titulo="Área de Pousio declarada",
            detalhe=(
                f"Área de pousio: {area_pousio_ha:.1f} ha. "
                "Pousio é o descanso temporário entre cultivos. "
                "Pode ser computado como RL em recuperação mediante adesão ao PRA."
            ),
            orientacao=(
                f"Foram identificados {area_pousio_ha:.1f} ha de área de pousio. "
                "Se sua propriedade aderiu ao Programa de Regularização Ambiental (PRA), "
                "essa área pode ser contabilizada no cronograma de recuperação da Reserva Legal."
            ),
            area_ha=area_pousio_ha,
        ))

    # ── 5. Servidão Administrativa ─────────────────────────────────────────────
    serv_gdf = _safe_load("sicar_servidao_administrativa", property_geom)
    serv_gdf = _own_filter(serv_gdf, car_code)
    servidao_ha = _intersect_ha(serv_gdf, prop_metric)
    servidao_tipos = _distinct_types(
        serv_gdf, "des_tipo", "tipo", "tipo_serv", "classifica", "tipo_area"
    )

    if servidao_ha > 0.1:
        tipo_str = f" ({', '.join(servidao_tipos)})" if servidao_tipos else ""
        pendencias.append(Pendencia(
            codigo="SERVIDAO_ADMINISTRATIVA",
            status=StatusCode.ATENCAO,
            titulo=f"Servidão Administrativa{tipo_str}",
            detalhe=(
                f"Faixa de servidão: {servidao_ha:.1f} ha. "
                "Pode incluir linhas de energia, dutos, estradas ou outros empreendimentos."
            ),
            orientacao=(
                f"Há {servidao_ha:.1f} ha de servidão administrativa registrada. "
                "Esta área tem restrições de uso impostas pelo órgão gestor da infraestrutura. "
                "Verifique as condicionantes junto ao empreendedor responsável."
            ),
            area_ha=servidao_ha,
        ))

    # ── Status geral ───────────────────────────────────────────────────────────
    if any(p.status == StatusCode.CRITICO for p in pendencias):
        status = StatusCode.CRITICO
    elif pendencias:
        status = StatusCode.ATENCAO
    else:
        status = StatusCode.OK

    return LandUseResult(
        rl_polygon_ha=round(rl_polygon_ha, 2),
        rl_polygon_encontrado=rl_polygon_encontrado,
        area_consolidada_ha=round(area_consolidada_ha, 2),
        uso_restrito_ha=round(uso_restrito_ha, 2),
        uso_restrito_tipos=uso_restrito_tipos,
        area_pousio_ha=round(area_pousio_ha, 2),
        servidao_ha=round(servidao_ha, 2),
        servidao_tipos=servidao_tipos,
        pra_elegivel_ha=round(pra_elegivel_ha, 2),
        status=status,
        pendencias=pendencias,
    )
