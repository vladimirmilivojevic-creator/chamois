# syntax=docker/dockerfile:1

# --- stage 1: build the Rust scheduler into a Python wheel (PyO3 + maturin) ---
FROM python:3.12-slim AS scheduler-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"
WORKDIR /build
COPY scheduler/ scheduler/
RUN pip wheel --no-cache-dir ./scheduler --wheel-dir /wheels

# --- stage 2: runtime image, no compiler inside ---
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHAMOIS_REQUIRE_RUST=1 \
    PORT=8000
WORKDIR /app
COPY --from=scheduler-build /wheels /wheels
COPY pyproject.toml README.md ./
COPY chamois/ chamois/
RUN pip install /wheels/*.whl ".[qiskit]" && rm -rf /wheels
RUN useradd --create-home chamois
USER chamois
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
CMD ["sh", "-c", "uvicorn chamois.api:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers"]
