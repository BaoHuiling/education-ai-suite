# Get Started on Linux

This guide walks you through cloning the repository, configuring defaults, and running the application on Linux using the `setup.sh` script.

## Step 1: Prerequisites

Make sure the following are installed. See [System Requirements](./system-requirements.md) for details.

- Docker Engine and the Docker Compose plugin (your user must be able to run `docker` without `sudo`)
- Git
- Python 3 (`python3`) with `pyyaml` available (used by `setup.sh` helper scripts)
- An Intel GPU and its Linux driver, available at `/dev/dri`. The Content Search reranker requires GPU access; `setup.sh` stops with an error if the GPU device or host `render` group is missing.

> [!NOTE]
> Node.js for the UI build is provisioned by `setup.sh` through `scripts/node-setup.sh`.

## Step 2: Clone Repository

Go to the target directory of your choice and clone the suite.
If you want to clone a specific release branch, replace `main` with the desired tag.
To learn more about partial cloning, see the [Repository Cloning guide](https://docs.openedgeplatform.intel.com/dev/OEP-articles/contribution-guide.html#repository-cloning-partial-cloning).

```bash
git clone --filter=blob:none --branch main https://github.com/open-edge-platform/education-ai-suite.git
cd education-ai-suite/smart-classroom/Linux
```

## Step 3: Configuration

### A. Enable Feature Configuration

Features are enabled or disabled through the `features:` block in `config.yaml`. Only enabled features are initialized at startup. `setup.sh` derives `COMPOSE_PROFILES` in `.env` from this block.

```yaml
features:
  asr:                { enabled: true }   # Speech-to-text transcription
  summary:            { enabled: true }   # AI class summary 
  mindmap:            { enabled: true }   # Mind map generation
  topic_segmentation: { enabled: true }
  video_analytics:    { enabled: true }   # Video analytics
  content_search:     { enabled: true }   # Multimodal search + RAG service (port 9011)
  qa:                 { enabled: true }   # RAG-based Q&A over uploaded materials
```

### B. Default Configuration

Models are set in `config.yaml`. Changing `models.text_gen.vlm_name`, the embedding model, or the content search reranker is picked up by `setup.sh`, which syncs `components/vlm/config/startup-models.yaml` and `startup-models.gpu.yaml`.

```yaml
asr:
  provider: openai            # Supported: openvino, openai, funasr
  name: whisper-small
  device: CPU

text_gen:
  provider: vlm
  vlm_name: Qwen/Qwen3-VL-8B-Instruct
  device: GPU                 # Options: GPU or CPU
  weight_format: int4         # Supported: fp16, int4, int8
  max_new_tokens: 5120
```

### C. Hugging Face Token (Optional)

Speaker diarization (pyannote) and gated models need a Hugging Face token. Request access to the [Pyannote Speaker Diarization Community v1](https://huggingface.co/pyannote/speaker-diarization-community-1) model, create a Read token at [Hugging Face Access Tokens](https://huggingface.co/settings/tokens), then:

1. Set `diarization: true` under `models.asr` in `config.yaml`.
2. Add the token to `.env`:

```bash
HF_TOKEN=hf_your_access_token_here
```

> [!NOTE]
> `setup.sh` creates `.env` from `.env.example` on first run. If you want to set `HF_TOKEN` beforehand, run `cp .env.example .env` first. `setup.sh` warns when diarization is enabled and `HF_TOKEN` is missing.

### D. Docker Image Source (Optional)

The `REGISTRY` flag in `config.yaml` selects where `setup.sh` gets the third-party images:

```yaml
REGISTRY: true   # true: pull from Docker Hub (default); false: build locally
```

| `REGISTRY` | `audio-analyzer`, `model-downloader`, `metrics-collector` | Compose files |
|---|---|---|
| `true` | Pulled from Docker Hub (`intel/audio-analyzer`, `intel/model-download`, `intel/retail-benchmark`) | `docker-compose.yml` |
| `false` | Built locally from the `edge-ai-libraries` and `performance-tools` submodules | `docker-compose.yml` + `docker-compose.local-build.yml` |

The Smart Classroom app and video-analytics images are always built locally. For manual
Compose commands, see [Manual Docker Startup](#manual-docker-startup-optional).

## Step 4: Run the Setup Script

From the `Linux` directory, run:

```bash
chmod +x setup.sh
./setup.sh
```

The script performs the following:

1. Initializes the `edge-ai-libraries` (sparse checkout) and `performance-tools` git submodules.
2. Installs and verifies the Node.js toolchain for the UI.
3. Creates `.env` from `.env.example` if it does not exist.
4. Warns if diarization is enabled without `HF_TOKEN`.
5. Syncs `startup-models.yaml` and `startup-models.gpu.yaml` with `config.yaml`.
6. Derives `COMPOSE_PROFILES` from the enabled features, and sets `STARTUP_MODELS_CONFIG` only when
   `text-gen` is active (so an ASR-only stack's shared `model-downloader` skips the OVMS exports).
7. Requires an Intel GPU (`/dev/dri`), sets `RENDER_GID` from the host's `render` group, and enables the GPU Compose configuration.
8. Validates the compose config, then follows `REGISTRY` in `config.yaml`: pulls the
   Docker Hub images when `true`, or builds them locally when `false`. The app and
   video-analytics images are always built locally. Then starts the stack.
9. Waits for the `smart-classroom-app` container to become healthy and, when search
   features are enabled, for Content Search aggregate readiness. Setup exits with
   an error rather than reporting success if either readiness check times out.
10. Runs `npm install` and starts the UI dev server in the background.

> [!NOTE]
> First-time execution can take a long time (up to about 30 minutes) while models are downloaded and exported. Override the wait time with `HEALTH_TIMEOUT_S`, for example `HEALTH_TIMEOUT_S=3600 ./setup.sh`.

On success you should see:

```text
[setup] ✅ App is healthy: http://localhost:8000/
[setup] ✅ UI: http://localhost:5173/  (logs: .../ui/vite-dev.log)
[setup] ✅ setup complete.
```

## Step 5: Verify and Manage the Application

- Backend: <http://localhost:8000/>
- UI: <http://localhost:5173/> (change with `UI_PORT=<port> ./setup.sh`). If the port is already in use, `setup.sh` uses the next free one and prints the actual URL.

To see which containers `setup.sh` starts, how they depend on each other, and which ports they use, see
[Linux Docker Architecture](../linux-docker-architecture.md).

Check status and logs:

```bash
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f app
docker compose -f docker/docker-compose.yml logs -f model-downloader ovms
```

Stop the services:

```bash
docker compose -f docker/docker-compose.yml down
kill $(cat ui/vite-dev.pid)   # stop the UI dev server
```

> [!IMPORTANT]
> After changing `config.yaml`, rerun `./setup.sh` so that profiles and model lists are regenerated and the stack restarts with the new configuration.

## Manual Docker Startup (Optional)

Use this instead of `./setup.sh` to run the Compose commands yourself. Complete
Steps 1–3 first. The reranker requires an Intel GPU at `/dev/dri` and a host
`render` group. From `smart-classroom/Linux`, configure GPU permissions and start
the stack:

```bash
test -d /dev/dri || { echo "Required GPU device /dev/dri not found"; exit 1; }
export RENDER_GID="$(getent group render | cut -d: -f3)"
test -n "$RENDER_GID" || { echo "Required render group not found"; exit 1; }
cp -n .env.example .env
python3 -m utils.compose_profiles --write-env
python3 -m utils.sync_startup_models --write
docker compose --env-file .env \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.gpu.yml \
  up -d --build
```

The default `REGISTRY: true` in `config.yaml` pulls `intel/audio-analyzer`,
`intel/model-download` and `intel/retail-benchmark` from Docker Hub. When
`REGISTRY: false`, add `-f docker/docker-compose.local-build.yml` to the Compose
command, after the base file and before `up`. This override builds
`smart-classroom-audio-analyzer:local`, `sc-model-downloader:local` and
`sc-metrics-collector:local` from the `edge-ai-libraries` and `performance-tools`
submodules instead of pulling them. To pull the registry images explicitly first,
run the same command with `pull --ignore-buildable` instead of `up -d --build`.

`RENDER_GID` gives the container GPU access. Export it in each new terminal or
save its numeric value in `.env`. The image includes the Intel GPU runtime;
first startup may take longer while models download.

Verify Content Search readiness:

```bash
curl --noproxy '*' http://127.0.0.1:8000/api/v1/system/health
```

HTTP 200 with `"status":"ok"` means ready. Use the same Compose command with
`logs -f app` or `down` instead of `up -d --build` to inspect logs or stop services.

## Network Requirements

- **Proxy**: If behind a proxy, configure `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` for the shell and for Docker.
- **Model Downloads**: Stable access to `huggingface.co` is required to download pre-trained models.
