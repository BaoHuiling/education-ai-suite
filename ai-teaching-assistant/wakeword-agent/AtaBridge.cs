using System.Text;
using System.Text.Json;

namespace AtaWakeAgent;

/// <summary>
/// Hands a detected wake off to the existing ATA pipeline. This is the only
/// integration point with the rest of the app: a single POST to kiosk-core's
/// session endpoint - the same thing a mic-button press or the software
/// openWakeWord path does. No audio/ASR/RAG/TTS logic lives here.
/// </summary>
internal static class AtaBridge
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(15) };
    private static readonly HttpClient UiHttp = new() { Timeout = TimeSpan.FromSeconds(2) };

    /// <summary>Reports the wake to the UI server; true when an open browser UI will record the question.</summary>
    public static async Task<bool> ReportWakeDetectedAsync()
    {
        var body = await ReportWakeStatusAsync(new { @event = "detected" });
        if (body is null) return false;
        try
        {
            using var json = JsonDocument.Parse(body);
            return json.RootElement.TryGetProperty("capture", out var capture) && capture.GetString() == "browser";
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>
    /// Tells kiosk-core to open a normal voice session now. kiosk-core then
    /// captures the mic and runs ASR -> RAG -> LLM -> TTS exactly as today.
    /// </summary>
    public static async Task<bool> TriggerAsync()
    {
        var baseUrl = Environment.GetEnvironmentVariable("KIOSK_CORE_UI_BASE_URL")
                      ?? "http://127.0.0.1:8012";

        // Mirrors the payload mic_session.py sends on /sessions/start.
        var payload = """
        {
          "sample_rate": 16000,
          "chunk_seconds": 4.0,
          "silence_timeout_seconds": 1.5,
          "max_session_seconds": 20.0,
          "language": "en",
          "tts_model": "kokoro",
          "tts_language": "English"
        }
        """;

        var endpoint = $"{baseUrl.TrimEnd('/')}/api/v1/sessions/start";
        using var body = new StringContent(payload, Encoding.UTF8, "application/json");
        try
        {
            var resp = await Http.PostAsync(endpoint, body);
            var responseBody = await resp.Content.ReadAsStringAsync();
            AgentLog.Write(
                $"[AtaBridge] POST {endpoint} -> {(int)resp.StatusCode} {resp.StatusCode}; response: {responseBody}");
            if (resp.IsSuccessStatusCode)
            {
                try
                {
                    using var json = JsonDocument.Parse(responseBody);
                    if (json.RootElement.TryGetProperty("session_id", out var sessionIdValue))
                    {
                        var sessionId = sessionIdValue.GetString();
                        if (!string.IsNullOrWhiteSpace(sessionId))
                        {
                            var uiBaseUrl = Environment.GetEnvironmentVariable("ATA_WAKEWORD_UI_BASE_URL")
                                            ?? "http://127.0.0.1:7860";
                            await ReportWakeStatusAsync(new { @event = "session_started", session_id = sessionId });
                        }
                    }
                }
                catch (Exception ex)
                {
                    AgentLog.Write($"[WakeStatus] UI notification failed: {ex.Message}");
                }
            }
            return resp.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            AgentLog.Write($"[AtaBridge] Failed to reach kiosk-core at {endpoint}: {ex}");
            return false;
        }
    }

    private static async Task<string?> ReportWakeStatusAsync<T>(T payload)
    {
        var uiBaseUrl = Environment.GetEnvironmentVariable("ATA_WAKEWORD_UI_BASE_URL")
                        ?? "http://127.0.0.1:7860";
        try
        {
            using var notification = new StringContent(
                JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json");
            using var response = await UiHttp.PostAsync(
                $"{uiBaseUrl.TrimEnd('/')}/api/wakeword/activation", notification);
            var body = await response.Content.ReadAsStringAsync();
            AgentLog.Write($"[WakeStatus] UI notification -> {(int)response.StatusCode}; response: {body}");
            return response.IsSuccessStatusCode ? body : null;
        }
        catch (Exception ex)
        {
            AgentLog.Write($"[WakeStatus] UI notification failed: {ex.Message}");
            return null;
        }
    }
}
