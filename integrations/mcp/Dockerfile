# syntax=docker/dockerfile:1

FROM python:3.12.13-slim AS wheel-build

ENV PIP_NO_CACHE_DIR=1
ARG PAPERSEEK_BUILD_COMMIT=unknown
ARG PAPERSEEK_BUILD_DIRTY=false
ENV PAPERSEEK_BUILD_COMMIT=${PAPERSEEK_BUILD_COMMIT} \
    PAPERSEEK_BUILD_DIRTY=${PAPERSEEK_BUILD_DIRTY}

WORKDIR /build

COPY backend ./backend
COPY integrations/mcp ./integrations/mcp
COPY web/package.json web/package-lock.json ./web/
COPY web/src ./web/src
COPY web/public ./web/public

RUN python -m pip wheel --wheel-dir /wheels ./backend \
    && python -m pip wheel --wheel-dir /wheels --find-links /wheels ./integrations/mcp \
    && cd /wheels \
    && sha256sum *.whl > SHA256SUMS


FROM python:3.12.13-slim AS runtime

LABEL io.modelcontextprotocol.server.name="io.github.paperseek/paperseek" \
      org.opencontainers.image.title="PaperSeek MCP Server" \
      org.opencontainers.image.version="1.0.0-beta3" \
      org.opencontainers.image.source="https://github.com/paperseek/paperseek" \
      org.opencontainers.image.ref.name="ghcr.io/paperseek/paperseek-mcp:1.0.0-beta3"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PAPERSEEK_DATABASE=/data/paperseek.db \
    PAPERSEEK_MCP_PUBLIC_URL="" \
    PAPERSEEK_MCP_ALLOWED_HOSTS="" \
    PAPERSEEK_MCP_ALLOWED_ORIGINS=""

RUN useradd --create-home --shell /bin/sh paperseek \
    && install -d -o paperseek -g paperseek /data

COPY --from=wheel-build /wheels /wheels
RUN cd /wheels \
    && sha256sum --check SHA256SUMS \
    && python -m pip install --no-index --find-links /wheels paperseek-mcp==1.0.0b3 \
    && rm -rf /wheels

USER paperseek

VOLUME ["/data"]
EXPOSE 8766

ENTRYPOINT ["paperseek-mcp"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8766", "--path", "/mcp"]
