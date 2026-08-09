# Containerized Python for this project.
#
# Exists because a host pyenv shim intercepts `.python-version` (pinned 3.14)
# before uv resolves an interpreter, breaking `uv run` locally. This image
# defaults to 3.13 - the version CI installs - so a green container run means
# the same thing a green CI run does.
#
#   docker compose build
#   docker compose run --rm app pytest tests/ -q
#
# Override the interpreter to reproduce the host pin (tagged separately, so
# the 3.13 image is not clobbered):
#   PYTHON_VERSION=3.14 docker compose build
#
# Two targets share the dependency-layer base below: `test` (default, what
# compose.yaml/CI build) and `dashboard` (the deploy/ manifests' image,
# built explicitly with --target dashboard). `test` stays last so the
# implicit default target is unchanged.
ARG PYTHON_VERSION=3.13
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base

# The venv lives outside /app so a bind-mounted source tree can't shadow it
# with the host's own .venv (which is built against a different interpreter).
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependency layer: only busts when the lockfile changes, not on every edit.
COPY pyproject.toml uv.lock ./

FROM base AS dashboard
RUN uv sync --extra dashboard --frozen --no-install-project
COPY . .

# data/ is personal financial data - hostPath/bind-mounted at runtime, never
# baked in.
ENV DATA_DIR=/app/data

EXPOSE 8501
CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501"]

FROM base AS test
RUN uv sync --extra dev --frozen --no-install-project
COPY . .

# data/ is personal financial data - bind-mounted at runtime, never baked in.
ENV DATA_DIR=/app/data

CMD ["pytest", "tests/", "-q"]
