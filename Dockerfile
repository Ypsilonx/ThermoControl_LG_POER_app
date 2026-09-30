# ============================================================
# Dockerfile – LG ThinQ Klimatizace (webový server)
# Multi-stage build:
#   1) builder  – sestaví .venv vč. kompilace C rozšíření (gcc)
#   2) runtime  – štíhlý obraz BEZ gcc/curl (menší plocha útoku)
# Cílové prostředí: Linux (Docker), Python 3.12-slim.
# Aplikace běží v režimu --mode web (výchozí).
# ============================================================

# ------------------------------------------------------------
# Stage 1: builder – instalace závislostí do /app/.venv
# ------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# gcc je potřeba pro sestavení C rozšíření (awscrt, cryptography).
# curl je potřeba pro instalaci UV. V runtime obrazu už nejsou.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Instalace UV (správce závislostí).
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

# Kopírujeme jen soubory pro instalaci závislostí (efektivní Docker cache).
# --frozen: přesné verze z uv.lock; --no-dev: bez vývojových závislostí;
# --no-editable: neinstaluje projekt samotný (src/ se kopíruje v runtime).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

# ------------------------------------------------------------
# Stage 2: runtime – štíhlý běhový obraz
# ------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="LG Klimatizace" \
      org.opencontainers.image.description="Webové rozhraní pro ovládání LG ThinQ klimatizace"

WORKDIR /app

# Časová zóna: slim obraz nemá tzdata, takže TZ=Europe/Prague z docker-compose
# by se tiše ignorovala a místní čas by byl UTC (posunutý HAND plánovač,
# hranice dnů v historii a v exportu).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Přebereme hotové virtuální prostředí z builderu.
# Cesta /app/.venv musí být shodná s builderem (uv používá absolutní cesty).
COPY --from=builder /app/.venv /app/.venv

# Zdrojový kód aplikace.
COPY src/ ./src/

# Datový adresář (skutečná data se připojují jako volume).
RUN mkdir -p data

# Bezpečnost: běh pod neprivilegovaným uživatelem.
RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Aktivace venv přes PATH (není potřeba absolutní cesta v CMD).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Port, na kterém uvicorn naslouchá.
EXPOSE 8000

# Health check – /health je veřejný (mimo autentizaci).
HEALTHCHECK --interval=30s \
            --timeout=10s \
            --start-period=20s \
            --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]

# sys.path setup je v src/main.py; --mode web spustí uvicorn programaticky.
CMD ["python", "src/main.py", "--mode", "web"]
