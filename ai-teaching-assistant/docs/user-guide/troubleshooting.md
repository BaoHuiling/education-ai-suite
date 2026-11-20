# Troubleshooting

## 1. Startup Fails

**Symptom**:

`start_ata.ps1` exits early or one or more services never become ready.

**Checks**:

```powershell
# Verify required files exist
Test-Path .\setup_windows.ps1
Test-Path .\start_ata.ps1

# Verify submodule paths
Test-Path .\edge-ai-libraries\microservices\audio-analyzer\main.py
Test-Path .\edge-ai-libraries\microservices\text-to-speech\main.py
Test-Path .\voice-enabled-interactions\smart-kiosk-assistant\main.py
Test-Path .\voice-enabled-interactions\smart-kiosk-assistant\rag-service\main.py
```

If missing, rerun:

```powershell
.\setup_windows.ps1
```

## Windows Path-Length Error During Setup (WinError 206)

**Symptom**:

`setup_windows.ps1` stops while installing PyTorch dependencies for `rag-service`
with an error such as:

```text
ERROR: Could not install packages due to an OSError: [WinError 206] The filename or extension is too long
```

Deeply nested paths inside the PyTorch package can exceed the Windows path-length
limit, even when the repository is cloned under a relatively short path such as
`C:\rc1`. Setup exits before all dependencies are installed.

**Fix**:

1. Open **PowerShell as Administrator** and enable Windows long-path support:

	```powershell
	New-ItemProperty -Path "HKLM:\System\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
	```

2. Close and reopen PowerShell. If using the VS Code integrated terminal, restart
	VS Code as well.
3. From the AI Teaching Assistant repository directory, rerun setup:

	```powershell
	.\setup_windows.ps1
	```

4. If the same error persists, restart Windows and retry setup. A Windows restart
	may be required because the long-path setting is cached per process;
	already-running processes may not pick up the change.

## Smart App Control Blocks regex

**Symptom**:

Windows Smart App Control blocks the native extension in Python package
`regex==2026.11.19`, preventing a service from starting or importing its
dependencies. Version `2026.11.18` was previously reported working, but its
Windows x64 / Python 3.11 wheel was also blocked on the tested installation.
Version `2026.9.29` passed the native import and matching checks in all four
service environments without changing Application Control settings.

**Fix**:

[setup_windows.ps1](../../setup_windows.ps1) reads `regex==2026.9.29` from
[windows-dependency-pins.psd1](../../configs/windows-dependency-pins.psd1) and adds
it to the dependency-install command for all four service virtual environments:
`text-to-speech`, `audio-analyzer`, `rag-service`, and `kiosk-core`. The pin is
resolved together with each service's requirements, so fresh installs and setup
reruns use the same version, including downgrading an existing `2026.11.18` or
`2026.11.19` install.

1. Update your checkout to include the pinned setup script.
2. From the AI Teaching Assistant repository directory, stop the app, rerun setup,
  and restart it:

  ```powershell
  .\stop_ata.ps1
  .\setup_windows.ps1
  .\start_ata.ps1
  ```

  The stop script clears the uploaded knowledge base. Preserve any needed
  data with a controlled shutdown and backup before using it.

3. Verify the installed version and native-extension import in an affected service's
  virtual environment. For example, for `rag-service`:

  ```powershell
  & .\voice-enabled-interactions\smart-kiosk-assistant\rag-service\venv\Scripts\python.exe -c "import regex; from importlib.metadata import version; print(version('regex')); assert regex.fullmatch('ok', 'ok')"
  ```

  The command should print `2026.9.29` and exit without an import error.

Keep Smart App Control enabled. When installing service requirements manually,
include `"regex==2026.9.29"` in the same pip install command. Only change the setup
pin after validating a replacement version with Smart App Control enabled.

## Smart App Control Blocks cygrpc / Model Warmup Fails

**Symptom**:

RAG model warmup fails with:

```text
ImportError: DLL load failed while importing cygrpc: An Application Control policy has blocked this file.
```

`cygrpc` is the native extension supplied by `grpcio`, not `regex`. On the affected
Windows x64 / Python 3.12 installation, `grpcio==1.85.3` was blocked in both the RAG
and audio-analyzer environments. `grpcio==1.85.1` passed the native import check
without changing Application Control settings.

**Fix**:

[setup_windows.ps1](../../setup_windows.ps1) reads the compatibility pins from
[windows-dependency-pins.psd1](../../configs/windows-dependency-pins.psd1), including
`grpcio==1.85.1` alongside `regex==2026.9.29` for all four service environments.
This data file records each pin's exact requirement and service scope;
maintain compatibility pins there rather than in the setup script.
Update your checkout, stop
the app, and rerun setup as described above. For manual dependency installs,
include both `"regex==2026.9.29"` and `"grpcio==1.85.1"` in the same pip command.

Further warmup checks exposed two additional native-extension blocks. Setup also
applies these service-specific pins, tested on the same Windows x64 / Python 3.12
installation:

| Service | Blocked Package | Tested Pin | Blocked Extension |
| --- | --- | --- | --- |
| RAG | `xxhash==4.1.0` | `xxhash==4.0.2` | `_xxhash` |
| Audio analyzer | `ml-dtypes==0.7.0` | `ml-dtypes==0.6.0` | `_ml_dtypes_ext` |

Include the matching service-specific pin for manual installs too. Do not apply
the audio `ml-dtypes` pin to RAG: this release requires NumPy 2, while RAG uses
NumPy 1.26.

Verify the native import in the affected environment, for example:

```powershell
& .\voice-enabled-interactions\smart-kiosk-assistant\rag-service\venv\Scripts\python.exe -c "from grpc._cython import cygrpc; import grpc; print(grpc.__version__)"
```

This should print `1.85.1` without an import error. Application Control decisions
can differ by machine and policy; keep the policy enabled and consult your
administrator if this version is also blocked.

An audio-analyzer error about a missing `openvino_encoder_model.xml` after
`Export failed: 1` is a downstream symptom of a failed export, not proof of the
same `grpcio` problem. Setup now displays the exporter's INFO output and full
warmup traceback. In the tested installation, the exporter was blocked while
importing `_ml_dtypes_ext`; the audio-specific pin above fixed that import.
Diagnose the first exporter error before the missing-file
message; do not create an empty XML file or delete working models to suppress it.

If any model warmup fails, setup reports `SETUP INCOMPLETE` and exits with code 1.
A package metadata check alone does not verify native DLL imports or model
readiness. Resolve the reported error and rerun setup before starting services.

## Port Conflicts

**Symptom**:

Launcher reports an existing process on required ports.

**Checks**:

```powershell
netstat -ano | findstr :7860
netstat -ano | findstr :8012
netstat -ano | findstr :8020
netstat -ano | findstr :8011
netstat -ano | findstr :8010
netstat -ano | findstr :9000
```

Stop conflicting PIDs or run:

```powershell
.\stop_ata.ps1 -Force
```

## Models Download Slowly or First Run Is Long

First startup can take significant time due to model downloads and OpenVINO™
compilation.

Useful checks:

```powershell
ping huggingface.co
```

If interrupted, rerun `start_ata.ps1` after connectivity is stable.

## UI Not Loading on 7860

**Symptom**:

`http://127.0.0.1:7860` is unavailable.

**Checks**:

```powershell
curl http://127.0.0.1:7860/healthz
```

If health fails:
- Verify `ata_ui_server.py` is running.
- Ensure React build assets exist under `assistant-react-ui/dist`.

Rebuild UI assets if needed:

```powershell
cd assistant-react-ui
npm install
npm run build
```

## Microphone Permission Issues

**Symptom**:

Mic button does nothing or browser shows permission errors.

### Fixes
- Allow microphone for `http://127.0.0.1:7860` in browser site settings.
- Verify Windows microphone privacy settings allow the browser.
- Test with another input device in Windows sound settings.

## Upload or Ingestion Failures

**Symptom**:

File uploads fail or context stats do not increase.

**Checks**:

- Supported formats: `.txt`, `.md`, `.docx`, `.pdf`
- Verify file size is within configured limits.
- Clear context and retry upload.

Service checks:

```powershell
curl http://127.0.0.1:8020/health
curl http://127.0.0.1:8020/api/v1/context/stats
```

## Clear Context Fails

The UI now surfaces clear-context errors. If clear fails, inspect `rag-service`
output and verify storage paths are writable.

## Slow or Low-Quality Answers

### Speed tuning
- Use a smaller LLM in `rag-service/config.yaml`
- Lower retrieval complexity (`top_k`, `fetch_k`)
- Use GPU for LLM where available

### Relevance tuning
- Improve document quality and structure
- Re-ingest after changing chunking settings
- Adjust reranker usage in `rag-service/config.yaml`

## No Audio Playback

**Symptom**:

Transcript/answer text appears but no spoken audio.

**Checks**:

- Verify `text-to-speech` health: `curl http://127.0.0.1:8011/health`
- Verify browser/tab volume and output device
- Verify session snapshot contains `tts_audio_segments`

## Metrics Panel Errors

**Symptom**:

Metrics cards show errors or blank values.

**Checks**:

```powershell
curl http://127.0.0.1:9000/health
curl http://127.0.0.1:8012/api/v1/metrics
curl http://127.0.0.1:8012/api/v1/platform-info
```

## Hardware Wake Word (Cortana)

Setup steps are in
[wakeword-agent/README.md](../../wakeword-agent/README.md).

### Hardware access configuration is missing or rejected

Both the Windows activation host and the full-trust worker need Microsoft-approved
LAF access. They share a private `wake-access.local.json` in the installed user's
package `LocalState`; there is no token embedded in the public source or MSIX.

From `ai-teaching-assistant\wakeword-agent`, run:

```powershell
.\configure-access.ps1 -ValidateOnly
.\configure-access.ps1
```

The first command validates your local JSON without printing values. The second
requires the matching package to be installed and copies the file into that
user's package data. Run it as the same signed-in user who installed the package.
Do not put the token in `.env`, source, logs, screenshots, or issue reports.

Afterward, run `start_wakeword.ps1` from `ai-teaching-assistant` again and check
`wake-host.log`. An LAF status of `Available` or `AvailableWithoutToken`, followed
by `SetSignalModelId(1033)=True`, is the expected sequence. If LAF access is denied,
confirm the approval, token/attestation, and package identity with Microsoft.
Format validation alone does not authorize the feature. Leave Windows security
settings enabled; see the [approval and configuration guide](../../wakeword-agent/README.md#microsoft-approval-and-private-access-configuration).

### Saying "Cortana" does nothing

Check in this order:

```powershell
# 1. Keyword bound to ATA's agent (one line, ending in ...!AtaWakeAgent,background)
Get-Content C:\aarlog\aarconfig.txt

# 2. AarSvc running (it stays Stopped after sign-in until the host is opened)
Get-Service AarSvc_*

# 3. Host armed this sign-in
Get-Content "$env:LOCALAPPDATA\Packages\Intel.ATA.WakeAgent_t2v8cwdf3mgnr\LocalState\wake-host.log" -Tail 5
```

- **AarSvc is `Stopped`** — Open the host once: `.\start_wakeword.ps1`, or
  **Win + R** → `shell:AppsFolder\Intel.ATA.WakeAgent_t2v8cwdf3mgnr!AtaWakeAgent`.
- **Binding names another app** — Run `wakeword-agent\register-keyword.ps1`
  in an elevated PowerShell, then reboot.
- **Nothing after `register-keyword.ps1` or `package.ps1 -Install`** — Reboot.
  Restarting AarSvc leaves keyword detection disabled until the next boot. Use
  `package.ps1 -Install -SkipKeywordBinding` for code updates.
- Confirm **Settings → Privacy & security → Voice activation** and
  **Microphone** are on.

### UI shows "Cortana wake agent is not armed"

The UI server checks the voice-activation registry entries for
`ATA_WAKEWORD_AGENT_AUMID`. Rerun `register-keyword.ps1` (elevated), reboot,
then restart ATA so the UI server reloads `.env`.

### Wake is detected but the question is not recorded or answered

- Keep the browser UI open at `http://127.0.0.1:7860`. Without an open UI the
  agent falls back to host-microphone capture, which can transcribe poorly on
  some microphone arrays.
- Hard-refresh the page (**Ctrl+F5**) after updating the UI, then click once on
  the page so the browser may play the answer.
- Check the worker log `%TEMP%\AtaWakeAgent.log` for
  `Browser UI is open; it records the question`.

## Collect Useful Debug Data

```powershell
systeminfo > system_info.txt
python --version > python_version.txt

# Record running processes/ports
netstat -ano > netstat_ports.txt
```

Then include launcher and service terminal logs when reporting issues.
