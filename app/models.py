"""
Modelos Pydantic — request/response da API CAR Doutor
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StatusCode(str, Enum):
    CRITICO = "critico"       # não conformidade grave
    ATENCAO = "atencao"       # irregularidade menor ou pendência
    OK = "ok"                 # conforme


class Pendencia(BaseModel):
    codigo: str               # ex: "APP_DEFICIT"
    status: StatusCode
    titulo: str
    detalhe: str              # técnico
    orientacao: str           # linguagem simples para o produtor
    area_ha: float | None = None


class AnalyseRequest(BaseModel):
    car_code: str | None = Field(
        None,
        description="Código CAR no formato MT-XXXX-... (busca no SICAR)",
        examples=["MT-5100102-5F1A2B3C4D5E"],
    )
    geometry: dict[str, Any] | None = Field(
        None,
        description="GeoJSON Polygon/MultiPolygon alternativo ao car_code",
    )
    use_ai: bool = Field(
        True,
        description="Se True, usa a API Anthropic para gerar os resumos. Se False, usa template por regras (mais rápido).",
    )

    model_config = {"json_schema_extra": {
        "example": {"car_code": "MT-5100102-5F1A2B3C4D5E", "use_ai": True}
    }}


class AppResult(BaseModel):
    area_declarada_ha: float
    area_calculada_ha: float
    deficit_ha: float
    status: StatusCode
    pendencias: list[Pendencia]


class RlResult(BaseModel):
    area_imovel_ha: float
    area_declarada_ha: float
    area_minima_ha: float
    percentual_declarado: float
    percentual_minimo: float
    bioma: str
    status: StatusCode
    pendencias: list[Pendencia]


class DeforestResult(BaseModel):
    alertas_prodes: int
    alertas_deter: int
    area_desmatada_ha: float
    status: StatusCode
    pendencias: list[Pendencia]


class RestricaoResult(BaseModel):
    sobreposicao_ti: bool
    sobreposicao_uc: bool
    area_ti_ha: float
    area_uc_ha: float
    status: StatusCode
    pendencias: list[Pendencia]


class SoloResult(BaseModel):
    disponivel: bool = False
    ph: float | None = None
    ph_classe: str | None = None
    argila_pct: float | None = None
    areia_pct: float | None = None
    silte_pct: float | None = None
    carbono_organico_g_kg: float | None = None
    nitrogenio_g_kg: float | None = None
    cec_cmol_kg: float | None = None
    densidade_bulk_kg_dm3: float | None = None
    textura: str | None = None
    risco_erosao: str | None = None
    classe_ibge: str | None = None
    estoque_carbono_tC_ha: float | None = None


class CadastroPerfil(BaseModel):
    """Dados do perfil do imóvel extraídos do SICAR."""
    ind_status: str | None = None         # AT/SU/CA/PE
    des_condic: str | None = None         # situação de análise
    ind_tipo: str | None = None           # IRU/AST/PCT
    mod_fiscal: float | None = None       # tamanho em módulos fiscais
    dat_criacao: str | None = None
    dat_atualizacao: str | None = None
    classificacao_porte: str | None = None  # Minifúndio/Pequena/Média/Grande


class LaudoResult(BaseModel):
    car_code: str | None
    status_geral: StatusCode
    area_imovel_ha: float
    municipio: str | None
    geometry: dict[str, Any] | None = None
    app: AppResult
    rl: RlResult
    desmatamento: DeforestResult
    restricoes: RestricaoResult
    solo: SoloResult = SoloResult()
    cadastro: CadastroPerfil = CadastroPerfil()
    resumo_simples: str
    resumo_tecnico: str
    resumo_gerado_por: str = "template"  # "ia" | "template"
    total_pendencias: int
    pendencias_criticas: int
