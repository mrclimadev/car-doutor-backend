"""
Cálculo de APP — Área de Preservação Permanente
Lei 12.651/2012, Art. 4º

Regras implementadas:
  - Cursos d'água: buffer conforme largura do curso (art. 4º, I)
  - Nascentes: 50m de raio (art. 4º, IV)
  - Encostas > 45°: área inteira (não implementado no MVP — requer DEM)

Simplificação do MVP:
  Sem atributo de largura na hidrografia do SICAR → aplica 30m (menor faixa legal).
  O laudo indica que uma medição em campo pode aumentar a exigência.
"""

import math

from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

from ..data.loader import load_by_polygon
from ..models import Pendencia, StatusCode, AppResult
from .geom_utils import gdf_to_metric, to_metric

# Tabela de largura → buffer mínimo (metros)
_LARGURA_BUFFER = [
    (10,  30),
    (50,  50),
    (200, 100),
    (600, 200),
    (math.inf, 500),
]

# Projeção métrica para MT (SIRGAS 2000 / UTM zona 21S)
_CRS_METRIC = "EPSG:31981"

_LARGURA_COL_CANDIDATES = ["largura", "width", "larg", "ds_largura"]


def _buffer_for_width(width_m: float) -> float:
    for limit, buf in _LARGURA_BUFFER:
        if width_m <= limit:
            return buf
    return 500.0


def _get_width(row) -> float:
    for col in _LARGURA_COL_CANDIDATES:
        if col in row.index and row[col] is not None:
            try:
                return float(row[col])
            except (ValueError, TypeError):
                pass
    return 0.0  # desconhecida → usar menor faixa


def calculate_app(property_geom, declared_app_geom=None) -> AppResult:
    """
    property_geom: shapely geometry do imóvel (WGS84)
    declared_app_geom: shapely geometry da APP declarada no SICAR (pode ser None)
    """
    # ── Hidrografia declarada no SICAR dentro do imóvel ──────────────────────
    hidro = load_by_polygon("sicar_hidrografia", property_geom)

    pendencias: list[Pendencia] = []

    if hidro.empty:
        # sem hidrografia declarada no imóvel → não calcula APP hídrica
        return AppResult(
            area_declarada_ha=_area_ha(declared_app_geom),
            area_calculada_ha=0.0,
            deficit_ha=0.0,
            status=StatusCode.ATENCAO,
            pendencias=[Pendencia(
                codigo="APP_SEM_HIDROGRAFIA",
                status=StatusCode.ATENCAO,
                titulo="Hidrografia não declarada",
                detalhe="Nenhum curso d'água declarado no imóvel. APP hídrica não calculada.",
                orientacao="Verifique se o imóvel possui rios ou nascentes. Se tiver, eles precisam ser declarados no CAR.",
            )],
        )

    # ── Reprojetar para métrico para fazer buffer em metros ───────────────────
    hidro_metric = gdf_to_metric(hidro)

    buffers = []
    used_default = False

    for _, row in hidro_metric.iterrows():
        if row.geometry is None or row.geometry.is_empty:
            continue
        width = _get_width(row)
        buf_m = _buffer_for_width(width)
        if width == 0:
            used_default = True
        buffers.append(row.geometry.buffer(buf_m))

    if not buffers:
        calculated_area_ha = 0.0
        deficit_ha = 0.0
    else:
        app_union = unary_union(buffers)
        prop_metric = to_metric(property_geom)
        app_clipped = app_union.intersection(prop_metric)

        import geopandas as gpd
        app_clipped_wgs = (
            gpd.GeoSeries([app_clipped], crs=_CRS_METRIC)
            .to_crs("EPSG:4326")
            .iloc[0]
        )
        calculated_area_ha = _area_ha(app_clipped_wgs)
        declared_area_ha = _area_ha(declared_app_geom)
        deficit_ha = max(0.0, calculated_area_ha - declared_area_ha)

    declared_area_ha = _area_ha(declared_app_geom)
    deficit_ha = max(0.0, calculated_area_ha - declared_area_ha)

    if used_default:
        pendencias.append(Pendencia(
            codigo="APP_LARGURA_DESCONHECIDA",
            status=StatusCode.ATENCAO,
            titulo="Largura dos cursos d'água não informada",
            detalhe="Buffer mínimo de 30m aplicado. Rios com largura > 10m exigem faixas maiores.",
            orientacao="Se o rio que passa na sua propriedade tiver mais de 10 metros de largura, a faixa de proteção obrigatória é maior que 30 metros.",
        ))

    if deficit_ha > 0.5:  # tolerância de 0.5 ha
        status = StatusCode.CRITICO
        pendencias.append(Pendencia(
            codigo="APP_DEFICIT",
            status=StatusCode.CRITICO,
            titulo="Déficit de APP",
            detalhe=f"APP calculada: {calculated_area_ha:.1f} ha | Declarada: {declared_area_ha:.1f} ha | Déficit: {deficit_ha:.1f} ha",
            orientacao=f"A área de proteção ao redor dos rios e nascentes está {deficit_ha:.1f} hectares menor do que exige a lei. É preciso retificar o CAR.",
            area_ha=deficit_ha,
        ))
    elif deficit_ha > 0:
        status = StatusCode.ATENCAO
    else:
        status = StatusCode.OK

    return AppResult(
        area_declarada_ha=declared_area_ha,
        area_calculada_ha=calculated_area_ha,
        deficit_ha=deficit_ha,
        status=status,
        pendencias=pendencias,
    )


def _area_ha(geom) -> float:
    if geom is None:
        return 0.0
    import geopandas as gpd
    series = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(_CRS_METRIC)
    return series.area.iloc[0] / 10_000
