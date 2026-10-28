using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media.Animation;
using Windows.Graphics;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private bool assistantCompact;
    private SizeInt32 assistantPreviousSize;
    private readonly Storyboard assistantPulse = new();
    private void InitializeAssistantPresentation()
    {
        var pulse = new DoubleAnimation { From = .12, To = .40, AutoReverse = true,
            Duration = new Duration(TimeSpan.FromSeconds(1.1)), RepeatBehavior = RepeatBehavior.Forever };
        Storyboard.SetTarget(pulse, AssistantAvatarGlow); Storyboard.SetTargetProperty(pulse,"Opacity"); assistantPulse.Children.Add(pulse);
        Closed += (_, _) => assistantPulse.Stop();
    }
    private void SetAssistantStage(string stage)
    {
        AssistantStageTitle.Text = stage switch {
            "loading_model" => "Готовлюсь слушать", "recording" or "listening" => "Слушаю вас",
            "transcribing" => "Распознаю речь", "planning" => "Разбираю команду", "ready" => "Проверьте действие",
            "executing" => "Выполняю", "dispatched" => "Запрос отправлен", "succeeded" => "Готово",
            "blocked" => "Нужна настройка", "rejected" => "Команда не поддерживается", "unverified" => "Проверьте результат",
            "failed" => "Не получилось", "cancelled" => "Отменено", "paused" => "Микрофон на паузе", _ => "Готов к команде" };
        int step = stage is "ready" or "planning" ? 1 : stage is "executing" or "dispatched" or "succeeded" or "unverified" ? 2 : 0;
        AssistantInputStep.Opacity = step == 0 ? 1 : .5; AssistantReviewStep.Opacity = step == 1 ? 1 : .5; AssistantActionStep.Opacity = step == 2 ? 1 : .5;
        assistantPulse.Stop();
        if (stage is "loading_model" or "recording" or "listening" or "transcribing" or "planning" or "executing")
        { if (appearanceUi.AnimationsEnabled) assistantPulse.Begin(); }
    }
    private void AssistantCompactClick(object sender, RoutedEventArgs args)
    {
        assistantCompact = !assistantCompact;
        if (assistantCompact) { assistantPreviousSize = AppWindow.Size; Navigation.IsPaneVisible = false; AppWindow.Resize(new SizeInt32(620,900)); }
        else { Navigation.IsPaneVisible = true; AppWindow.Resize(assistantPreviousSize); }
        AssistantCompact.Content = assistantCompact ? "Развернуть" : "Компактный вид";
        UpdatePagePadding();
    }
    private void AssistantExampleClick(object sender, RoutedEventArgs args)
    {
        if (assistantRequest is null && sender is Button { Tag: string example }) { AssistantText.Text = example; AssistantText.Focus(FocusState.Programmatic); }
    }
}
