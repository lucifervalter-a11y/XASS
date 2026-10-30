using System.Collections.ObjectModel;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Storage.Pickers;
using Windows.Storage.Streams;

namespace Xass.Native;

public sealed record CurationRow(string Id, string Name, string Status)
{
    public string Display => $"{Name}\n{Status}";
}

public sealed partial class MainWindow
{
    private readonly ObservableCollection<CurationRow> curationRows = new();
    private readonly ListView curationList = new() { DisplayMemberPath = "Display", MaxHeight = 360, SelectionMode = ListViewSelectionMode.Single };
    private readonly TextBlock curationStatus = DLabel("Выберите папку. Файлы проверяются на этом ПК, оригиналы меняются только после вашего подтверждения.");
    private readonly TextBlock curationReview = DLabel("");
    private readonly TextBlock curationDuplicates = DLabel("");
    private readonly ComboBox curationSuggestions = new() { PlaceholderText = "Вариант из каталога", HorizontalAlignment = HorizontalAlignment.Stretch };
    private readonly TextBox curationTitle = new() { Header = "Название", MaxLength = 480 };
    private readonly TextBox curationArtist = new() { Header = "Исполнитель", MaxLength = 480 };
    private readonly TextBox curationAlbum = new() { Header = "Альбом", MaxLength = 480 };
    private readonly TextBox curationLyrics = new() { Header = "Текст / LRC с таймингами", MaxLength = 65536, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 200 };
    private readonly TextBlock curationArtwork = DLabel("Обложка: без изменений");
    private readonly Image curationOldCover = new() { Width = 120, Height = 120, Stretch = Stretch.UniformToFill };
    private readonly Image curationNewCover = new() { Width = 120, Height = 120, Stretch = Stretch.UniformToFill };
    private readonly TextBox curationOriginalLyrics = new() { Header = "Исходный текст", IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MaxHeight = 180 };
    private readonly ListView curationHistory = new() { DisplayMemberPath = "Display", MaxHeight = 260, SelectionMode = ListViewSelectionMode.Multiple };
    private readonly ProgressBar curationProgress = new() { Minimum = 0, Maximum = 1, Visibility = Visibility.Collapsed };
    private CancellationTokenSource? curationOperation;
    private string curationSelected = "", curationCoverPath = "", curationProposal = "", curationDigest = "";
    private bool curationFilling;
    private readonly List<JsonElement> curationProposals = new();

    private void InitializeCuration()
    {
        var page = AddDesktopPage("curation", "Разобрать медиатеку", Symbol.Library);
        page.Children.Add(DLabel("Порядок в вашей музыке", 28));
        page.Children.Add(curationStatus);
        page.Children.Add(DActions(DButton("Выбрать папку и проверить", PickCurationFolderAsync),
            DButton("Найти данные для всех", () => SearchCurationAsync(all: true)),
            DButton("Остановить", () => { curationOperation?.Cancel(); return Task.CompletedTask; })));
        page.Children.Add(curationProgress);
        curationList.ItemsSource = curationRows;
        curationList.SelectionChanged += async (_, _) =>
        {
            if (curationOperation is not null || curationList.SelectedItem is not CurationRow row) return;
            await CurationRunAsync(async token =>
            {
                var detail = await CurationRequestAsync(new { action = "desktop_curation_inspect", id = row.Id, version = 1 }, token);
                curationSelected = row.Id; curationDigest = DValue(detail, "digest"); curationCoverPath = ""; curationProposal = "";
                SetCurationFields(detail.GetProperty("tags"));
                curationReview.Text = "Выберите поиск в каталоге или исправьте поля вручную. Перед записью будет показано сравнение.";
                LoadCurationProposals(detail.GetProperty("proposals"));
            });
        };
        page.Children.Add(curationList);
        page.Children.Add(DActions(DButton("Сравнить звук локально", FingerprintCurationAsync)));
        page.Children.Add(curationDuplicates);
        page.Children.Add(DLabel("Проверка выбранного трека", 20));
        page.Children.Add(DActions(DButton("Найти в открытом каталоге", () => SearchCurationAsync(all: false)),
            DButton("Добавить свою обложку", PickCurationArtworkAsync), DButton("Распознать текст на этом ПК", TranscribeCurationAsync)));
        page.Children.Add(DLabel("Поиск отправляет названия в LRCLIB и MusicBrainz; обложки — из Cover Art Archive. Если на ПК настроен AcoustID, отправляется акустический отпечаток. Аудиофайл не отправляется. Распознавание текста использует уже установленную модель из настроек помощника."));
        curationSuggestions.SelectionChanged += (_, _) =>
        {
            int index = curationSuggestions.SelectedIndex;
            if (index < 0 || index >= curationProposals.Count) return;
            curationCoverPath = "";
            ShowCurationProposal(curationProposals[index]);
        };
        page.Children.Add(curationSuggestions);
        foreach (var editor in new[] { curationTitle, curationArtist, curationAlbum, curationLyrics })
        {
            editor.TextChanged += (_, _) => { if (!curationFilling) curationProposal = ""; };
            page.Children.Add(editor);
        }
        page.Children.Add(curationArtwork);
        page.Children.Add(DActions(DStack(DLabel("Обложка до"), curationOldCover), DStack(DLabel("Обложка после"), curationNewCover)));
        page.Children.Add(curationOriginalLyrics);
        page.Children.Add(DActions(DButton("Посмотреть изменения", PreviewCurationAsync), DButton("Применить просмотренную правку", ApplyCurationAsync)));
        page.Children.Add(curationReview);
        page.Children.Add(DLabel("Журнал оригиналов", 20));
        page.Children.Add(DLabel("Выберите одну или несколько правок (например, все треки альбома). Откат идёт от новых к старым, по одному файлу. Конфликты пропускаются, а успешные восстановления сохраняются даже при отмене. Дубликаты только показываются — файлы не удаляются."));
        page.Children.Add(curationHistory);
        page.Children.Add(DActions(DButton("Обновить журнал", LoadCurationHistoryAsync), DButton("Откатить выбранные правки", UndoCurationAsync)));
        Closed += (_, _) => curationOperation?.Cancel();
    }

    private async Task<JsonElement> CurationRequestAsync(object request, CancellationToken token)
    {
        var result = await client.RequestAsync(request, token);
        if (result.TryGetProperty("error", out var error)) throw new InvalidOperationException(error.GetString());
        return result;
    }

    private async Task CurationRunAsync(Func<CancellationToken, Task> action)
    {
        if (curationOperation is not null) { curationStatus.Text = "Дождитесь завершения текущего действия или нажмите «Остановить»."; return; }
        using var cancel = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
        curationOperation = cancel;
        curationList.IsEnabled = curationSuggestions.IsEnabled = curationHistory.IsEnabled = false;
        foreach (var editor in new[] { curationTitle, curationArtist, curationAlbum, curationLyrics }) editor.IsEnabled = false;
        curationProgress.Visibility = Visibility.Visible;
        try { await action(cancel.Token); }
        catch (OperationCanceledException) { curationStatus.Text = "Обработка остановлена. Уже проверенные файлы остались в списке. Состояние записи можно проверить в журнале."; }
        catch (Exception error) { curationStatus.Text = error is InvalidOperationException or TimeoutException ? error.Message : "Действие не завершено. Проверьте доступ к файлам."; }
        finally
        {
            curationOperation = null; curationProgress.Visibility = Visibility.Collapsed;
            curationList.IsEnabled = curationSuggestions.IsEnabled = curationHistory.IsEnabled = true;
            foreach (var editor in new[] { curationTitle, curationArtist, curationAlbum, curationLyrics }) editor.IsEnabled = true;
        }
    }

    private async Task PickCurationFolderAsync()
    {
        if (curationOperation is not null) return;
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.MusicLibrary };
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        var folder = await picker.PickSingleFolderAsync();
        if (folder is null || closed) return;
        await CurationRunAsync(async token =>
        {
            curationRows.Clear(); curationSelected = curationProposal = curationCoverPath = "";
            curationReview.Text = ""; curationDuplicates.Text = "";
            var start = await CurationRequestAsync(new { action = "desktop_curation_start", folder = folder.Path, version = 1 }, token);
            var tracks = start.GetProperty("tracks").EnumerateArray().ToArray();
            curationProgress.Maximum = Math.Max(1, tracks.Length); curationProgress.Value = 0;
            int failures = 0;
            foreach (var track in tracks)
            {
                token.ThrowIfCancellationRequested();
                string id = DValue(track, "id"), name = DValue(track, "name");
                curationStatus.Text = $"Проверка {curationRows.Count + 1} из {tracks.Length}: {name}";
                try
                {
                    var detail = await CurationRequestAsync(new { action = "desktop_curation_inspect", id, version = 1 }, token);
                    string issues = string.Join(" · ", detail.GetProperty("issues").EnumerateArray().Select(x => x.GetString()));
                    curationRows.Add(new(id, name, string.IsNullOrWhiteSpace(issues) ? "Теги, обложка и текст присутствуют" : issues));
                }
                catch (InvalidOperationException error) { failures++; curationRows.Add(new(id, name, error.Message)); }
                curationProgress.Value = curationRows.Count;
            }
            await RefreshCurationDuplicatesAsync(token);
            curationStatus.Text = $"Проверено: {tracks.Length - failures}; не удалось прочитать: {failures}. Ничего не изменено."
                + (start.GetProperty("truncated").GetBoolean() ? " Достигнут предел: выберите меньшую папку для оставшихся файлов." : "");
        });
    }

    private async Task RefreshCurationDuplicatesAsync(CancellationToken token)
    {
        var groups = await CurationRequestAsync(new { action = "desktop_curation_duplicates", version = 1 }, token);
        curationDuplicates.Text = string.Join("\n\n", groups.GetProperty("groups").EnumerateArray().Select(g =>
            $"{DValue(g, "reason")}\n{string.Join(" / ", g.GetProperty("tracks").EnumerateArray().Select(t => DValue(t, "name")))}"));
        if (groups.GetProperty("truncated").GetBoolean()) curationDuplicates.Text += "\nПоказаны первые 200 групп.";
    }

    private async Task FingerprintCurationAsync()
    {
        var ids = curationRows.Select(row => row.Id).ToArray();
        if (ids.Length == 0) { curationStatus.Text = "Сначала проверьте папку с музыкой."; return; }
        await CurationRunAsync(async token =>
        {
            curationProgress.Maximum = ids.Length; curationProgress.Value = 0;
            int completed = 0, failed = 0;
            foreach (string id in ids)
            {
                token.ThrowIfCancellationRequested();
                try
                {
                    var result = await CurationRequestAsync(new { action = "desktop_curation_fingerprint", version = 1, id }, token);
                    if (!result.GetProperty("available").GetBoolean()) { curationStatus.Text = DValue(result, "notice"); return; }
                    completed++;
                }
                catch (InvalidOperationException) { failed++; }
                curationProgress.Value++;
                curationStatus.Text = $"Сравнение звука на ПК: {curationProgress.Value} из {ids.Length}; ошибок: {failed}. Можно остановить.";
            }
            await RefreshCurationDuplicatesAsync(token);
            curationStatus.Text = $"Получено отпечатков: {completed}; ошибок: {failed}. Совпадения — только кандидаты: прослушайте их. Файлы не менялись, звук не отправлялся в сеть.";
        });
    }

    private async Task SearchCurationAsync(bool all)
    {
        var ids = all ? curationRows.Select(r => r.Id).ToArray() : string.IsNullOrEmpty(curationSelected) ? [] : new[] { curationSelected };
        if (ids.Length == 0) { curationStatus.Text = "Сначала выберите проверенный трек."; return; }
        if (!await DesktopConfirmAsync("Поиск данных", $"Найти данные для {ids.Length} треков в открытых каталогах? Отправятся названия и исполнители, а при настроенном AcoustID — акустический отпечаток. Результаты требуют просмотра, файлы сами не изменятся.", "Найти")) return;
        await CurationRunAsync(async token =>
        {
            curationProgress.Maximum = ids.Length; curationProgress.Value = 0;
            int found = 0, failed = 0;
            foreach (string id in ids)
            {
                token.ThrowIfCancellationRequested();
                try
                {
                    var result = await CurationRequestAsync(new { action = "desktop_curation_candidates", id, version = 1 }, token);
                    var proposals = result.GetProperty("proposals").EnumerateArray().Select(p => p.Clone()).ToArray();
                    if (proposals.Length > 0) found++;
                    int rowIndex = curationRows.ToList().FindIndex(r => r.Id == id);
                    if (rowIndex >= 0) curationRows[rowIndex] = curationRows[rowIndex] with { Status = proposals.Length > 0 ? $"Найдено вариантов: {proposals.Length}. Требуют проверки" : "Точного совпадения в каталоге нет" };
                    if (!all)
                    {
                        LoadCurationProposals(result.GetProperty("proposals"));
                        curationReview.Text += "\n\n" + string.Join("\n", result.GetProperty("notices").EnumerateArray().Select(x => x.GetString()));
                    }
                }
                catch (InvalidOperationException) { failed++; }
                curationProgress.Value++;
                curationStatus.Text = $"Поиск: {curationProgress.Value} из {ids.Length}. Варианты: {found}; ошибки: {failed}.";
            }
            curationStatus.Text += " Ничего не записано. Выберите трек и вариант для просмотра.";
            await RefreshCurationDuplicatesAsync(token);
        });
    }

    private void LoadCurationProposals(JsonElement proposals)
    {
        curationProposals.Clear(); curationSuggestions.Items.Clear();
        foreach (var proposal in proposals.EnumerateArray())
        {
            curationProposals.Add(proposal.Clone());
            var after = proposal.GetProperty("after");
            curationSuggestions.Items.Add($"{DValue(after, "artist")} — {DValue(after, "title")} · {DValue(after, "album")} · {DValue(proposal, "source")}");
        }
        // A weak catalog match must be explicitly selected, never preselected.
        curationSuggestions.SelectedIndex = -1; curationProposal = "";
    }

    private void SetCurationFields(JsonElement tags)
    {
        curationFilling = true;
        try { curationTitle.Text = DValue(tags, "title"); curationArtist.Text = DValue(tags, "artist"); curationAlbum.Text = DValue(tags, "album"); curationLyrics.Text = DValue(tags, "lyrics"); }
        finally { curationFilling = false; }
        curationArtwork.Text = tags.TryGetProperty("artwork", out var art) && art.GetBoolean() ? "Обложка уже есть в файле" : "Обложки в файле нет";
    }

    private void ShowCurationProposal(JsonElement proposal)
    {
        SetCurationFields(proposal.GetProperty("after"));
        curationProposal = DValue(proposal, "id");
        var before = proposal.GetProperty("before"); var after = proposal.GetProperty("after");
        curationOriginalLyrics.Text = DValue(before, "lyrics");
        _ = LoadCurationCoverAsync(curationOldCover, DValue(before, "cover_preview"));
        _ = LoadCurationCoverAsync(curationNewCover, DValue(after, "cover_preview"));
        var changes = new List<string>();
        foreach (var pair in new[] { ("title", "Название"), ("artist", "Исполнитель"), ("album", "Альбом") })
            if (DValue(before, pair.Item1) != DValue(after, pair.Item1)) changes.Add($"{pair.Item2}: {DValue(before, pair.Item1, "—")} → {DValue(after, pair.Item1, "—")}");
        if (DValue(before, "lyrics") != DValue(after, "lyrics")) changes.Add($"Текст: {DValue(before, "lyrics").Length} → {DValue(after, "lyrics").Length} символов (новый текст в поле выше)");
        if (proposal.TryGetProperty("artwork_changed", out var artChanged) && artChanged.GetBoolean()) changes.Add(before.GetProperty("artwork").GetBoolean() ? "Обложка будет заменена" : "Будет добавлена обложка");
        if (!string.IsNullOrEmpty(curationCoverPath)) changes.Add("Обложка: заменить выбранным изображением");
        string confidence = DValue(proposal, "confidence") switch { "manual" => "Ручной выбор", "review" => "Требует проверки", _ => "Сомнительный вариант: выберите вручную после сравнения" };
        curationReview.Text = $"Источник: {DValue(proposal, "source")} · {confidence}\n{string.Join("\n", proposal.GetProperty("evidence").EnumerateArray().Select(x => x.GetString()))}\n\n{string.Join("\n", changes)}";
    }

    private static async Task LoadCurationCoverAsync(Image image, string encoded)
    {
        image.Tag = encoded; image.Source = null;
        if (string.IsNullOrEmpty(encoded) || encoded.Length > 65536) return;
        try
        {
            byte[] bytes = Convert.FromBase64String(encoded);
            using var stream = new InMemoryRandomAccessStream();
            using (var writer = new DataWriter(stream.GetOutputStreamAt(0))) { writer.WriteBytes(bytes); await writer.StoreAsync(); }
            stream.Seek(0);
            var bitmap = new BitmapImage(); await bitmap.SetSourceAsync(stream);
            if (image.Tag?.ToString() == encoded) image.Source = bitmap;
        }
        catch (Exception) { /* A preview must never prevent reviewing the text tags. */ }
    }

    private async Task PickCurationArtworkAsync()
    {
        if (curationOperation is not null || string.IsNullOrEmpty(curationSelected)) return;
        var picker = new FileOpenPicker();
        foreach (string extension in new[] { ".jpg", ".jpeg", ".png" }) picker.FileTypeFilter.Add(extension);
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        var image = await picker.PickSingleFileAsync();
        if (image is null) return;
        curationCoverPath = image.Path; curationProposal = ""; curationArtwork.Text = "Выбрана обложка: " + image.Name;
    }

    private Task PreviewCurationAsync() => CurationRunAsync(async token =>
    {
        if (string.IsNullOrEmpty(curationSelected)) throw new InvalidOperationException("Выберите трек.");
        var proposal = await CurationRequestAsync(new { action = "desktop_curation_propose", version = 1, id = curationSelected,
            digest = curationDigest, changes = new { title = curationTitle.Text, artist = curationArtist.Text, album = curationAlbum.Text, lyrics = curationLyrics.Text }, artwork_path = curationCoverPath }, token);
        ShowCurationProposal(proposal);
    });

    private async Task TranscribeCurationAsync()
    {
        if (string.IsNullOrEmpty(curationSelected)) { curationStatus.Text = "Выберите трек."; return; }
        string model = AssistantModelPath.Text.Trim();
        if (string.IsNullOrEmpty(model)) { curationStatus.Text = "Локальная модель не найдена. Укажите уже установленную модель в настройках помощника. Автозагрузка отключена."; return; }
        await CurationRunAsync(async token =>
        {
            curationStatus.Text = "Распознавание текста и таймингов на этом ПК. Это может занять несколько минут. Можно нажать «Остановить».";
            curationProgress.IsIndeterminate = true;
            try
            {
                var proposal = await CurationRequestAsync(new { action = "desktop_curation_transcribe", version = 1, id = curationSelected, model_path = model }, token);
                ShowCurationProposal(proposal);
                curationStatus.Text = "Текст распознан локально. Проверьте слова и тайминги перед применением.";
            }
            finally { curationProgress.IsIndeterminate = false; }
        });
    }

    private async Task ApplyCurationAsync()
    {
        string proposal = curationProposal;
        if (string.IsNullOrEmpty(proposal)) { curationStatus.Text = "Сначала нажмите «Посмотреть изменения»."; return; }
        if (!await DesktopConfirmAsync("Записать просмотренные теги?", curationReview.Text + "\n\nИсходный файл целиком сохранится в журнале для отката.", "Записать")) return;
        await CurationRunAsync(async token =>
        {
            await CurationRequestAsync(new { action = "desktop_curation_apply", version = 1, id = proposal, confirmed = true }, token);
            curationProposal = ""; curationCoverPath = "";
            var detail = await CurationRequestAsync(new { action = "desktop_curation_inspect", version = 1, id = curationSelected }, token);
            curationDigest = DValue(detail, "digest"); SetCurationFields(detail.GetProperty("tags"));
            curationProposals.Clear(); curationSuggestions.Items.Clear();
            int index = curationRows.ToList().FindIndex(row => row.Id == curationSelected);
            if (index >= 0) curationRows[index] = curationRows[index] with { Status = "Правка записана · оригинал сохранён" };
            curationStatus.Text = "Правка записана. Оригинал сохранён для отката.";
        });
        await LoadCurationHistoryAsync();
    }

    private Task LoadCurationHistoryAsync() => CurationRunAsync(async token =>
    {
        var result = await CurationRequestAsync(new { action = "desktop_curation_history", version = 1 }, token);
        curationHistory.ItemsSource = result.GetProperty("edits").EnumerateArray().Select(e => new CurationRow(DValue(e, "id"), DValue(e, "name"), DValue(e, "state") switch
            { "applied" => "Правка применена · доступен откат", "reverted" => "Оригинал восстановлен", "not_applied" => "Запись прервана до замены · оригинал не менялся", _ => "Конфликт: файл изменился · оригинал сохранён" })).ToArray();
    });

    private async Task UndoCurationAsync()
    {
        var selected = curationHistory.SelectedItems.OfType<CurationRow>().Select(row => row.Id).ToHashSet();
        // Items arrive in newest-first journal order, independent of click order.
        var edits = curationHistory.Items.OfType<CurationRow>().Where(row => selected.Contains(row.Id)).ToArray();
        if (edits.Length == 0) { curationStatus.Text = "Выберите правки в журнале оригиналов."; return; }
        if (!await DesktopConfirmAsync("Восстановить оригиналы?", $"Выбрано правок: {edits.Length}. Вернуть точное содержимое файлов до этих правок? Обработка идёт от новых к старым; конфликтные файлы не заменяются, остальные могут быть восстановлены.\n\n" + string.Join("\n", edits.Select(edit => edit.Name)), "Восстановить")) return;
        await CurationRunAsync(async token =>
        {
            curationProgress.Maximum = edits.Length; curationProgress.Value = 0;
            int restored = 0; var failures = new List<string>();
            foreach (var edit in edits)
            {
                token.ThrowIfCancellationRequested();
                try
                {
                    await CurationRequestAsync(new { action = "desktop_curation_undo", version = 1, id = edit.Id, confirmed = true }, token);
                    restored++;
                }
                catch (InvalidOperationException error) { failures.Add(edit.Name + ": " + error.Message); }
                curationProgress.Value++;
                curationStatus.Text = $"Восстановлено: {restored} из {edits.Length}; пропущено: {failures.Count}.";
            }
            curationStatus.Text += " Повторите проверку папки для обновления списка.\n" + string.Join("\n", failures.Take(12));
        });
        await LoadCurationHistoryAsync();
    }
}
