# Dream Job - one image: FastAPI backend + the built React SPA.
#
# Two stages, so Node never ships in the runtime image:
#   1. "frontend" builds frontend/dist with Vite (npm ci = exact lockfile).
#   2. "runtime" installs the Python deps and copies the backend + dist.
# The backend already serves frontend/dist when it exists (main.py), so the
# whole app is one container on port 8000.
#
#   docker compose up --build      -> http://localhost:8000

# --- Stage 1: build the React frontend ---------------------------------------
# Vite 8 needs Node >= 22.12 (same major as CI).
FROM node:22-slim AS frontend
WORKDIR /app/frontend
# Lockfile first: the npm layer is cached until package*.json change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime ---------------------------------------------------
# 3.11 = the floor pyproject allows and the version CI tests on.
FROM python:3.11-slim AS runtime

# No .pyc clutter, unbuffered logs (docker logs shows them live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Deps before code: this layer is reused until requirements.txt changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY pyproject.toml ./
COPY --from=frontend /app/frontend/dist ./frontend/dist

# Non-root user: a compromised process cannot modify the image files.
# /app/data (SQLite DB, documents) and /app/logs (rotating app logs) are the
# only writable places; compose mounts /app/data as a volume.
RUN useradd --create-home --uid 1000 dreamjob \
    && mkdir -p /app/data /app/logs \
    && chown -R dreamjob:dreamjob /app/data /app/logs
USER dreamjob

EXPOSE 8000

# Healthcheck on the existing /api/health route; python is already in the image,
# so no curl install is needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Apply DB migrations first (same as scripts/dev.sh), then serve.
# 0.0.0.0 inside the container is required for Docker's port mapping; the
# compose file publishes it on 127.0.0.1 only, so it stays local to the PC.
CMD ["sh", "-c", "python -m dreamjob.db.migrator && exec python -m uvicorn dreamjob.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10"]
