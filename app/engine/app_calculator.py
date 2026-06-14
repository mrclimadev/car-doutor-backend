"""
Cálculo de APP — Área de Preservação Permanente
Lei 12.651/2012, Art. 4º e Art. 61-A

Regras implementadas:
  - Cursos d'água: buffer conforme largura do curso (art. 4º, I)
  - Nascentes: 50m de raio (art. 4º, IV)
  - Art. 61-A §§1-4: buffers reduzidos para áreas consolidadas por porte do imóvel
  - Encostas > 45°: área inteira (não implementado no MVP — requer DEM)

Simplificação do MVP:
  Sem atributo de largura na hidrografia do SICAR → aplica 30m (menor faixa legal)
  ou o buffer Art. 61-A quando mod_fiscal é fornecido.
  O laudo indica que uma medição em campo pode aumentar a exigência.
"""

import math

from shapely.ops import unary_union

from ..data.loader import load_by_polygon
from ..models import Pendencia, StatusCode, AppResult
from .geom_utils import gdf_to_metric, to_metric

# Tabela de largura → buffer mínimo Art. 4° (metros)
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

# Limiar de déficit pequeno para tolerância metodológica (ha)
_DEFICIT_TOLERANCIA_HA = 2.0


def _buffer_for_width(width_m: float) -> float:
    for limit, buf in _LARGURA_BUFFER:
        if width_m <= limit:
            return buf
    return 500.0


def _art61a_buffer(mod_fiscal: float) -> float:
    """
    Buffer mínimo de recomposição de APP em áreas consolidadas — Art. 61-A §§1-4.
    Aplica-se apenas a imóveis com áreas consolidadas antes de jul/2008.
    """
    if mod_fiscal <= 1:  return 5.0
    if mod_fiscal <= 2:  return 8.0
    if mod_fiscal <= 4:  return 15.0
    # > 4 MF: Art. 61-A §4° I — cursos < 10m = 20m (usar como padrão sem largura)
    return 20.0


def _get_width(row) -> float:
    for col in _LARGURA_COL_CANDIDATES:
        if col in row.index and row[col] is not None:
            try:
                return float(row[col])
            except (ValueError, TypeError):
                pass
    return 0.0  # desconhecida → usar menor faixa


def calculate_app(property_geom, declared_app_geom=None, mod_fiscal: float | None = None, uf: str = "MT") -> AppResult:
    """
    property_geom: shapely geometry do imóvel (WGS84)
    declared_app_geom: shapely geometry da APP declarada no SICAR (pode ser None)
    mod_fiscal: módulos fiscais do imóvel — ativa Art. 61-A quando fornecido
    """
    # ── Hidrografia declarada no SICAR dentro do imóvel ──────────────────────
    hidro = load_by_polygon("sicar_hidrografia", property_geom, uf)

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

    # Determinar se aplica Art. 61-A (imóvel com mod_fiscal conhecido)
    usar_art61a = mod_fiscal is not None
    buf_art61a = _art61a_buffer(mod_fiscal) if usar_art61a else None

    # ── Reprojetar para métrico para fazer buffer em metros ───────────────────
    hidro_metric = gdf_to_metric(hidro)

    buffers = []
    used_default = False

    for _, row in hidro_metric.iterrows():
        if row.geometry is None or row.geometry.is_empty:
            continue
        width = _get_width(row)
        if width == 0:
            # Largura desconhecida: usa Art. 61-A se disponível, senão Art. 4° mínimo (30m)
            buf_m = buf_art61a if usar_art61a else _buffer_for_width(0)
            used_default = True
        else:
            buf_art4 = _buffer_for_width(width)
            # Art. 61-A nunca exige mais que Art. 4°; limita pelo menor
            buf_m = min(buf_art4, buf_art61a) if usar_art61a else buf_art4

        geom_type = row.geometry.geom_type
        if geom_type in ("Polygon", "MultiPolygon"):
            # Corpo hídrico como polígono: APP é a faixa de terra em volta, não a água em si.
            # buffer().difference() remove o interior do polígono do resultado.
            buffered = row.geometry.buffer(buf_m).difference(row.geometry)
        else:
            buffered = row.geometry.buffer(buf_m)
        buffers.append(buffered)

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

    if usar_art61a:
        pendencias.append(Pendencia(
            codigo="APP_ART61A_APLICADO",
            status=StatusCode.OK,
            titulo="APP calculada com Art. 61-A (área consolidada)",
            detalhe=(
                f"Imóvel com {mod_fiscal:.2f} módulo(s) fiscal(is). "
                f"Buffer de recomposição: {buf_art61a:.0f}m (Art. 61-A "
                f"{'§1°' if mod_fiscal <= 1 else '§2°' if mod_fiscal <= 2 else '§3°' if mod_fiscal <= 4 else '§4°'}, "
                f"Lei 12.651/2012), em vez dos {_buffer_for_width(0):.0f}m do Art. 4°."
            ),
            orientacao=(
                f"Para pequenas propriedades, a lei permite uma faixa de recomposição menor "
                f"({buf_art61a:.0f}m) nas áreas que já eram usadas antes de julho de 2008."
            ),
        ))
    elif used_default:
        pendencias.append(Pendencia(
            codigo="APP_LARGURA_DESCONHECIDA",
            status=StatusCode.ATENCAO,
            titulo="Largura dos cursos d'água não informada",
            detalhe="Buffer mínimo de 30m aplicado (Art. 4°). Rios com largura > 10m exigem faixas maiores.",
            orientacao="Se o rio que passa na sua propriedade tiver mais de 10 metros de largura, a faixa de proteção obrigatória é maior que 30 metros.",
        ))

    if deficit_ha > 0.5:
        # Tolerância metodológica: déficit pequeno + buffer padrão sem largura → ATENCAO
        if used_default and deficit_ha < _DEFICIT_TOLERANCIA_HA:
            status = StatusCode.ATENCAO
            pendencias.append(Pendencia(
                codigo="APP_DEFICIT_TOLERANCIA",
                status=StatusCode.ATENCAO,
                titulo="Possível déficit de APP (verificar em campo)",
                detalhe=(
                    f"APP calculada: {calculated_area_ha:.1f} ha | Declarada: {declared_area_ha:.1f} ha | "
                    f"Déficit estimado: {deficit_ha:.1f} ha. "
                    f"Calculado com buffer padrão (largura do curso d'água não declarada no SICAR)."
                ),
                orientacao=(
                    f"O déficit de {deficit_ha:.1f} ha pode ser decorrente do uso do buffer padrão de "
                    f"{_buffer_for_width(0):.0f}m. Se os rios tiverem largura inferior a 10m, a faixa "
                    f"exigida seria a mesma e o déficit real pode ser menor. Recomenda-se verificação em campo."
                ),
                area_ha=deficit_ha,
            ))
        else:
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
