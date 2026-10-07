using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private readonly DiscordMusicPresence discordMusic = new();
    private readonly DiscordPresenceStore discordStore = new();
    private DiscordPresencePreferences discordPreferences = new();
    private readonly ToggleSwitch discordSharing = new() { Header = "Показывать текущую музыку в Discord", IsOn = false };
    private readonly TextBox discordApplication = new() { Header = "Публичный Application ID приложения XASS", MaxLength = 20 };
    private readonly TextBlock discordStatus = new() { TextWrapping = TextWrapping.Wrap };
    private bool updatingDiscord;

    private UIElement BuildDiscordPresenceControl()
    {
        discordPreferences = discordStore.Load();
        discordApplication.Text = discordPreferences.ApplicationId;
        discordSharing.IsOn = discordPreferences.Enabled;
        discordSharing.IsEnabled = DiscordPresencePreferences.ValidId(discordPreferences.ApplicationId);
        discordStatus.Text = discordPreferences.Enabled ? "Ожидаем воспроизведение и запущенный Discord." : discordSharing.IsEnabled
            ? "Показ музыки в Discord выключен." : "Сначала укажите Application ID XASS. Токены и вход в аккаунт не нужны.";
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(discordSharing, "DiscordMusicSharing");
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetAutomationId(discordApplication, "DiscordApplicationId");
        var save = DButton("Сохранить Application ID", async () =>
        {
            string id = discordApplication.Text.Trim();
            if (id.Length > 0 && !DiscordPresencePreferences.ValidId(id))
            { discordStatus.Text = "Application ID должен содержать 16–20 цифр. Не вводите токен или секрет."; return; }
            await SaveDiscordPreferencesAsync(new(false, id));
        });
        discordSharing.Toggled += async (_, _) =>
        {
            if (!updatingDiscord) await SaveDiscordPreferencesAsync(discordPreferences with { Enabled = discordSharing.IsOn });
        };
        var content = DStack(discordSharing, discordStatus,
            DLabel("Discord получит только название, исполнителя и время текущего трека. Пауза и остановка убирают статус. Видимость для других людей определяется настройками активности Discord."),
            discordApplication, save);
        _ = discordMusic.ConfigureAsync(discordPreferences);
        content.MaxWidth = 360;
        return new ScrollViewer { Content = content, MaxHeight = 440, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled };
    }
    private async Task SaveDiscordPreferencesAsync(DiscordPresencePreferences selected)
    {
        updatingDiscord = true; discordSharing.IsEnabled = false;
        try
        {
            // Disabling takes effect even if persisting the setting fails.
            if (!selected.Enabled) await discordMusic.ConfigureAsync(selected);
            await Task.Run(() => discordStore.Save(selected));
            discordPreferences = selected.Normalize();
            await discordMusic.ConfigureAsync(discordPreferences);
            discordStatus.Text = discordMusic.Status;
        }
        catch { discordStatus.Text = "Сейчас показ в Discord выключен, но настройка не сохранена для следующего запуска. Повторите сохранение."; discordPreferences = discordPreferences with { Enabled = false }; await discordMusic.ConfigureAsync(discordPreferences); }
        finally
        {
            discordSharing.IsOn = discordPreferences.Enabled;
            discordSharing.IsEnabled = DiscordPresencePreferences.ValidId(discordPreferences.ApplicationId);
            updatingDiscord = false;
        }
    }
    private async Task PublishDiscordMusicAsync(DiscordMusicTrack? track)
    {
        await discordMusic.PublishAsync(track);
        if (!closed && DiscordPresencePreferences.ValidId(discordPreferences.ApplicationId))
            discordStatus.Text = discordMusic.Status;
    }
}
