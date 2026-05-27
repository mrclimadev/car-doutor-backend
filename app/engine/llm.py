"""
Geração de resumos via Claude (Anthropic API).
Dois resumos por laudo:
  - resumo_simples: linguagem de produtor rural (Seu Raimundo)
  - resumo_tecnico: linguagem técnica para analista OEMA (Luana)
"""

import logging
import os

from ..models import LaudoResult

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"  # rápido e barato para o hackathon

def _api_key() -> str:
    """Lê a chave em tempo de execução para garantir que load_dotenv() já rodou."""
    return os.getenv("ANTHROPIC_API_KEY", "")


def _build_context(laudo: LaudoResult) -> str:
    pendencias_txt = "\n".join(
        f"  [{p.status.value.upper()}] {p.titulo}: {p.detalhe}"
        for grupo in [laudo.app.pendencias, laudo.rl.pendencias,
                      laudo.desmatamento.pendencias, laudo.restricoes.pendencias]
        for p in grupo
    ) or "  Nenhuma pendência encontrada."

    return f"""
Imóvel rural — CAR Doutor
CAR: {laudo.car_code or 'não informado'}
Área: {laudo.area_imovel_ha:.1f} ha
Município: {laudo.municipio or 'MT'}
Status geral: {laudo.status_geral.value}
Total de pendências: {laudo.total_pendencias} ({laudo.pendencias_criticas} críticas)

APP: declarada={laudo.app.area_declarada_ha:.1f}ha, calculada={laudo.app.area_calculada_ha:.1f}ha, déficit={laudo.app.deficit_ha:.1f}ha
RL: declarada={laudo.rl.area_declarada_ha:.1f}ha ({laudo.rl.percentual_declarado}%), mínima={laudo.rl.percentual_minimo}%, bioma={laudo.rl.bioma}
Desmatamento: {laudo.desmatamento.alertas_prodes} alertas PRODES, {laudo.desmatamento.alertas_deter} alertas DETER
Restrições: TI={laudo.restricoes.sobreposicao_ti}, UC={laudo.restricoes.sobreposicao_uc}

Pendências detalhadas:
{pendencias_txt}
""".strip()


_PROMPT_SIMPLES = """
Você é o CAR Doutor, um assistente que ajuda produtores rurais do Brasil a entender o Cadastro Ambiental Rural (CAR).
Seu papel é explicar, com linguagem simples e respeitosa, o resultado da análise do imóvel.
Use linguagem que um produtor rural com ensino fundamental entenderia.
Evite termos técnicos. Quando necessário, explique o que significa (ex: "APP — a faixa de mata que deve ficar ao redor dos rios").
Seja direto: diga o que está errado, o que precisa ser feito, e que o produtor pode resolver.
Não cause pânico, mas seja honesto sobre a gravidade quando houver problemas críticos.
Responda em no máximo 5 parágrafos curtos.
"""

_PROMPT_TECNICO = """
Você é um sistema de análise ambiental que gera laudos técnicos para analistas de OEMA.
Com base nos dados fornecidos, elabore um resumo técnico objetivo com:
- Status geral de conformidade com o Código Florestal (Lei 12.651/2012)
- Pendências críticas identificadas (APP, RL, desmatamento, restrições)
- Artigos legais aplicáveis
- Recomendações de encaminhamento
Responda em linguagem técnica, máximo 4 parágrafos.
"""


def generate_summaries(laudo: LaudoResult, use_ai: bool = True) -> tuple[str, str, str]:
    """
    Retorna (resumo_simples, resumo_tecnico, gerado_por).
    gerado_por: "ia" | "template"
    """
    if not use_ai:
        log.info("Modo template — geração por regras (use_ai=False)")
        return _fallback_simples(laudo), _fallback_tecnico(laudo), "template"

    key = _api_key()
    if not key:
        log.warning("ANTHROPIC_API_KEY não configurado — usando resumo por regras")
        return _fallback_simples(laudo), _fallback_tecnico(laudo), "template"

    context = _build_context(laudo)

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)

        simples = client.messages.create(
            model=_MODEL,
            max_tokens=600,
            system=_PROMPT_SIMPLES,
            messages=[{"role": "user", "content": context}],
        ).content[0].text

        tecnico = client.messages.create(
            model=_MODEL,
            max_tokens=800,
            system=_PROMPT_TECNICO,
            messages=[{"role": "user", "content": context}],
        ).content[0].text

        log.info("Resumos gerados pela IA (%s)", _MODEL)
        return simples, tecnico, "ia"

    except Exception as exc:
        log.error("Erro na API Anthropic: %s — usando fallback", exc)
        return _fallback_simples(laudo), _fallback_tecnico(laudo), "template"


def _fallback_simples(laudo: LaudoResult) -> str:
    if laudo.status_geral == "ok":
        return (
            f"Sua propriedade de {laudo.area_imovel_ha:.0f} hectares está em conformidade "
            f"com o Código Florestal. Continue mantendo as áreas de preservação declaradas."
        )

    partes = [f"Analisamos sua propriedade de {laudo.area_imovel_ha:.0f} hectares."]

    if laudo.app.deficit_ha > 0:
        partes.append(
            f"A faixa de proteção ao redor dos rios (APP) está {laudo.app.deficit_ha:.0f} "
            f"hectares menor do que a lei exige. É preciso corrigir isso no seu CAR."
        )
    if laudo.rl.area_declarada_ha < laudo.rl.area_minima_ha:
        partes.append(
            f"A Reserva Legal declarada ({laudo.rl.percentual_declarado}%) está abaixo do "
            f"mínimo exigido de {laudo.rl.percentual_minimo}% para o bioma {laudo.rl.bioma}."
        )
    if laudo.desmatamento.alertas_prodes > 0:
        partes.append(
            "Há registros de desmatamento dentro da sua propriedade segundo o INPE. "
            "Procure orientação de um técnico ambiental."
        )
    partes.append("Entre em contato com a SEMA-MT ou um técnico habilitado para regularizar seu CAR.")
    return " ".join(partes)


def _fallback_tecnico(laudo: LaudoResult) -> str:
    return (
        f"Imóvel rural {laudo.car_code or 's/n'} — {laudo.area_imovel_ha:.2f} ha — "
        f"Bioma: {laudo.rl.bioma}. "
        f"Status: {laudo.status_geral.value.upper()}. "
        f"Pendências: {laudo.total_pendencias} ({laudo.pendencias_criticas} críticas). "
        f"APP: déficit de {laudo.app.deficit_ha:.2f} ha. "
        f"RL: {laudo.rl.percentual_declarado}% declarado (mínimo: {laudo.rl.percentual_minimo}%). "
        f"Desmatamento: {laudo.desmatamento.alertas_prodes} pol. PRODES, "
        f"{laudo.desmatamento.alertas_deter} alertas DETER. "
        f"Restrições: TI={laudo.restricoes.sobreposicao_ti}, UC={laudo.restricoes.sobreposicao_uc}."
    )
