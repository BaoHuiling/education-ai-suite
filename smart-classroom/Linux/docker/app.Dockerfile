# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#

FROM node:22-bookworm-slim AS ui-builder

ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""

WORKDIR /ui

COPY ui/package.json ui/package-lock.json ./
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" npm ci

ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

COPY ui/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1


ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""

COPY docker/system_requirements.txt ./system_requirements.txt
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
    http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    apt-get update && apt-get install -y --no-install-recommends \
        $(grep -v '^#' system_requirements.txt | xargs) \
    && rm -rf /var/lib/apt/lists/*

# Optional corporate TLS-inspection CA (NOT baked into the repo or the image
# by default -- this RUN step is a no-op unless a build invoker explicitly
# supplies one). Some corporate networks terminate/re-sign outbound HTTPS
# through an SSL-inspecting proxy; the CA that re-signs that traffic is not
# in Debian's default trust store, which can make the curl calls below fail
# with "unable to get local issuer certificate". For such environments, pass
# the environment's own approved root CA (PEM) at build time via a BuildKit
# secret -- it is never written into the Dockerfile, never committed to the
# repo, and only read for the duration of this RUN step:
#   docker build --secret id=corporate_ca,src=/path/to/corporate-ca.pem ...
# TLS verification is never disabled (no curl -k/--insecure); if no secret
# is supplied, this is a pure no-op and behaves exactly as before.
#
# IMPORTANT: the CA is installed and the driver downloads below both happen
# in this SAME RUN step, and the CA file + its trust-store entry are removed
# again (via `update-ca-certificates --fresh`) before this step/layer
# completes. A later `RUN rm` in a subsequent layer would still leave the
# certificate's bytes recoverable in image history, so cleanup must happen
# within this one layer for the CA to be genuinely build-only.
RUN --mount=type=secret,id=corporate_ca,required=false \
    export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" no_proxy="$NO_PROXY" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl libnuma1 libtbb12 ocl-icd-libopencl1 \
    && CORPORATE_CA_INSTALLED=0 \
    && if [ -s /run/secrets/corporate_ca ]; then \
        cp /run/secrets/corporate_ca /usr/local/share/ca-certificates/corporate-ca.crt \
        && update-ca-certificates \
        && CORPORATE_CA_INSTALLED=1; \
    fi \
    && mkdir -p /tmp/intel-gpu-runtime \
    && cd /tmp/intel-gpu-runtime \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://github.com/intel/compute-runtime/releases/download/26.18.38308.1/libze-intel-gpu1_26.18.38308.1-0_amd64.deb" \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://github.com/intel/compute-runtime/releases/download/26.18.38308.1/intel-opencl-icd_26.18.38308.1-0_amd64.deb" \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://github.com/intel/compute-runtime/releases/download/26.18.38308.1/libigdgmm12_22.10.0_amd64.deb" \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://github.com/intel/intel-graphics-compiler/releases/download/v2.34.4/intel-igc-core-2_2.34.4+21428_amd64.deb" \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://github.com/intel/intel-graphics-compiler/releases/download/v2.34.4/intel-igc-opencl-2_2.34.4+21428_amd64.deb" \
    && curl -fL --retry 5 --retry-delay 5 --retry-all-errors -O \
        "https://ppa.launchpadcontent.net/kobuk-team/intel-graphics/ubuntu/pool/main/l/level-zero-loader/libze1_1.32.0-1~24.04~ppa1_amd64.deb" \
    && printf '%s\n' \
        '12b8254e6d3415c32cee9cd13943030b991d91212445c79fe1cc27176a72eca4  libze-intel-gpu1_26.18.38308.1-0_amd64.deb' \
        'b2d0c924e56b3f9e5837774d68b0c67461b8633035d93ca18b1a8e3e5ead15fa  intel-opencl-icd_26.18.38308.1-0_amd64.deb' \
        '6031a63d6e8a12ce61c14efc15f2c8e727061286e3820b8594e6d00615e04d54  libigdgmm12_22.10.0_amd64.deb' \
        '0ce1e715ec5bf30b1304b42acfbda34cc514a0f616dd48c6a31973c4075d8d09  intel-igc-core-2_2.34.4+21428_amd64.deb' \
        '09a71e8b6ad432ed0511b09fa4f580c1b44534a60ccb0545212507bf67dc0d1b  intel-igc-opencl-2_2.34.4+21428_amd64.deb' \
        'd4001dc474d3cb74ded50a449b9f646cfdfbb31e69e70b8af38f2d18273dfce1  libze1_1.32.0-1~24.04~ppa1_amd64.deb' \
        | sha256sum --check - \
    && dpkg -i ./*.deb \
    && ldconfig \
    && test -e /usr/lib/x86_64-linux-gnu/libOpenCL.so.1 \
    && test -e /usr/lib/x86_64-linux-gnu/libze_loader.so.1 \
    && cd / \
    && rm -rf /tmp/intel-gpu-runtime \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && if [ "$CORPORATE_CA_INSTALLED" = "1" ]; then \
        rm -f /usr/local/share/ca-certificates/corporate-ca.crt \
        && update-ca-certificates --fresh; \
    fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" NO_PROXY="$NO_PROXY" \
    pip install --no-cache-dir -r requirements.txt

# Non-root runtime user.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY main.py ./main.py
COPY pipeline.py ./pipeline.py
COPY monitoring/ ./monitoring/
COPY --from=ui-builder /ui/dist ./ui/dist

COPY utils/ ./utils/
COPY components/ ./components/
COPY content_search/ ./content_search/
COPY api/ ./api/
COPY model_manager/ ./model_manager/
COPY services/ ./services/
COPY dto/ ./dto/
COPY constants/ ./constants/
COPY prompts/ ./prompts/
COPY config.yaml runtime_config.yaml ./

RUN mkdir -p /app/storage/content-search \
    && ln -s /app/storage/content-search /app/content_search/data \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]