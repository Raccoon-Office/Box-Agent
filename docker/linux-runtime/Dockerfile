# syntax=docker/dockerfile:1.7

ARG TARGETARCH
FROM quay.io/pypa/manylinux_2_28_x86_64:latest AS base-amd64
FROM quay.io/pypa/manylinux_2_28_aarch64:latest AS base-arm64
FROM base-${TARGETARCH} AS build

ARG TARGETARCH
ARG BOX_AGENT_VERSION

WORKDIR /workspace

COPY . /workspace

RUN --mount=type=cache,target=/root/.cache/pip \
    test -n "${BOX_AGENT_VERSION}" && \
    /usr/bin/python3 -m ensurepip && \
    /usr/bin/python3 -m pip install --disable-pip-version-check --timeout 120 --retries 5 . "pyinstaller>=6.0.0" "pydantic==2.12.3" && \
    /usr/bin/python3 -c 'import openai; assert openai.__version__ == "2.8.0", openai.__version__'

RUN --mount=type=cache,target=/root/.cache/pip \
    case "${TARGETARCH}" in \
      amd64) box_arch=x64 ;; \
      arm64) box_arch=arm64 ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    /usr/bin/python3 scripts/build_runtime.py \
      --version "${BOX_AGENT_VERSION}" \
      --output /workspace/dist/runtime \
      --target "linux-${box_arch}"

RUN case "${TARGETARCH}" in \
      amd64) box_arch=x64 ;; \
      arm64) box_arch=arm64 ;; \
      *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    /usr/bin/python3 scripts/verify_linux_runtime.py \
      "/workspace/dist/runtime/box-agent-runtime-v${BOX_AGENT_VERSION}-linux-${box_arch}.tar.gz" \
      --arch "${box_arch}" && \
    mkdir -p /artifact && \
    cp "/workspace/dist/runtime/box-agent-runtime-v${BOX_AGENT_VERSION}-linux-${box_arch}.tar.gz" \
       "/workspace/dist/runtime/box-agent-runtime-v${BOX_AGENT_VERSION}-linux-${box_arch}.tar.gz.sha256" \
       /artifact/

FROM scratch AS artifact
COPY --from=build /artifact/ /
