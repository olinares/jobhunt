# The hosted MCP server (jobhunt-remote) for Cloud Run. Built and deployed by
# .github/workflows/deploy.yml; one-time setup in docs/deploy.md.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# .dockerignore lets in only pyproject.toml, src/ and config/.
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && rm -rf src build

# roles.yaml and seeds.yaml are read from --root.
COPY config ./config

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin jobhunt
USER jobhunt

# Cloud Run sets PORT (8080 unless told otherwise).
ENV PORT=8080
EXPOSE 8080
CMD ["jobhunt-remote", "--root", "/app"]
