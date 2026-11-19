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

## Collect Useful Debug Data

```powershell
systeminfo > system_info.txt
python --version > python_version.txt

# Record running processes/ports
netstat -ano > netstat_ports.txt
```

Then include launcher and service terminal logs when reporting issues.
