"""
Análise de solo via SoilGrids ISRIC (REST) + IBGE tipos de solo (WFS).
Enriquece o laudo com dados pedológicos do imóvel.
"""

import logging
import urllib3
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

_SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
_IBGE_SOLOS_URL = "https://geoservicos.ibge.gov.br/geoserver/CGEO/wfs"
_UA = "Mozilla/5.0 (compatible; CAR-Doutor/1.0)"

_PROPS = ["phh2o", "soc", "clay", "sand", "silt", "nitrogen", "bdod", "cec"]
_D_FACTORS = {"phh2o": 10, "soc": 10, "clay": 10, "sand": 10,
               "silt": 10, "nitrogen": 100, "bdod": 100, "cec": 10}

# Interpretações simples para o laudo
_PH_CLASS = [
    (4.5, "Muito ácido"),
    (5.5, "Ácido"),
    (6.5, "Levemente ácido"),
    (7.5, "Neutro a alcalino"),
    (14,  "Alcalino"),
]

_TEXTURE_CLASS = [
    # (clay %, sand %) → textura
    (60, None, "Argiloso"),
    (35, None, "Franco-argiloso"),
    (None, 70, "Arenoso"),
    (None, None, "Franco"),
]


def _ph_class(ph: float) -> str:
    for limit, label in _PH_CLASS:
        if ph <= limit:
            return label
    return "Alcalino"


def _texture(clay: float, sand: float) -> str:
    if clay >= 60:
        return "Argiloso"
    if clay >= 35:
        return "Franco-argiloso"
    if sand >= 70:
        return "Arenoso"
    if clay >= 20:
        return "Franco-argiloso leve"
    return "Franco / Médio"


def _erosion_risk(clay: float, sand: float, slope_pct: float = 0) -> str:
    """Risco erosão simplificado (sem DEM usa textura como proxy)."""
    if sand >= 70:
        return "Alto"
    if sand >= 50 and clay < 25:
        return "Médio-alto"
    if clay >= 40:
        return "Baixo"
    return "Médio"


def fetch_soilgrids(lon: float, lat: float) -> dict:
    """Retorna dict com propriedades do solo para o ponto (centróide do imóvel)."""
    try:
        resp = requests.get(
            _SOILGRIDS_URL,
            params={
                "lon": round(lon, 5),
                "lat": round(lat, 5),
                "property": _PROPS,
                "depth": ["0-5cm", "5-15cm"],
                "value": ["mean"],
            },
            headers={"User-Agent": _UA},
            timeout=12,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("SoilGrids erro: %s", exc)
        return {}

    result = {}
    for layer in data.get("properties", {}).get("layers", []):
        name = layer["name"]
        d = _D_FACTORS.get(name, 1)
        depths = {
            dep["label"]: (dep["values"]["mean"] / d if dep["values"].get("mean") is not None else None)
            for dep in layer.get("depths", [])
        }
        result[name] = depths
    return result


def fetch_ibge_soil_type(lon: float, lat: float) -> str | None:
    """Retorna a classe pedológica IBGE para o ponto."""
    try:
        resp = requests.get(
            _IBGE_SOLOS_URL,
            params={
                "service": "WFS",
                "version": "1.0.0",
                "request": "GetFeature",
                "typeName": "CGEO:andb2022_02_tipos_de_solo",
                "outputFormat": "application/json",
                "maxFeatures": "1",
                "bbox": f"{lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01},EPSG:4326",
            },
            headers={"User-Agent": _UA},
            timeout=10,
            verify=False,
        )
        fc = resp.json()
        features = fc.get("features", [])
        if features:
            props = features[0].get("properties", {})
            # legenda_2 has soil class name
            return props.get("legenda_2") or props.get("an021901")
    except Exception as exc:
        log.warning("IBGE solos WFS erro: %s", exc)
    return None


def analyze_soil(property_geom) -> dict:
    """
    Retorna dict com análise completa do solo para o imóvel.
    Usa centróide do polígono como ponto de consulta.
    """
    centroid = property_geom.centroid
    lon, lat = centroid.x, centroid.y

    sg = fetch_soilgrids(lon, lat)
    ibge_class = fetch_ibge_soil_type(lon, lat)

    if not sg:
        return {"disponivel": False}

    ph     = sg.get("phh2o", {}).get("0-5cm")
    clay   = sg.get("clay",  {}).get("0-5cm")
    sand   = sg.get("sand",  {}).get("0-5cm")
    soc    = sg.get("soc",   {}).get("0-5cm")   # carbono orgânico g/kg
    n      = sg.get("nitrogen", {}).get("0-5cm")
    cec    = sg.get("cec",   {}).get("0-5cm")

    result = {
        "disponivel": True,
        "ph": ph,
        "ph_classe": _ph_class(ph) if ph else None,
        "argila_pct": clay,
        "areia_pct": sand,
        "silte_pct": sg.get("silt", {}).get("0-5cm"),
        "carbono_organico_g_kg": soc,
        "nitrogenio_g_kg": n,
        "cec_cmol_kg": cec,
        "densidade_bulk_kg_dm3": sg.get("bdod", {}).get("0-5cm"),
        "textura": _texture(clay, sand) if (clay and sand) else None,
        "risco_erosao": _erosion_risk(clay, sand) if (clay and sand) else None,
        "classe_ibge": ibge_class,
        # Carbon stock potential (tC/ha) = SOC(g/kg) * bulk(kg/dm³) * depth(m) * 10
        "estoque_carbono_tC_ha": round(soc * sg.get("bdod", {}).get("0-5cm", 1.2) * 0.05 * 10, 1) if soc else None,
    }
    return result
