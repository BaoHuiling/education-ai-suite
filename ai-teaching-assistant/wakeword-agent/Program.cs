namespace AtaWakeAgent;

/// <summary>
/// CLI entry point for the ATA Wake Agent.
///
///   AtaWakeAgent --arm       Register + enable the DSP keyword (one-time).
///   AtaWakeAgent --wake      Simulate a detection locally (no DSP) and trigger ATA.
///   AtaWakeAgent --trigger   Call kiosk-core directly - test the bridge against a
///                            running ATA with no hardware keyword detector needed.
///
/// On a real device Windows activates the packaged UWP host, which dispatches
/// --wake to this full-trust worker.
/// </summary>
internal static class Program
{
    private static async Task<int> Main(string[] args)
    {
        AgentLog.Write($"Worker started. Arguments: {string.Join(" ", args)}");
        // FullTrustProcessLauncher prepends "/InvokerPRAID: <appid>", so find the real
        // --command among the args; absence of one means a wake activation.
        var command = "--wake";
        foreach (var a in args)
        {
            if (a.StartsWith("--", StringComparison.Ordinal)) { command = a.ToLowerInvariant(); break; }
        }

        switch (command)
        {
            case "--arm":
                try
                {
                    await WakeAgent.ArmAsync();
                    AgentLog.Write("DSP keyword arm completed.");
                    return 0;
                }
                catch (Exception ex)
                {
                    AgentLog.Write($"[arm] failed: {ex}");
                    return 1;
                }

            case "--wake":
                try
                {
                    await WakeAgent.OnWakeAsync();
                    return 0;
                }
                catch (Exception ex)
                {
                    AgentLog.Write($"[wake] failed: {ex}");
                    return 1;
                }

            case "--trigger":
                // Proves the hand-off end-to-end without a hardware keyword detector.
                return await AtaBridge.TriggerAsync() ? 0 : 1;

            default:
                Console.WriteLine("ATA Wake Agent");
                Console.WriteLine("Usage: AtaWakeAgent [--arm | --wake | --trigger]");
                Console.WriteLine("  (no args)  Handle a wake activation -> trigger ATA.");
                Console.WriteLine("  --arm      Register + enable the DSP keyword (one-time).");
                Console.WriteLine("  --wake     Simulate a detection and trigger ATA.");
                Console.WriteLine("  --trigger  Call kiosk-core directly (test the bridge).");
                return 0;
        }
    }
}

internal static class AgentLog
{
    private static readonly string LogPath = Path.Combine(Path.GetTempPath(), "AtaWakeAgent.log");

    public static void Write(string message)
    {
        var entry = $"{DateTimeOffset.Now:O} {message}";
        Console.WriteLine(entry);
        try
        {
            File.AppendAllText(LogPath, entry + Environment.NewLine);
        }
        catch
        {
        }
    }
}
