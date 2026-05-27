# CAR Doutor — Backend

API REST em FastAPI que analisa inconsistências do **Cadastro Ambiental Rural (CAR)**, cruzando dados do SICAR com bases abertas (PRODES, DETER, FUNAI, CNUC, SoilGrids).

---

## Pré-requisitos

| Ferramenta | Versão mínima |
|---|---|
| Python | 3.11+ |
| pip | qualquer |
| GDAL / GEOS | instalado no sistema (ver abaixo) |

### Instalar GDAL no Windows
```powershell
# Opção 1 — OSGeo4W (recomendado)
# Baixar em: https://trac.osgeo.org/osgeo4w/

# Opção 2 — conda
conda install -c conda-forge gdal geopandas
```

---

## Instalação local

```bash
# 1. Clone o repositório
git clone https://github.com/mrclimadev/car-doutor-backend.git
cd car-doutor-backend

# 2. Crie e ative o virtualenv
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 3. Instale as dependências
pip install -r requirements.txt

# 4. Configure as variáveis de ambiente
copy .env.example .env
# Edite o .env com seus valores reais (ver seção abaixo)
```

---

## Configuração (`.env`)

```env
# Fonte dos dados geoespaciais
DATA_SOURCE=local          # "local" para dev | "s3" para produção

# S3 (obrigatório quando DATA_SOURCE=s3)
S3_BUCKET=car-doutor-geodata
S3_PREFIX=bases/mt/vectors
AWS_REGION=us-east-1

# Chave da API Anthropic (opcional — sem ela usa resumos por template)
ANTHROPIC_API_KEY=sk-ant-api03-...

# Porta
PORT=8000
```

---

## Dados geoespaciais (DATA_SOURCE=local)

Os parquets processados **não estão no repositório** (arquivos binários grandes).
Você precisa de uma das opções abaixo:

### Opção A — Baixar do S3 (se tiver acesso ao bucket)
```bash
pip install s3fs
python -c "
import s3fs, pathlib
fs = s3fs.S3FileSystem()
dest = pathlib.Path('../data-pipeline/processed/vectors')
dest.mkdir(parents=True, exist_ok=True)
for f in fs.ls('car-doutor-geodata/bases/mt/vectors/'):
    fs.get(f, str(dest / pathlib.Path(f).name))
"
```

### Opção B — Gerar localmente (ver repo `car-doutor-pipeline`)
```bash
cd ../car-doutor-pipeline
python convert_to_parquet.py
```

Os parquets devem ficar em:
```
../data-pipeline/processed/vectors/
  sicar_area_imovel.parquet
  sicar_apps.parquet
  sicar_hidrografia.parquet
  sicar_vegetacao_nativa.parquet
  sicar_area_consolidada.parquet
  sicar_uso_restrito.parquet
```

---

## Executar

```bash
uvicorn app.main:app --reload --port 8000
```

Acesse: `http://localhost:8000/docs` para a documentação interativa (Swagger UI).

### Endpoints principais

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/health` | Status da API |
| `GET` | `/layers` | Camadas disponíveis localmente |
| `POST` | `/analyze` | Analisa um imóvel (CAR code ou geometria) |
| `GET` | `/stats` | Estatísticas agregadas do SICAR |
| `GET` | `/stats/map` | GeoJSON de centroides filtrados |
| `GET` | `/imovel/{car_code}` | Dados cadastrais brutos do SICAR |
| `GET` | `/map-layer` | Proxy WFS → GeoJSON (PRODES/DETER/TI/UC) |

---

## Executar com Docker

```bash
# Construir imagem
docker build -t car-doutor-backend .

# Rodar (modo S3 — sem volume de dados)
docker run -p 8000:8000 \
  -e DATA_SOURCE=s3 \
  -e S3_BUCKET=car-doutor-geodata \
  -e AWS_REGION=us-east-1 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  car-doutor-backend

# Rodar (modo local — com volume de dados)
docker run -p 8000:8000 \
  -e DATA_SOURCE=local \
  -v /caminho/para/data-pipeline:/data-pipeline \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  car-doutor-backend
```

---

## Estrutura

```
app/
  main.py            # FastAPI app e endpoints
  models.py          # Modelos Pydantic (request/response)
  engine/
    analyzer.py      # Orquestrador da análise
    app_calculator.py   # Verificação de APP
    rl_checker.py       # Verificação de Reserva Legal
    deforest_checker.py # PRODES / DETER
    restriction_checker.py # TI / UC
    soil_checker.py     # SoilGrids/ISRIC
    llm.py              # Resumos via Claude (Anthropic)
  data/
    loader.py        # Carga de parquets (local ou S3)
```

---

## Repositórios relacionados

- **Frontend:** https://github.com/mrclimadev/car-doutor-frontend
- **Pipeline de dados:** https://github.com/mrclimadev/car-doutor-pipeline
