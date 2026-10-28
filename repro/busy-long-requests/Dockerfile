# syntax=docker/dockerfile:1
# Build context is the repository root (see docker-compose.yml).

ARG PYTHON_VERSION=3.12
# `source` builds granian from this checkout; `pypi` installs GRANIAN_VERSION from PyPI.
ARG GRANIAN_FROM=source

FROM python:${PYTHON_VERSION} AS granian-source
# LTO off: the release build otherwise spends most of its time linking, and
# rebuilding after a change to src/ should be quick.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    CARGO_PROFILE_RELEASE_LTO=off \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSfL -o /tmp/rustup-init "https://static.rust-lang.org/rustup/dist/$(uname -m)-unknown-linux-gnu/rustup-init" \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --profile minimal --no-modify-path \
    && rm /tmp/rustup-init
RUN pip install --no-cache-dir maturin
WORKDIR /src
COPY Cargo.toml Cargo.lock build.rs pyproject.toml README.md LICENSE ./
COPY .cargo .cargo
COPY src src
COPY granian granian
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target \
    maturin build --release --out /wheels

FROM python:${PYTHON_VERSION}-slim AS granian-pypi
ARG GRANIAN_VERSION=2.8.5
RUN pip download --no-cache-dir --no-deps --only-binary=:all: --dest /wheels "granian==${GRANIAN_VERSION}"

FROM granian-${GRANIAN_FROM} AS granian

FROM python:${PYTHON_VERSION}-slim AS web
COPY --from=granian /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
WORKDIR /app
COPY repro/busy-long-requests/app.py .

FROM python:${PYTHON_VERSION}-slim AS tools
RUN pip install --no-cache-dir matplotlib
WORKDIR /app
COPY repro/busy-long-requests/loadgen.py repro/busy-long-requests/report.py ./
