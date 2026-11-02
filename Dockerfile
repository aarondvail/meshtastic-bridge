# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.15.2

# ---------------------------------------------------------------------------
# Build stage: compile wheels for packages with no prebuilt 3.15 wheel
# (pyyaml, and secp256k1 for nostr, which builds a bundled libsecp256k1)
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config libffi-dev \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN pip install -U pip \
    && pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BRIDGE_CONFIG=/code/config.yaml

# Non-root user; dialout grants access to USB serial radios
RUN useradd --system --uid 10001 --no-create-home --groups dialout bridge

WORKDIR /code

COPY requirements.txt .
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels -r requirements.txt

COPY main.py plugins.py ./

USER bridge

# config.yaml is mounted at /code/config.yaml at run time
CMD ["python", "main.py"]
