using Microsoft.Win32;
using Windows.ApplicationModel.ConversationalAgent;

namespace AtaWakeAgent;

/// <summary>
/// The hardware wake-word logic. Two responsibilities:
///   1. Arm()  - enable a DSP-provisioned keyword so Windows wakes this agent.
///   2. OnWakeAsync() - run the 2-stage verify, then trigger ATA.
///
/// NOTE: the WinRT activation API surface (ActivationSignalDetector*) varies
/// slightly across Windows SDK versions. The calls below follow the official
/// "Voice Activation / ConversationalAgent" sample; validate member names
/// against the SDK you build with. This file compiles against WinRT but only
/// does anything real on an Intel DSP device with a provisioned keyword.
/// </summary>
internal static class WakeAgent
{
    // Cortana (en-US) identifiers read from the device's AarConfig at:
    //   HKCU\SOFTWARE\Microsoft\Speech_OneCore\Settings\VoiceActivation\
    //     <PackageFamilyName>!App\
    //       SignalDetectionConfigurations\{SignalId}\1033\{ModelId}  (DisplayName=Cortana)
    // SignalId = Intel HW keyword detector; ModelId = Cortana keyword model; 1033 = en-US.
    // Prefer discovering these at runtime via GetAllActivationSignalDetectors +
    // GetSupportedModelIdsForSignalId; these constants are the known-good fallback.
    private const string SignalId = "787E8510-069C-456F-AECE-068D884999EE";
    private const string ModelId = "86FA368E-357C-408C-BFCF-B89CBA2B0006";
    private const string DisplayName = "Cortana";
    // This detector reports supported LOCALES (LCIDs) as its "model ids"; en-US has the
    // Cortana model provisioned on this device.
    private const string PreferredLocale = "1033";
    private const int Langid = 0x409; // en-US
    private const string VoiceActivationRoot =
        @"SOFTWARE\Microsoft\Speech_OneCore\Settings\VoiceActivation";
    // AarSvc builds keyword->app bindings from this file when UseAarConfigFile=1.
    private const string AarConfigPath = @"C:\aarlog\aarconfig.txt";

    /// <summary>
    /// Sets this app's Windows voice-activation language and permission values.
    /// The keyword binding itself comes from AarSvc reading C:\aarlog\aarconfig.txt, which
    /// register-keyword.ps1 writes (elevated); this only verifies that binding exists. Idempotent.
    /// </summary>
    public static Task ArmAsync()
    {
        TryUnlockConversationalAgentFeature();

        var pfn = Windows.ApplicationModel.Package.Current.Id.FamilyName;
        var appKey = $"{pfn}!AtaWakeAgent";

        using (var app = Registry.CurrentUser.CreateSubKey($@"{VoiceActivationRoot}\{appKey}"))
        {
            app!.SetValue("LANGID", Langid, RegistryValueKind.DWord);
            app.SetValue("IsLanguageAvailable", 1, RegistryValueKind.DWord);
            app.SetValue("AgentActivationEnabled", 1, RegistryValueKind.DWord);
        }

        var expected = $"{DisplayName},{{{ModelId}}},{PreferredLocale},{appKey},background";
        var bound = File.Exists(AarConfigPath) && File.ReadAllLines(AarConfigPath)
            .Any(l => string.Equals(l.Trim(), expected, StringComparison.OrdinalIgnoreCase));
        AgentLog.Write(bound
            ? $"Armed: {appKey} language set; aarconfig binds '{DisplayName}' to this app."
            : $"NOT BOUND: {AarConfigPath} has no line '{expected}'. Run wakeword-agent\\register-keyword.ps1 elevated.");
        return Task.CompletedTask;
    }

    private static void TryUnlockConversationalAgentFeature()
    {
        if (!WakeAccess.TryUnlock(out var status))
        {
            throw new InvalidOperationException(status);
        }
        AgentLog.Write(status);
    }

    /// <summary>
    /// Invoked by the UWP activation host after Windows reports a DSP wake.
    /// Confirms the keyword, then triggers ATA.
    /// </summary>
    public static async Task OnWakeAsync()
    {
        TryUnlockConversationalAgentFeature();
        var browserCaptures = await AtaBridge.ReportWakeDetectedAsync();

        // Best-effort: when voice-activated, Windows gives us a session to drive
        // the agent state. When launched any other way (or if a full-trust app
        // does not receive a session), we still trigger ATA - launch == wake.
        ConversationalAgentSession? session = null;
        try
        {
            session = await ConversationalAgentSession.GetCurrentSessionAsync();
        }
        catch (Exception ex)
        {
            AgentLog.Write($"No ConversationalAgentSession ({ex.Message}); triggering ATA anyway.");
        }

        if (session is not null)
        {
            if (!await Verify2ndStageAsync(session))
            {
                AgentLog.Write("2nd-stage verify rejected the wake (false alarm). Sleeping.");
                return;
            }
            try { await session.RequestAgentStateChangeAsync(ConversationalAgentState.Listening); } catch { }
        }

        if (browserCaptures)
        {
            AgentLog.Write("Browser UI is open; it records the question (host-mic session skipped).");
        }
        else
        {
            var ok = await AtaBridge.TriggerAsync();
            AgentLog.Write(ok ? "ATA session started." : "ATA trigger failed.");
        }

        if (session is not null)
        {
            try { await session.RequestAgentStateChangeAsync(ConversationalAgentState.Inactive); } catch { }
        }
    }

    /// <summary>
    /// Second-stage (software) keyword confirmation over the DSP-buffered audio.
    /// The hardware 1st stage is deliberately trigger-happy; this rejects the
    /// false positives. STUB: always accepts so the pipeline is testable before
    /// a real verifier/model is wired in.
    /// </summary>
    private static Task<bool> Verify2ndStageAsync(ConversationalAgentSession session)
    {
        // TODO: read session audio and run a small keyword verifier (e.g. an
        // ONNX model) before returning true. Kept as a stub for now.
        _ = session;
        return Task.FromResult(true);
    }
}
