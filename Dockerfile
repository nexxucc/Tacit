FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev
ENV TACIT_HOST=0.0.0.0
CMD ["uv", "run", "--no-sync", "tacit-api"]
