namespace Xass.Native.Services;

public enum MicrophonePhase { Off, Preparing, Listening, Processing, Paused, Stopping, Failed }
public sealed record MicrophonePresentation(string Action, bool CanStop)
{
    public static MicrophonePresentation For(MicrophonePhase phase, bool sessionRequested) => phase switch
    {
        MicrophonePhase.Failed => new("Повторить остановку", true),
        MicrophonePhase.Stopping => new("Останавливаю…", false),
        MicrophonePhase.Listening => new("Выключить микрофон", true),
        MicrophonePhase.Preparing => new("Отменить подготовку", true),
        MicrophonePhase.Processing => new("Отменить обработку", true),
        MicrophonePhase.Paused when sessionRequested => new("Отключить фоновый режим", true),
        _ when sessionRequested => new("Отменить подготовку", true),
        _ => new("Микрофон выключен", false)
    };
}
