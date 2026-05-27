FROM python:3.12-slim

# GDAL + GEOS para geopandas/shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # s3fs necessário para leitura direta de parquet no S3
    && pip install --no-cache-dir s3fs

COPY app/ ./app/

EXPOSE 8000

# DATA_SOURCE=s3 em produção — sem volume de dados necessário
ENV DATA_SOURCE=s3

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
