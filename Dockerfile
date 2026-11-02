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
COPY requirements.txt requirements-meshtastic.txt ./
# Use `python -m pip` (never bare `pip`) and bootstrap pip if the image lacks it.
# meshtastic declares Requires-Python <3.15. `pip wheel` filters it out even with
# --ignore-requires-python, so its pure-Python wheel is downloaded as if for 3.14.
# Everything else keeps pip's normal checks.
RUN (python -m pip --version || python -m ensurepip --upgrade) \
    && python -m pip install -U pip \
    && python -m pip wheel --wheel-dir /wheels -r requirements.txt \
    && python -m pip download --dest /wheels --no-deps --only-binary=:all: \
        --python-version 3.14 -r requirements-meshtastic.txt

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

COPY requirements.txt requirements-meshtastic.txt ./
RUN --mount=type=bind,from=build,source=/wheels,target=/wheels \
    (python -m pip --version || python -m ensurepip --upgrade) \
    && python -m pip install --no-index --find-links=/wheels -r requirements.txt \
    && python -m pip install --no-index --find-links=/wheels --no-deps \
        --ignore-requires-python -r requirements-meshtastic.txt \
    && python -c "import meshtastic.tcp_interface, meshtastic.serial_interface, paho.mqtt.client, yaml, nostr.event"

COPY main.py plugins.py ./

USER bridge

# config.yaml is mounted at /code/config.yaml at run time
CMD ["python", "main.py"]
