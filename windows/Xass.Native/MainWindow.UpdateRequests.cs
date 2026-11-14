using System.Text.Json;
using Xass.Native.Services;

namespace Xass.Native;

public sealed partial class MainWindow
{
    private async Task PollRemoteNativeUpdateAsync()
    {
        if (!NativeRuntimeMode.AllowUpdates || nativeUpdateSelection.IsBusy || nativeUpdateRunning) return;
        var response = await DesktopRequestAsync("desktop_update_requests");
        var requests = response.GetProperty("requests");
        long id = requests.GetArrayLength() == 0 ? 0 : requests[0].GetProperty("id").GetInt64();
        if (id > 0 && id != remoteNativeRequestId)
            DesktopNotice("С сервера пришёл запрос обновления. Откройте раздел «Обновления» для выбора и подтверждения.");
        remoteNativeRequestId = id;
        nativeRemoteUpdateStatus.Text = id == 0 ? "Запросов с сервера нет." : $"Запрос сервера №{id}: {RemoteOperationLabel(requests[0])}. Установка ещё не выполнена.";
        RefreshNativeUpdateControls();
    }
    private static string RemoteOperationLabel(JsonElement request) => request.GetProperty("operation").GetString() switch
    {
        "check" => "проверить обновления",
        "install" => "выбрать и установить обновление",
        "rollback" => "вернуться к предыдущей установке",
        _ => throw new InvalidOperationException("Неизвестный запрос обновления.")
    };
    private async Task ReviewRemoteNativeUpdateAsync()
    {
        using var operation = nativeUpdateSelection.TryBegin();
        if (operation is null || !NativeRuntimeMode.AllowUpdates || nativeUpdateRunning) return;
        string? action = null;
        try
        {
            RefreshNativeUpdateControls();
            var response = await DesktopRequestAsync("desktop_update_requests");
            var requests = response.GetProperty("requests");
            if (requests.GetArrayLength() == 0) return;
            var request = requests[0]; long id = request.GetProperty("id").GetInt64();
            string choice = request.GetProperty("channel").GetString() ?? "";
            var channel = choice == "selected" ? operation.Channel : NativeReleaseChannel.FromDistribution(choice);
            bool accepted = await DesktopConfirmAsync("Запрос сервера", $"№{id}: {RemoteOperationLabel(request)}. Канал: {channel.DisplayName}.\nКанал будет сохранён для последующих проверок, в том числе автоматических, если они включены. Установка или возврат потребуют отдельного подтверждения точной версии. Никакая ссылка с сервера не запускается.", "Рассмотреть");
            // Acknowledgment consumes this intent, not a license to install.
            if (!accepted)
            {
                await client.RequestAsync(new { action = "desktop_ack_update_request", id }, lifetime.Token);
                remoteNativeRequestId = 0;
                nativeRemoteUpdateStatus.Text = "Запрос сервера отклонён на ПК.";
                return;
            }
            action = request.GetProperty("operation").GetString();
            if (action != "rollback" && channel != operation.Channel)
            {
                operation.Dispose();
                nativeChannelChoiceInProgress = true;
                try
                {
                    if (!nativeUpdateSelection.TrySelect(channel, NativeUpdatePreferences.SaveChannel))
                        throw new InvalidOperationException("Канал занят другой операцией. Повторите запрос.");
                    nativeUpdateChannel.SelectedIndex = channel.Prerelease ? 1 : 0;
                    availableNativeUpdate = null;
                }
                finally { nativeChannelChoiceInProgress = false; }
            }
            // Consume only after local review and requested channel persistence succeed.
            await client.RequestAsync(new { action = "desktop_ack_update_request", id }, lifetime.Token);
            remoteNativeRequestId = 0;
        }
        finally { operation.Dispose(); RefreshNativeUpdateControls(); }
        if (action == "rollback") await RollbackNativeUpdateAsync();
        else if (action is "check" or "install")
        {
            await CheckNativeUpdateAsync();
            if (action == "install" && availableNativeUpdate is not null) await InstallNativeUpdateAsync();
        }
    }
    private async Task RollbackNativeUpdateAsync()
    {
        using var operation = nativeUpdateSelection.TryBegin();
        if (operation is null || !NativeRuntimeMode.AllowUpdates || nativeUpdateRunning) return;
        PreparedNativeUpdate? prepared = null;
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        try
        {
            nativeUpdateRunning = true; nativeUpdateCancellation = cancel;
            RefreshNativeUpdateControls();
            var response = await DesktopRequestAsync("desktop_updates");
            if (!response.TryGetProperty("rollback", out var value) || value.ValueKind == JsonValueKind.Null)
            { updateStatus.Text = "Нет резервной копии последней успешной установки для ручного возврата. Текущая версия не изменена."; return; }
            var target = NativeRollbackTarget.Parse(value);
            if (!await DesktopConfirmAsync("Вернуться к предыдущей установке?", $"XASS {target.Version} · {NativeReleaseChannel.FromDistribution(target.Distribution).DisplayName}\n{target.Revision}\nБудут проверены все файлы резервной копии и создана новая копия текущей программы. XASS, агент и микрофоны завершатся. Пользовательский профиль, ключи, архив и настройки не откатываются. Старый клиент может не поддерживать новые форматы данных. При неудачном запуске будет восстановлена текущая программа. Автообновление будет отключено, чтобы новая версия не установилась снова. Выбранный канал сохранится; включить автоматические обновления можно вручную.", "Проверить копию и вернуться")) return;
            prepared = await updateCoordinator.PrepareRollbackAsync(target,
                new Progress<string>(message => updateStatus.Text = message), cancel.Token);
            NativeUpdatePreferences.Save(false); nativeAutomatic.IsChecked = false;
            nativeUpdateCommitAuthorized = true;
            await QuitDesktopAsync(); prepared = null;
        }
        catch (OperationCanceledException) { updateStatus.Text = "Подготовка возврата отменена. Программа не заменена."; }
        finally
        {
            if (prepared is not null) await NativeUpdateCoordinator.CancelPreparedAsync(prepared);
            nativeUpdateCancellation = null; nativeUpdateRunning = false; nativeUpdateCommitAuthorized = false;
            operation.Dispose(); RefreshNativeUpdateControls();
        }
    }
}
