FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # cdr_report_app (Demo data source, PDF engine) is kept in this project under
    # vendor/report_app and copied in below - no Report_App folder on the server.
    SHERLOCKS_REPORT_APP_SRC=/app/vendor/report_app/src \
    PYTHONPATH=/app/vendor/report_app/src

WORKDIR /app

# Fonts back the reused bilingual (Urdu/English) PDF engine; curl is for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, for layer caching. Sherlocks' own deps come from pyproject;
# these are the extra runtime libraries cdr_report_app needs for the providers,
# phone/CNIC helpers and PDF/Excel rendering that Sherlocks imports from it.
COPY pyproject.toml README.md ./
COPY docker/report-app-deps.txt ./docker/report-app-deps.txt
RUN pip install --upgrade pip \
    && pip install -r docker/report-app-deps.txt \
    && pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9" \
       "pydantic>=2.9" "SQLAlchemy>=2.0.30" "psycopg2-binary>=2.9.9" "alembic>=1.13" \
       "PyYAML>=6.0" "python-dotenv>=1.0" "requests>=2.32" "httpx>=0.27" "fpdf2>=2.8" \
       "pypdf>=5.0" "matplotlib>=3.9" "networkx>=3.3" "rapidfuzz>=3.9" \
       "arabic-reshaper>=3.0" "python-bidi>=0.6" "Pillow>=10.0"

# OSINT: the OpenOSINT tool library and the pip-installable binaries it shells out to
# (username search across sites, email-registration checks). phoneinfoga is a Go binary
# and is not installed; phone OSINT reports it as unavailable.
RUN pip install "openosint>=2.27.0" "sherlock-project>=0.15" "holehe>=1.61" "social-analyzer>=0.45"

# The application.
COPY src/ ./src/
COPY configs/ ./configs/
COPY alembic.ini ./alembic.ini
RUN pip install --no-deps -e . \
    && mkdir -p output logs output/images

COPY vendor/ ./vendor/

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7401
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
