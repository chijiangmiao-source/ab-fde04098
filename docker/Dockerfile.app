# syntax=docker/dockerfile:1
# Multi-stage image: build the frontend bundle with Node, serve everything
# from a small Python runtime.

FROM node:20-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npx tsc --noEmit && npm run build && test -s static/app.js

FROM python:3.11-slim-bookworm AS runtime
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend /frontend/static ./static
COPY scripts/healthcheck.py /usr/local/bin/healthcheck.py

ENV DATA_DIR=/data \
    STATIC_DIR=/app/static \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "/usr/local/bin/healthcheck.py"]

# waitress honors HOST/PORT via app.wsgi config; shell form expands env vars.
CMD waitress-serve --host="${HOST}" --port="${PORT}" app.wsgi:app
