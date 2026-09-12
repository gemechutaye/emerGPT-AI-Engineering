FROM node:22-bookworm-slim AS web
WORKDIR /build
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8017 COOKIE_SECURE=true
ENV TIKTOKEN_CACHE_DIR=/app/.local/tokenizers
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.24 && useradd --create-home --uid 10001 emer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps/api ./apps/api
RUN uv sync --frozen --no-dev
# Tokenization is required even with reranking disabled. Cache it during build,
# before the image loses network access or starts serving user requests.
RUN uv run --no-sync python -c "from emer.domain.chunking import tokenizer; tokenizer()"
ARG PREPARE_RERANKER=false
RUN if [ "$PREPARE_RERANKER" = "true" ]; then uv run --no-sync emer prepare-retrieval; fi
COPY alembic.ini ./
COPY config ./config
COPY EMER_AI_TakeHome_Knowledge_Corpus.jsonl EMER_AI_TakeHome_Data_README.txt ./
COPY --from=web /build/dist ./apps/web/dist
COPY deployment/start.sh deployment/check_index.py ./deployment/
RUN mkdir -p .local && chown -R emer:emer /app
USER emer
EXPOSE 8017
CMD ["bash", "deployment/start.sh"]
