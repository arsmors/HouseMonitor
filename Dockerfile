FROM node:22-alpine AS frontend
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright XDG_CACHE_HOME=/tmp/cache
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home monitor \
    && mkdir -p /app/data \
    && chown monitor:monitor /app/data
COPY backend ./backend
COPY --from=frontend /app/dist ./dist
USER monitor
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health')" || exit 1
CMD ["python", "-m", "backend.app"]