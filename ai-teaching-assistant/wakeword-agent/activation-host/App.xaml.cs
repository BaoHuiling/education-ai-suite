using System;
using System.Linq;
using System.Threading.Tasks;
using Windows.ApplicationModel.Activation;
using Windows.ApplicationModel.Background;
using Windows.ApplicationModel.ConversationalAgent;
using Windows.ApplicationModel;
using Windows.Storage;
using Windows.UI.Xaml;

namespace AtaWakeAgent.ActivationHost
{
sealed partial class App : Application
{
    // AarSvc delivers DSP wakes for "background" aarconfig entries to this in-process task.
    private const string WakeBackgroundTaskName = "AtaConversationalAgentTask";
    // en-US LCID: the only language with a provisioned DSP keyword model on this device.
    private const uint SignalModelId = 1033;

    private readonly string _unlockStatus;
    private readonly bool _accessAvailable;

    public App()
    {
        InitializeComponent();
        // Unlock on every process start so background wake activations can open the session.
        _accessAvailable = WakeAccess.TryUnlock(out var status);
        _unlockStatus = status;
    }

    protected override async void OnLaunched(LaunchActivatedEventArgs args)
    {
        await LogAsync($"Launched. {_unlockStatus}");
        if (!_accessAvailable)
        {
            Exit();
            return;
        }
        await RegisterWakeBackgroundTaskAsync();
        await PrepareSessionAsync();
        await LaunchWorkerAsync("Arm");
        // Headless launcher: close so no empty window lingers (worker runs independently).
        Exit();
    }

    protected override async void OnActivated(IActivatedEventArgs args)
    {
        base.OnActivated(args);
        await LogAsync($"Activation kind '{args.Kind}'. {_unlockStatus} Dispatching wake.");
        if (_accessAvailable)
        {
            await LaunchWorkerAsync("Wake");
        }
        Exit();
    }

    protected override async void OnBackgroundActivated(BackgroundActivatedEventArgs args)
    {
        base.OnBackgroundActivated(args);
        var taskInstance = args.TaskInstance;
        if (taskInstance.Task.Name != WakeBackgroundTaskName)
        {
            return;
        }

        var deferral = taskInstance.GetDeferral();
        try
        {
            await LogAsync($"Wake background activation. {_unlockStatus}");
            if (!_accessAvailable)
            {
                return;
            }
            var session = await PrepareSessionAsync();
            await LaunchWorkerAsync("Wake");
            if (session != null)
            {
                await session.RequestAgentStateChangeAsync(ConversationalAgentState.Inactive);
            }
        }
        catch (Exception ex)
        {
            await LogAsync($"Wake background activation failed: {ex}");
        }
        finally
        {
            deferral.Complete();
        }
    }

    private static async Task RegisterWakeBackgroundTaskAsync()
    {
        try
        {
            if (BackgroundTaskRegistration.AllTasks.Values.Any(t => t.Name == WakeBackgroundTaskName))
            {
                await LogAsync($"Background task '{WakeBackgroundTaskName}' already registered.");
                return;
            }

            var builder = new BackgroundTaskBuilder { Name = WakeBackgroundTaskName };
            builder.SetTrigger(new ConversationalAgentTrigger());
            builder.Register();
            await LogAsync($"Registered background task '{WakeBackgroundTaskName}' with ConversationalAgentTrigger.");
        }
        catch (Exception ex)
        {
            await LogAsync($"Background task registration failed: {ex}");
        }
    }

    private static async Task<ConversationalAgentSession> PrepareSessionAsync()
    {
        try
        {
            var session = await ConversationalAgentSession.GetCurrentSessionAsync();
            if (session == null)
            {
                await LogAsync("No conversational-agent session available.");
                return null;
            }

            var modelSet = await session.SetSignalModelIdAsync(SignalModelId);
            await session.RequestInterruptibleAsync(false);
            await LogAsync(
                $"Session ready: signal='{session.Signal?.SignalName}', " +
                $"SetSignalModelId({SignalModelId})={modelSet}, state={session.AgentState}.");
            return session;
        }
        catch (Exception ex)
        {
            await LogAsync($"Unable to prepare conversational-agent session: {ex}");
            return null;
        }
    }

    private static async Task LaunchWorkerAsync(string parameterGroup)
    {
        try
        {
            await LogAsync($"Launching full-trust worker with parameter group '{parameterGroup}'.");
            await FullTrustProcessLauncher.LaunchFullTrustProcessForCurrentAppAsync(parameterGroup);
            await LogAsync($"Worker launch request accepted for '{parameterGroup}'.");
        }
        catch (Exception ex)
        {
            await LogAsync($"Wake worker launch failed: {ex}");
        }
    }

    private static async Task LogAsync(string message)
    {
        try
        {
            var file = await ApplicationData.Current.LocalFolder.CreateFileAsync(
                "wake-host.log", CreationCollisionOption.OpenIfExists);
            await FileIO.AppendTextAsync(file, $"{DateTimeOffset.Now:O} {message}{Environment.NewLine}");
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"Wake host logging failed: {ex}");
        }
    }
}
}