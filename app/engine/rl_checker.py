"""
Verificação de Reserva Legal — Lei 12.651/2012, Art. 12
Percentual mínimo por bioma:
  Amazônia Legal:  80%
  Cerrado (dentro da Amazônia Legal): 35%
  Cerrado (fora): 20%
  Caatinga, Pantanal, Pampa: 20%

Simplificação MVP:
  Usa o centróide do imóvel e uma fronteira simplificada
  para determinar Amazônia vs Cerrado em MT.
  Fronteira real: polígono IBGE de biomas (pode ser adicionado pós-hackathon).
"""

from shapely.geometry import Point

from ..data.loader import load_by_polygon
from ..models import Pendencia, RlResult, StatusCode
from .geom_utils import gdf_to_metric, to_metric

_CRS_METRIC = "EPSG:31981"

# Percentuais mínimos por bioma
_RL_MINIMA = {
    "Amazônia Legal": 0.80,
    "Cerrado (Amazônia Legal)": 0.35,
    "Cerrado": 0.20,
    "Caatinga": 0.20,
    "Pantanal": 0.20,
    "Pampa": 0.20,
}

# Fronteira simplificada Amazônia/Cerrado em MT
# Latitude aproximada de transição (norte = Amazônia, sul = Cerrado)
_AMAZONIA_LAT_THRESHOLD = -13.0


def _biome_for_centroid(centroid: Point) -> str:
    """
    Determina bioma pelo centróide.
    Para MT: norte de -13° → Amazônia Legal, sul → Cerrado dentro da Amazônia Legal.
    """
    if centroid.y > _AMAZONIA_LAT_THRESHOLD:
        return "Amazônia Legal"
    else:
        # MT inteiro está dentro da Amazônia Legal para fins legais
        # mesmo o Cerrado de MT tem RL de 35% (não 20%)
        return "Cerrado (Amazônia Legal)"


def check_rl(
    property_geom,
    property_area_ha: float,
    car_code: str | None = None,
    mod_fiscal: float | None = None,
) -> RlResult:
    """
    property_geom: shapely geometry do imóvel (WGS84)
    property_area_ha: área do imóvel em hectares
    car_code: quando fornecido, filtra vegetação pelo cod_imovel do próprio CAR,
              evitando contabilizar RL declarada por propriedades vizinhas.
    """
    pendencias: list[Pendencia] = []

    # Determinar bioma
    centroid = property_geom.centroid
    bioma = _biome_for_centroid(centroid)
    pct_minimo = _RL_MINIMA[bioma]
    area_minima_ha = property_area_ha * pct_minimo

    # Carregar vegetação nativa declarada no SICAR
    veg = load_by_polygon("sicar_vegetacao_nativa", property_geom)

    # Filtrar pelo cod_imovel quando disponível — impede que RL de vizinhos
    # seja contabilizada como deste imóvel (falso-positivo de conformidade).
    if car_code and not veg.empty and "cod_imovel" in veg.columns:
        own = veg[veg["cod_imovel"].astype(str).str.upper() == car_code.upper()]
        if not own.empty:
            veg = own

    if veg.empty:
        declared_ha = 0.0
    else:
        veg_metric  = gdf_to_metric(veg)
        prop_metric = to_metric(property_geom)
        clipped = veg_metric.geometry.intersection(prop_metric)
        declared_ha = clipped.area.sum() / 10_000

    pct_declarado = declared_ha / property_area_ha if property_area_ha > 0 else 0.0
    deficit_ha = max(0.0, area_minima_ha - declared_ha)

    # Art. 67 — imóveis com até 4 módulos fiscais têm regime especial:
    # a vegetação nativa existente na data do cadastro é a RL, sem % mínimo.
    art67 = mod_fiscal is not None and mod_fiscal <= 4

    if art67:
        if declared_ha > 0:
            # Tem vegetação → conforme pelo Art. 67 (mantém o que tem)
            status = StatusCode.OK
            pendencias.append(Pendencia(
                codigo="RL_ART67_PEQUENA_PROPRIEDADE",
                status=StatusCode.OK,
                titulo="Reserva Legal — Art. 67 (Pequena Propriedade)",
                detalhe=(
                    f"Imóvel com {mod_fiscal:.2f} módulo(s) fiscal(is) — aplica-se o Art. 67 "
                    f"da Lei 12.651/2012. Vegetação nativa existente ({declared_ha:.1f} ha) "
                    f"é computada como RL independente do percentual mínimo do bioma."
                ),
                orientacao=(
                    "Sua propriedade é pequena (até 4 módulos fiscais). Pela lei, você precisa "
                    "apenas manter a vegetação nativa que já existe — não há obrigação de atingir "
                    f"os {pct_minimo*100:.0f}% normalmente exigidos para o bioma {bioma}."
                ),
            ))
        else:
            # Sem nenhuma vegetação declarada — atenção mesmo com Art. 67
            status = StatusCode.ATENCAO
            pendencias.append(Pendencia(
                codigo="RL_ART67_SEM_VEGETACAO",
                status=StatusCode.ATENCAO,
                titulo="Nenhuma vegetação nativa declarada (Art. 67)",
                detalhe=(
                    f"Imóvel com {mod_fiscal:.2f} módulo(s) fiscal(is). "
                    "O Art. 67 exige manutenção da vegetação existente, mas nenhuma foi declarada."
                ),
                orientacao=(
                    "Mesmo sendo uma pequena propriedade, é necessário declarar no CAR toda a "
                    "vegetação nativa presente. Se não houver vegetação, verifique se o imóvel "
                    "realmente está em conformidade com o órgão ambiental estadual (SEMA-MT)."
                ),
            ))
    elif deficit_ha > 0.5:
        status = StatusCode.CRITICO
        pendencias.append(Pendencia(
            codigo="RL_DEFICIT",
            status=StatusCode.CRITICO,
            titulo="Déficit de Reserva Legal",
            detalhe=(
                f"Bioma: {bioma} | RL mínima: {pct_minimo*100:.0f}% "
                f"({area_minima_ha:.1f} ha) | Declarada: {declared_ha:.1f} ha "
                f"({pct_declarado*100:.1f}%) | Déficit: {deficit_ha:.1f} ha"
            ),
            orientacao=(
                f"A lei exige que {pct_minimo*100:.0f}% da sua propriedade "
                f"({area_minima_ha:.1f} hectares) seja preservada como Reserva Legal. "
                f"Você declarou apenas {declared_ha:.1f} hectares. "
                f"É preciso regularizar os {deficit_ha:.1f} hectares que faltam, "
                f"seja recuperando a área ou compensando em outro imóvel."
            ),
            area_ha=deficit_ha,
        ))
    elif veg.empty:
        status = StatusCode.ATENCAO
        pendencias.append(Pendencia(
            codigo="RL_NAO_DECLARADA",
            status=StatusCode.ATENCAO,
            titulo="Vegetação nativa não declarada",
            detalhe="Nenhuma vegetação nativa encontrada no cadastro deste imóvel.",
            orientacao="Você precisa declarar a área de vegetação nativa da sua propriedade no CAR.",
        ))
    else:
        status = StatusCode.OK

    return RlResult(
        area_imovel_ha=property_area_ha,
        area_declarada_ha=declared_ha,
        area_minima_ha=area_minima_ha,
        percentual_declarado=round(pct_declarado * 100, 1),
        percentual_minimo=round(pct_minimo * 100, 1),
        bioma=bioma,
        status=status,
        pendencias=pendencias,
    )
