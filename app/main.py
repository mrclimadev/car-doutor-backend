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
    return {"status": "ok", "service": "car-doutor"}


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
