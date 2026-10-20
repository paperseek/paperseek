# syntax=docker/dockerfile:1

FROM node:22.14.0-alpine AS web-build

WORKDIR /build/web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


FROM python:3.12.13-slim AS wheel-build

COPY --from=ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5 /uv /usr/local/bin/uv

ENV PIP_NO_CACHE_DIR=1
ARG PAPERSEEK_BUILD_COMMIT=unknown
ARG PAPERSEEK_BUILD_DIRTY=false
ENV PAPERSEEK_BUILD_COMMIT=${PAPERSEEK_BUILD_COMMIT} \
    PAPERSEEK_BUILD_DIRTY=${PAPERSEEK_BUILD_DIRTY}

WORKDIR /build/backend

COPY backend/pyproject.toml backend/uv.lock backend/README.md backend/LICENSE backend/THIRD_PARTY_NOTICES.md ./
RUN uv export --locked --no-dev --no-emit-project --format requirements.txt --output-file /requirements.txt

COPY backend/setup.py ./
COPY backend/scripts/generate_openapi_types.py ./scripts/generate_openapi_types.py
COPY backend/src ./src
COPY --from=web-build /build/web/dist ./src/paperseek_community/static
COPY --from=web-build /build/web/src /build/web/src
COPY --from=web-build /build/web/public /build/web/public
COPY --from=web-build /build/web/package.json /build/web/package-lock.json /build/web/

RUN uv build --wheel --out-dir /wheels


FROM python:3.12.13-slim AS runtime

LABEL org.opencontainers.image.title="PaperSeek Community" \
      org.opencontainers.image.version="1.0.0-beta4" \
      org.opencontainers.image.source="https://github.com/paperseek/paperseek" \
      org.opencontainers.image.ref.name="ghcr.io/paperseek/paperseek:1.0.0-beta4"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPERSEEK_DATABASE=/var/lib/paperseek/paperseek.db

RUN useradd --create-home --shell /bin/sh paperseek \
    && install -d -o paperseek -g paperseek /var/lib/paperseek

ARG PIP_INDEX_URL=https://pypi.org/simple
COPY --from=wheel-build /requirements.txt /requirements.txt
RUN python -m pip install --require-hashes -r /requirements.txt \
    && rm /requirements.txt

COPY --from=wheel-build /wheels /wheels
RUN python -m pip install --no-deps /wheels/paperseek-*.whl \
    && rm -rf /wheels

USER paperseek

VOLUME ["/var/lib/paperseek"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).read()" || exit 1

ENTRYPOINT ["paperseek"]
CMD ["serve", "--database", "/var/lib/paperseek/paperseek.db", "--host", "0.0.0.0", "--port", "8765"]
