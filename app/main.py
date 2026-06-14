"""
CAR Doutor — API principal
FastAPI + endpoint POST /analyze
"""

import json
import logging
import sys
import urllib3

from pathlib import Path
from dotenv import load_dotenv

# Caminho explícito: sempre backend/.env, independente do diretório de trabalho
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_FILE, override=True)

import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .engine.analyzer import analyze
from .models import AnalyseRequest, LaudoResult

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(
    title="CAR Doutor",
    description=(
        "Analisa inconsistências no Cadastro Ambiental Rural (CAR) "
        "cruzando dados do SICAR com bases abertas. "
        "Gera laudo com pendências e orientações para o produtor rural."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restringir em produção
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    key = _read_env_var("RESEND_API_KEY")
    return {
        "status": "ok",
        "service": "car-doutor",
        "resend_key": f"{key[:8]}..." if key else "NAO CONFIGURADO",
        "env_path": str(Path(__file__).resolve().parent.parent / ".env"),
        "env_exists": (Path(__file__).resolve().parent.parent / ".env").exists(),
    }


@app.get("/layers")
def list_layers():
    """Lista camadas geoespaciais disponíveis localmente."""
    from .data.loader import available_layers
    return {"layers": available_layers()}


# ── WFS proxy endpoints ──────────────────────────────────────────────────────

_WFS_CONFIGS = {
    "prodes": {
        "url": "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-amazon-nb/wfs",
        "params": {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "prodes-amazon-nb:yearly_deforestation_biome",
            "outputFormat": "application/json",
            "count": "300",
        },
    },
    "deter": {
        "url": "https://terrabrasilis.dpi.inpe.br/geoserver/deter-amz/wfs",
        "params": {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": "deter-amz:deter_amz",
            "outputFormat": "application/json",
            "maxFeatures": "300",
        },
    },
    "ti": {
        "url": "https://geoserver.funai.gov.br/geoserver/Funai/wfs",
        "params": {
            "service": "WFS",
            "version": "1.0.0",
            "request": "GetFeature",
            "typeName": "Funai:tis_poligonais",
            "outputFormat": "application/json",
            "maxFeatures": "300",
        },
    },
    "uc": {
        "url": "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-amazon-nb/wfs",
        "params": {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "prodes-amazon-nb:conservation_units_amazon_biome",
            "outputFormat": "application/json",
            "count": "300",
        },
    },
}

_KEEP_PROPS = {
    "prodes": ["year", "area_km", "class_name", "state"],
    "deter": ["classname", "view_date", "areauckm", "municipality"],
    "ti": ["terrai_nom", "fase_ti", "modalidade", "etnia_nome"],
    "uc": ["nome", "categoria", "grupo", "esfera", "ano_cria"],
}


@app.get("/map-layer")
def map_layer(
    layer: str = Query(..., description="prodes | deter | ti | uc"),
    bbox: str = Query(..., description="west,south,east,north"),
):
    """
    Proxy WFS → GeoJSON para o frontend (contorna CORS).
    Retorna FeatureCollection com propriedades reduzidas.
    """
    if layer not in _WFS_CONFIGS:
        raise HTTPException(status_code=400, detail=f"Camada inválida: {layer}. Use: {list(_WFS_CONFIGS)}")

    cfg = _WFS_CONFIGS[layer]
    params = dict(cfg["params"])
    params["bbox"] = f"{bbox},EPSG:4326"

    headers = {"User-Agent": "Mozilla/5.0 (compatible; CAR-Doutor/1.0)"}

    try:
        resp = requests.get(cfg["url"], params=params, headers=headers, timeout=20, verify=False)
        resp.raise_for_status()
        fc = resp.json()
    except requests.exceptions.Timeout:
        log.warning("Timeout WFS layer=%s — retornando vazio", layer)
        return Response(
            content='{"type":"FeatureCollection","features":[]}',
            media_type="application/json",
        )
    except Exception as exc:
        log.warning("Erro WFS layer=%s: %s", layer, exc)
        return Response(
            content='{"type":"FeatureCollection","features":[]}',
            media_type="application/json",
        )

    keep = set(_KEEP_PROPS.get(layer, []))
    for feat in fc.get("features", []):
        props = feat.get("properties") or {}
        feat["properties"] = {k: v for k, v in props.items() if k in keep}

    # Remove non-standard crs field — MapLibre expects plain WGS84 GeoJSON
    fc.pop("crs", None)
    fc.pop("totalFeatures", None)
    fc.pop("numberMatched", None)
    fc.pop("numberReturned", None)
    fc.pop("timeStamp", None)

    return Response(
        content=json.dumps(fc, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/locate/{car_code}")
def locate_property(car_code: str):
    """
    Retorna apenas centróide + bbox do imóvel para o mapa voar antes da análise.
    Leve: só lê geometria, sem cálculos.
    """
    from .data.loader import find_property_by_car
    rows = find_property_by_car(car_code)
    if rows is None or rows.empty:
        raise HTTPException(status_code=404, detail=f"Imóvel não encontrado: {car_code}")
    geom = rows.iloc[0].geometry
    c    = geom.centroid
    b    = geom.bounds  # (minx, miny, maxx, maxy)
    return {
        "lng":  round(c.x, 6),
        "lat":  round(c.y, 6),
        "bbox": [round(b[0], 6), round(b[1], 6), round(b[2], 6), round(b[3], 6)],
    }


@app.get("/stats")
def get_stats():
    """Estatísticas agregadas do cadastro SICAR (sicar_area_imovel)."""
    from .data.loader import _load_full
    gdf = _load_full("sicar_area_imovel")

    total = int(len(gdf))

    # Condição cadastral
    por_condicao = []
    if "des_condic" in gdf.columns:
        counts = gdf["des_condic"].fillna("N/A").value_counts()
        por_condicao = [{"codigo": str(k), "count": int(v)} for k, v in counts.items()]

    # Status do imóvel
    por_status = []
    if "ind_status" in gdf.columns:
        counts = gdf["ind_status"].fillna("N/A").value_counts()
        por_status = [{"codigo": str(k), "count": int(v)} for k, v in counts.items()]

    # Top municípios (tenta múltiplas colunas)
    municipios = []
    for col in ["municipio", "nm_municip", "municipio_", "nm_mun"]:
        if col in gdf.columns:
            counts = gdf[col].fillna("").value_counts().head(12)
            municipios = [{"nome": str(k), "count": int(v)} for k, v in counts.items() if k]
            if municipios:
                break

    return {
        "total": total,
        "por_condicao": por_condicao,
        "por_status": por_status,
        "municipios": municipios,
    }


@app.get("/stats/map")
def stats_map(
    condic: str = Query(None, description="Filtrar por des_condic"),
    ind_status_filter: str = Query(None, alias="ind_status", description="Filtrar por ind_status"),
    municipio: str = Query(None, description="Filtrar por município (nome exato)"),
    limit: int = Query(500, ge=1, le=2000, description="Máximo de features retornadas"),
):
    """Retorna GeoJSON com centroides das propriedades filtradas (para visualização no mapa)."""
    from .data.loader import _load_full
    gdf = _load_full("sicar_area_imovel").copy()

    if condic and "des_condic" in gdf.columns:
        gdf = gdf[gdf["des_condic"].astype(str).str.upper() == condic.upper()]
    if ind_status_filter and "ind_status" in gdf.columns:
        gdf = gdf[gdf["ind_status"].astype(str).str.upper() == ind_status_filter.upper()]
    if municipio:
        for col in ["municipio", "nm_municip", "municipio_", "nm_mun"]:
            if col in gdf.columns:
                mask = gdf[col].astype(str).str.upper() == municipio.upper()
                if mask.any():
                    gdf = gdf[mask]
                    break

    if len(gdf) > limit:
        gdf = gdf.sample(limit, random_state=42)

    # Colunas úteis para o popup
    keep = [c for c in ["cod_imovel", "des_condic", "ind_status", "municipio",
                         "nm_municip", "nm_mun", "municipio_"] if c in gdf.columns]

    features = []
    for _, row in gdf.iterrows():
        try:
            c = row.geometry.centroid
            props = {col: (str(row[col]) if row[col] is not None else None) for col in keep}
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [c.x, c.y]},
                "properties": props,
            })
        except Exception:
            continue

    fc = {"type": "FeatureCollection", "features": features}
    return Response(
        content=json.dumps(fc, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=60"},
    )


_SKIP_COLS = {'cpf', 'cnpj', 'proprietario', 'prop_', 'titular', 'responsavel'}

@app.get("/imovel/{car_code}")
def get_imovel_data(car_code: str):
    """Retorna todos os dados cadastrais do SICAR para um imóvel (sem PII)."""
    import pandas as pd
    from .data.loader import find_property_by_car
    result = find_property_by_car(car_code)
    if result is None or result.empty:
        raise HTTPException(status_code=404, detail=f"Imóvel não encontrado: {car_code}")
    row = result.iloc[0]
    dados = {}
    for col in result.columns:
        if col == "geometry":
            continue
        if any(s in col.lower() for s in _SKIP_COLS):
            continue
        val = row[col]
        try:
            if pd.isna(val):
                dados[col] = None
            elif isinstance(val, float):
                dados[col] = round(float(val), 4)
            elif isinstance(val, int):
                dados[col] = int(val)
            else:
                dados[col] = str(val)
        except Exception:
            dados[col] = str(val) if val is not None else None
    return {"car_code": car_code, "dados": dados}


_SICAR_OVERLAY_LAYERS = {
    "reserva_legal":          "sicar_reserva_legal",
    "vegetacao_nativa":       "sicar_vegetacao_nativa",
    "area_consolidada":       "sicar_area_consolidada",
    "uso_restrito":           "sicar_uso_restrito",
    "area_pousio":            "sicar_area_pousio",
    "servidao_administrativa":"sicar_servidao_administrativa",
    "apps":                   "sicar_apps",
    "hidrografia":            "sicar_hidrografia",
}

_OVERLAY_COLORS = {
    "reserva_legal":          "#1b5e20",
    "vegetacao_nativa":       "#388e3c",
    "area_consolidada":       "#c8a97e",
    "uso_restrito":           "#f5a623",
    "area_pousio":            "#d4c200",
    "servidao_administrativa":"#7b1fa2",
    "apps":                   "#1565c0",
    "hidrografia":            "#0288d1",
}


@app.get("/property-layers/{car_code}")
def property_layers(
    car_code: str,
    layers: str = Query(
        "reserva_legal,vegetacao_nativa,area_consolidada,uso_restrito,area_pousio,servidao_administrativa",
        description="Camadas separadas por vírgula",
    ),
):
    """
    Retorna GeoJSON das camadas SICAR internas para um imóvel específico.
    Usado pelo frontend para visualização de overlays no mapa após análise.
    """
    from .data.loader import find_property_by_car, load_by_polygon
    from shapely.geometry import mapping

    rows = find_property_by_car(car_code)
    if rows is None or rows.empty:
        raise HTTPException(status_code=404, detail=f"Imóvel não encontrado: {car_code}")

    property_geom = rows.iloc[0].geometry

    result = {}
    requested = [l.strip() for l in layers.split(",") if l.strip() in _SICAR_OVERLAY_LAYERS]

    for key in requested:
        parquet = _SICAR_OVERLAY_LAYERS[key]
        color = _OVERLAY_COLORS.get(key, "#4ecb71")
        try:
            gdf = load_by_polygon(parquet, property_geom)
            features = []
            for _, row in gdf.iterrows():
                try:
                    geom = row.geometry
                    if geom is None or geom.is_empty:
                        continue
                    features.append({
                        "type": "Feature",
                        "geometry": mapping(geom),
                        "properties": {"layer": key, "color": color},
                    })
                except Exception:
                    continue
            result[key] = {"type": "FeatureCollection", "features": features, "color": color}
        except Exception as exc:
            log.warning("Overlay layer %s indisponível: %s", key, exc)
            result[key] = {"type": "FeatureCollection", "features": [], "color": color}

    return Response(
        content=json.dumps(result, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/stats/municipio/{municipio}")
def stats_municipio(municipio: str):
    """
    Estatísticas de conformidade para um município — usado para comparação com vizinhança.
    """
    from .data.loader import _load_full
    gdf = _load_full("sicar_area_imovel").copy()

    # Filtrar por município
    for col in ["municipio", "nm_municip", "municipio_", "nm_mun"]:
        if col in gdf.columns:
            mask = gdf[col].astype(str).str.upper() == municipio.upper()
            if mask.any():
                gdf = gdf[mask]
                break

    total = int(len(gdf))
    if total == 0:
        raise HTTPException(status_code=404, detail=f"Município não encontrado: {municipio}")

    # % em conformidade
    pct_conforme = 0.0
    if "des_condic" in gdf.columns:
        n_conf = int(gdf["des_condic"].astype(str).str.lower().str.contains("conformidade").sum())
        pct_conforme = round(n_conf / total * 100, 1)

    # % ativos
    pct_ativo = 0.0
    if "ind_status" in gdf.columns:
        n_at = int((gdf["ind_status"].astype(str).str.upper() == "AT").sum())
        pct_ativo = round(n_at / total * 100, 1)

    # Média de módulos fiscais
    media_mf = None
    if "mod_fiscal" in gdf.columns:
        vals = gdf["mod_fiscal"].dropna()
        vals = vals[vals > 0]
        if not vals.empty:
            media_mf = round(float(vals.mean()), 2)

    # Distribuição de porte (módulos fiscais)
    porte_dist = {}
    if "mod_fiscal" in gdf.columns:
        vals = gdf["mod_fiscal"].dropna()
        porte_dist = {
            "minifundio":  int((vals < 1).sum()),
            "pequena":     int(((vals >= 1) & (vals <= 4)).sum()),
            "media":       int(((vals > 4) & (vals <= 15)).sum()),
            "grande":      int((vals > 15).sum()),
        }

    return {
        "municipio": municipio,
        "total_imoveis": total,
        "pct_conforme": pct_conforme,
        "pct_ativo": pct_ativo,
        "media_modulos_fiscais": media_mf,
        "porte_distribuicao": porte_dist,
    }


def _build_email_html(laudo: dict) -> str:
    """Gera HTML do laudo para envio por email (inline CSS — compatível com clientes de email)."""
    import html as html_lib

    ST = {
        'ok':      {'label': 'CONFORME',   'color': '#1b7a3e', 'bg': '#e8f5e9', 'border': '#4caf50'},
        'atencao': {'label': 'ATENÇÃO',    'color': '#bf5e00', 'bg': '#fff8e1', 'border': '#fb8c00'},
        'critico': {'label': 'CRÍTICO',    'color': '#b71c1c', 'bg': '#ffebee', 'border': '#ef5350'},
    }
    status = laudo.get('status_geral', 'atencao')
    st = ST.get(status, ST['atencao'])

    def esc(v): return html_lib.escape(str(v) if v is not None else '—')
    def ha(v):  return f"{float(v):.1f} ha" if v is not None else "—"
    def pct(v): return f"{float(v):.1f}%" if v is not None else "—"

    car_code  = esc(laudo.get('car_code', 'Geometria manual'))
    municipio = esc(laudo.get('municipio') or 'Mato Grosso')
    area      = ha(laudo.get('area_imovel_ha'))
    today     = __import__('datetime').date.today().strftime('%d/%m/%Y')

    app_d     = laudo.get('app', {})
    rl_d      = laudo.get('rl', {})
    desm_d    = laudo.get('desmatamento', {})
    rest_d    = laudo.get('restricoes', {})

    app_ok    = (app_d.get('deficit_ha') or 0) <= 0
    rl_ok     = (rl_d.get('percentual_declarado') or 0) >= (rl_d.get('percentual_minimo') or 20)
    alertas   = (desm_d.get('alertas_prodes') or 0) + (desm_d.get('alertas_deter') or 0)
    desm_ok   = alertas == 0
    rest_ok   = not rest_d.get('sobreposicao_ti') and not rest_d.get('sobreposicao_uc')

    def chip(ok, good_txt, bad_txt):
        c = '#1b7a3e' if ok else '#b71c1c'
        bg = '#e8f5e9' if ok else '#ffebee'
        return f'<span style="background:{bg};color:{c};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;">{good_txt if ok else bad_txt}</span>'

    all_pend = (
        (laudo.get('app') or {}).get('pendencias', []) +
        (laudo.get('rl') or {}).get('pendencias', []) +
        (laudo.get('desmatamento') or {}).get('pendencias', []) +
        (laudo.get('restricoes') or {}).get('pendencias', []) +
        ((laudo.get('land_use') or {}).get('pendencias', []))
    )

    def pend_color(s):
        return {'critico': ('#b71c1c', '#ffebee', '#ef5350'),
                'atencao': ('#bf5e00', '#fff8e1', '#fb8c00')}.get(s, ('#1b7a3e', '#e8f5e9', '#4caf50'))

    pend_rows = ''
    for p in all_pend:
        c, bg, border = pend_color(p.get('status', 'atencao'))
        pend_rows += f'''
        <tr>
          <td style="padding:10px 12px;border-left:3px solid {border};background:{bg};border-radius:4px;margin-bottom:6px;">
            <div style="font-size:12px;font-weight:700;color:{c};">{esc(p.get("titulo",""))}</div>
            {f'<div style="font-size:11px;color:#555;margin-top:3px;">{esc(p.get("detalhe",""))}</div>' if p.get("detalhe") else ""}
            {f'<div style="font-size:11px;color:#333;margin-top:4px;font-style:italic;">→ {esc(p.get("orientacao",""))}</div>' if p.get("orientacao") else ""}
          </td>
        </tr>
        <tr><td style="height:6px;"></td></tr>'''

    resumo_tecnico = esc(laudo.get('resumo_tecnico') or '').replace('\n', '<br>')

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7f5;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7f5;padding:24px 0;">
 <tr><td align="center">
  <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

   <!-- Header -->
   <tr><td style="background:#0a2e0a;border-radius:12px 12px 0 0;padding:28px 32px;">
     <div style="color:#4caf50;font-size:22px;font-weight:bold;letter-spacing:-0.5px;">🌿 CAR Doutor</div>
     <div style="color:#81c784;font-size:12px;margin-top:4px;">Análise Automática do Cadastro Ambiental Rural</div>
   </td></tr>

   <!-- Status banner -->
   <tr><td style="background:{st['bg']};border:1px solid {st['border']};padding:20px 32px;">
     <table width="100%" cellpadding="0" cellspacing="0">
       <tr>
         <td>
           <div style="font-size:10px;font-weight:700;color:#666;letter-spacing:1px;text-transform:uppercase;">Status Geral</div>
           <div style="font-size:26px;font-weight:bold;color:{st['color']};margin-top:4px;">{st['label']}</div>
           <div style="font-size:12px;color:#555;margin-top:4px;">{car_code}</div>
         </td>
         <td align="right">
           <div style="font-size:30px;font-weight:bold;color:#1b4d1b;">{area}</div>
           <div style="font-size:12px;color:#666;">{municipio} · MT</div>
           <div style="font-size:11px;color:#999;margin-top:4px;">{today}</div>
         </td>
       </tr>
     </table>
   </td></tr>

   <!-- Métricas principais -->
   <tr><td style="background:#fff;padding:24px 32px;border:1px solid #e0e0e0;border-top:0;">
     <div style="font-size:11px;font-weight:700;color:#666;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;">Métricas de Conformidade</div>
     <table width="100%" cellpadding="0" cellspacing="0">
       <tr>
         <td width="50%" style="padding-right:8px;vertical-align:top;">
           <div style="background:#f9f9f9;border:1px solid #e8e8e8;border-radius:8px;padding:12px 14px;margin-bottom:8px;">
             <div style="font-size:11px;color:#888;">🌊 APP</div>
             <div style="margin-top:4px;">{chip(app_ok, f'ok · {ha(app_d.get("area_declarada_ha"))} declarada', f'déficit {ha(app_d.get("deficit_ha"))}')}</div>
           </div>
         </td>
         <td width="50%" style="padding-left:8px;vertical-align:top;">
           <div style="background:#f9f9f9;border:1px solid #e8e8e8;border-radius:8px;padding:12px 14px;margin-bottom:8px;">
             <div style="font-size:11px;color:#888;">🌳 Reserva Legal · {esc(rl_d.get('bioma',''))}</div>
             <div style="margin-top:4px;">{chip(rl_ok, f'{pct(rl_d.get("percentual_declarado"))} declarado', f'{pct(rl_d.get("percentual_declarado"))} (mín {pct(rl_d.get("percentual_minimo"))})')}</div>
           </div>
         </td>
       </tr>
       <tr>
         <td width="50%" style="padding-right:8px;vertical-align:top;">
           <div style="background:#f9f9f9;border:1px solid #e8e8e8;border-radius:8px;padding:12px 14px;">
             <div style="font-size:11px;color:#888;">🛰️ Desmatamento</div>
             <div style="margin-top:4px;">{chip(desm_ok, 'sem alertas', f'{alertas} alertas PRODES/DETER')}</div>
           </div>
         </td>
         <td width="50%" style="padding-left:8px;vertical-align:top;">
           <div style="background:#f9f9f9;border:1px solid #e8e8e8;border-radius:8px;padding:12px 14px;">
             <div style="font-size:11px;color:#888;">⚖️ Restrições TI/UC</div>
             <div style="margin-top:4px;">{chip(rest_ok, 'sem sobreposição', 'sobreposição detectada')}</div>
           </div>
         </td>
       </tr>
     </table>
   </td></tr>

   <!-- Pendências -->
   <tr><td style="background:#fff;padding:0 32px 24px;border:1px solid #e0e0e0;border-top:0;">
     <div style="font-size:11px;font-weight:700;color:#666;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;padding-top:4px;">
       Pendências ({len(all_pend)})
     </div>
     {'<div style="color:#1b7a3e;font-size:13px;padding:12px;background:#e8f5e9;border-radius:6px;">✓ Imóvel em conformidade com o Código Florestal nas bases analisadas.</div>' if not all_pend else f'<table width="100%" cellpadding="0" cellspacing="0">{pend_rows}</table>'}
   </td></tr>

   <!-- Resumo Técnico -->
   <tr><td style="background:#fff;padding:0 32px 28px;border:1px solid #e0e0e0;border-top:0;">
     <div style="font-size:11px;font-weight:700;color:#666;letter-spacing:1px;text-transform:uppercase;margin-bottom:12px;">Resumo Técnico — OEMA</div>
     <div style="font-size:13px;line-height:1.7;color:#333;background:#f9fdf9;border-left:3px solid #4caf50;padding:14px 16px;border-radius:0 6px 6px 0;">
       {resumo_tecnico or '<em style="color:#999">Resumo técnico não disponível.</em>'}
     </div>
   </td></tr>

   <!-- Footer -->
   <tr><td style="background:#0a2e0a;border-radius:0 0 12px 12px;padding:18px 32px;">
     <div style="font-size:10px;color:#81c784;line-height:1.6;">
       CAR Doutor — Análise Automática do Cadastro Ambiental Rural — ENAP Hackathon 2026<br>
       Este laudo é gerado automaticamente e não substitui vistoria de campo nem laudo técnico assinado por profissional habilitado.
     </div>
   </td></tr>

  </table>
 </td></tr>
</table>
</body>
</html>"""


class SendReportRequest(__import__('pydantic').BaseModel):
    email: str
    laudo: dict


def _read_env_var(key: str) -> str:
    """Lê variável do .env diretamente, sem depender de os.environ ou dotenv."""
    import os
    candidates = [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent / ".env",
        Path(os.getcwd()) / ".env",
    ]
    for p in candidates:
        if not p.exists():
            continue
        for raw_line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    # fallback para os.environ
    return os.environ.get(key, "")


@app.post("/send-report")
def send_report(req: SendReportRequest):
    """Envia laudo de conformidade por email via Resend API."""
    api_key  = _read_env_var("RESEND_API_KEY")
    from_addr = _read_env_var("RESEND_FROM") or "CAR Doutor <onboarding@resend.dev>"

    log.info("send_report: api_key presente=%s from=%s", bool(api_key), from_addr)

    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Serviço de email não configurado. Adicione RESEND_API_KEY no arquivo backend/.env",
        )
    laudo     = req.laudo
    car_code  = laudo.get("car_code") or "Geometria manual"
    municipio = laudo.get("municipio") or "MT"
    status_map = {"ok": "Conforme", "atencao": "Atenção", "critico": "Crítico"}
    status_label = status_map.get(laudo.get("status_geral", "atencao"), "Atenção")

    html_body = _build_email_html(laudo)

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": from_addr,
                "to": [req.email],
                "subject": f"[CAR Doutor] Laudo {status_label} — {car_code} · {municipio}",
                "html": html_body,
            },
            timeout=15,
        )
        if not resp.ok:
            detail = resp.json().get("message", resp.text)
            raise HTTPException(status_code=502, detail=f"Resend API: {detail}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Erro ao enviar email: {exc}")

    log.info("Email enviado para %s (CAR %s)", req.email, car_code)
    return {"success": True, "message": f"Laudo enviado para {req.email}"}


@app.post("/analyze", response_model=LaudoResult)
def analyze_property(request: AnalyseRequest):
    """
    Analisa um imóvel rural e retorna laudo com pendências do CAR.

    Forneça um dos dois:
    - `car_code`: código CAR no formato MT-XXXXXXX-...
    - `geometry`: GeoJSON Polygon/MultiPolygon do imóvel
    """
    if not request.car_code and not request.geometry:
        raise HTTPException(
            status_code=422,
            detail="Forneça car_code ou geometry.",
        )

    try:
        log.info("Iniciando análise: car_code=%s, geometry=%s",
                 request.car_code, "sim" if request.geometry else "nao")
        result = analyze(request)
        log.info("Análise concluída: status=%s, pendencias=%d",
                 result.status_geral, result.total_pendencias)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        log.exception("Erro interno na análise")
        raise HTTPException(status_code=500, detail=f"Erro interno: {exc}")
