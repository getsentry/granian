# syntax=docker/dockerfile:1
# Build context is this directory. GRANIAN_FROM=source also needs the
# `granian` build context to point at a granian checkout (see docker-compose.yml).

ARG PYTHON_VERSION=3.12
# `pypi` installs GRANIAN_VERSION from PyPI; `source` builds the `granian` context.
ARG GRANIAN_FROM=pypi

FROM python:${PYTHON_VERSION}-slim AS granian-pypi
ARG GRANIAN_VERSION=2.8.5
RUN pip download --no-cache-dir --no-deps --only-binary=:all: --dest /wheels "granian==${GRANIAN_VERSION}"

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
COPY --from=granian Cargo.toml Cargo.lock build.rs pyproject.toml README.md LICENSE ./
COPY --from=granian .cargo .cargo
COPY --from=granian src src
COPY --from=granian granian granian
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/src/target \
    maturin build --release --out /wheels

FROM granian-${GRANIAN_FROM} AS granian-wheel

FROM python:${PYTHON_VERSION}-slim AS web
COPY --from=granian-wheel /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
WORKDIR /app
COPY app.py .

FROM python:${PYTHON_VERSION}-slim AS tools
RUN pip install --no-cache-dir matplotlib
WORKDIR /app
COPY loadgen.py report.py ./

# Config is baked in rather than bind-mounted, so the stack also runs from
# directories the Docker VM doesn't share (e.g. /tmp with Colima).
FROM prom/prometheus:v3.5.0 AS prometheus
COPY prometheus.yml rules.yml /etc/prometheus/
