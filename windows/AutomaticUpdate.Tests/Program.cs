using System.Text.Json;
using Xass.Native.Services;

internal static class Program
{
    private static int passed;
    private static NativeAutomaticGuards Good => new(false, false, false, true, false, false, "idle", false, false, false, false);
    private static readonly string Sha = new('a', 64), NewSha = new('b', 64);
    private static void Check(bool condition, string name)
    { if (!condition) throw new Exception("FAILED: " + name); passed++; Console.WriteLine("PASS: " + name); }
    private static async Task ChangedGuard(string name, Func<NativeAutomaticGuards, NativeAutomaticGuards> change)
    {
        NativeAutomaticGuards state = Good; int cancel = 0, quit = 0;
        bool committed = await NativeAutomaticUpdatePolicy.FinishPreparationAsync(
            async () => { await Task.Yield(); state = change(state); return true; },
            () => NativeAutomaticUpdatePolicy.AllowsShutdown(state),
            () => { cancel++; return Task.CompletedTask; }, () => { quit++; return Task.CompletedTask; });
        Check(!committed && cancel == 1 && quit == 0, name);
    }
    public static async Task<int> Main()
    {
        string root = Path.Combine(Path.GetTempPath(), "xass-auto-safety-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(root);
        try
        {
            Check(NativeAutomaticUpdatePolicy.AllowsShutdown(Good), "idle opted-in state permits final shutdown");
            await ChangedGuard("restored window during preparation cancels prepared job", s => s with { WindowVisible = true });
            await ChangedGuard("opt-out during preparation cancels prepared job", s => s with { Enabled = false });
            await ChangedGuard("pairing begun during preparation prevents shutdown", s => s with { Pairing = true });
            await ChangedGuard("new music action prevents shutdown", s => s with { MusicBusy = true });
            foreach (string playback in new[] { "playing", "paused", "loading", "stopping" })
                await ChangedGuard("playback " + playback + " prevents shutdown", s => s with { MusicState = playback });
            await ChangedGuard("archive request pending prevents shutdown", s => s with { ArchiveBusy = true });
            await ChangedGuard("pending settings save prevents shutdown", s => s with { SettingsBusy = true });
            await ChangedGuard("assistant request prevents shutdown", s => s with { AssistantBusy = true });
            await ChangedGuard("cancel request before commit prevents shutdown", s => s with { CancellationRequested = true });
            await ChangedGuard("closing app is not force-closed a second time", s => s with { Quitting = true });

            int cancelled = 0, shut = 0;
            bool committed = await NativeAutomaticUpdatePolicy.FinishPreparationAsync(() => Task.FromResult(false), () => true,
                () => { cancelled++; return Task.CompletedTask; }, () => { shut++; return Task.CompletedTask; });
            Check(!committed && cancelled == 1 && shut == 0, "fresh backend reports busy: cancellation, no quit");
            cancelled = shut = 0;
            try
            {
                await NativeAutomaticUpdatePolicy.FinishPreparationAsync(() => throw new IOException("backend unavailable"), () => true,
                    () => { cancelled++; return Task.CompletedTask; }, () => { shut++; return Task.CompletedTask; });
                throw new Exception("probe failure escaped test");
            }
            catch (IOException) { }
            Check(cancelled == 1 && shut == 0, "failed final backend read cancels rather than forcing quit");
            cancelled = shut = 0;
            committed = await NativeAutomaticUpdatePolicy.FinishPreparationAsync(() => Task.FromResult(true), () => true,
                () => { cancelled++; return Task.CompletedTask; }, () => { shut++; return Task.CompletedTask; });
            Check(committed && cancelled == 0 && shut == 1, "unchanged fresh guards commit exactly once");
            cancelled = 0;
            try
            {
                await NativeAutomaticUpdatePolicy.FinishPreparationAsync(() => Task.FromResult(true), () => true,
                    () => { cancelled++; return Task.CompletedTask; }, () => throw new IOException("shutdown failed"));
            }
            catch (IOException) { }
            Check(cancelled == 1, "failed shutdown cancels already prepared installer");

            string job = Path.Combine(root, "job-" + new string('c', 32)); Directory.CreateDirectory(job);
            await NativeUpdateCoordinator.CancelPreparedAsync(new(job), root);
            Check(File.Exists(Path.Combine(job, "cancel")) && new FileInfo(Path.Combine(job, "cancel")).Length > 0, "prepared cancellation marker durably exists");
            await NativeUpdateCoordinator.CancelPreparedAsync(new(job), root);
            Check(File.Exists(Path.Combine(job, "cancel")), "prepared cancellation is idempotent");
            File.Delete(Path.Combine(job, "cancel"));
            var coordinator = new NativeUpdateCoordinator();
            typeof(NativeUpdateCoordinator).GetProperty("PendingJob", System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance)!.SetValue(coordinator, new PreparedNativeUpdate(job));
            await coordinator.CancelPendingAsync(root);
            Check(File.Exists(Path.Combine(job, "cancel")), "user Quit cancels pending job before releasing parent PID");
            string outside = Path.Combine(root, "other"); Directory.CreateDirectory(outside);
            try { await NativeUpdateCoordinator.CancelPreparedAsync(new(outside), root); throw new Exception("unsafe path accepted"); }
            catch (InvalidOperationException) { }
            Check(!File.Exists(Path.Combine(outside, "cancel")), "unknown job cannot receive cancellation marker");

            string prefs = Path.Combine(root, "preferences.json");
            var store = new NativeUpdatePreferencesStore(prefs);
            Check(!store.Load(), "new automatic updates default off");
            Check(store.LoadChannel(NativeReleaseChannel.Test) == NativeReleaseChannel.Test
                && store.LoadChannel(NativeReleaseChannel.Stable) == NativeReleaseChannel.Stable, "old preferences default to installed distribution without changing it");
            store.Save(true); store.SaveChannel(NativeReleaseChannel.Stable); store.RememberRejected(Sha);
            var reopened = new NativeUpdatePreferencesStore(prefs);
            Check(reopened.LoadChannel(NativeReleaseChannel.Test) == NativeReleaseChannel.Stable && reopened.Load() && reopened.IsRejected(Sha),
                "channel survives restart independently of installed beta, opt-in and rejection history");
            store.Save(false); store.RememberRejected(NewSha);
            Check(store.LoadChannel(NativeReleaseChannel.Test) == NativeReleaseChannel.Stable, "other preference writes preserve selected channel");
            store.SaveChannel(NativeReleaseChannel.Test);
            Check(!store.Load() && store.IsRejected(Sha) && store.IsRejected(NewSha), "changing channel does not enable automatic updates or erase rejected releases");
            File.Delete(prefs);

            var selection = new NativeUpdateSelection(NativeReleaseChannel.Stable);
            var operation = selection.TryBegin()!;
            Check(operation.Channel == NativeReleaseChannel.Stable && selection.IsBusy, "operation captures selected channel");
            Check(selection.TryBegin() is null, "concurrent manual and automatic checks cannot overwrite each other");
            int writes = 0;
            Check(!await Task.Run(() => selection.TrySelect(NativeReleaseChannel.Test, _ => writes++)) && writes == 0,
                "selection during check download or install is blocked before persistence");
            operation.Dispose();
            Check(selection.TrySelect(NativeReleaseChannel.Test, _ => writes++) && writes == 1 && selection.Selected == NativeReleaseChannel.Test,
                "channel can change after operation completion");
            Check(operation.Channel == NativeReleaseChannel.Stable, "old result retains captured channel after later selection");
            var second = selection.TryBegin()!; operation.Dispose();
            Check(selection.IsBusy && second.Channel == NativeReleaseChannel.Test, "disposing stale operation cannot unlock a newer operation");
            second.Dispose();
            try { selection.TrySelect(NativeReleaseChannel.Stable, _ => throw new IOException("disk full")); }
            catch (IOException) { }
            Check(selection.Selected == NativeReleaseChannel.Test && !selection.IsBusy, "failed save leaves previous channel and permits retry");

            var candidate = new NativeUpdate("0.21.4", new string('c', 40), "native-stable-fixture", new Uri("https://github.com/fixture"), 1024, Sha, "native");
            using var marker = JsonDocument.Parse(JsonSerializer.Serialize(new {
                app_id = "B4D7E8B9-9C58-4C36-A432-D114393006D8", distribution = "native-test", version = "0.21.4", revision = candidate.Revision }));
            Check(NativeUpdateCoordinator.ValidateInstallation(candidate, marker.RootElement), "same SHA beta-to-release requests explicit channel migration");
            foreach (var blocked in new[] { candidate with { Version = "0.21.2" }, candidate with { Version = "unknown" }, candidate with { Distribution = "other" } })
            {
                try { NativeUpdateCoordinator.ValidateInstallation(blocked, marker.RootElement); throw new Exception("unsafe update accepted"); }
                catch (InvalidOperationException) { }
                Check(true, "coordinator rejects downgrade unknown version or unknown channel: " + blocked.Version + "/" + blocked.Distribution);
            }
            try { NativeUpdateVersionPolicy.RequireInstallable(candidate, NativeReleaseChannel.Test, "", "0.21.3", "native-test"); throw new Exception("stale channel accepted"); }
            catch (InvalidOperationException) { }
            Check(true, "captured selected channel must match candidate at installation");
            Check(NativeUpdateVersionPolicy.Parse("0.21.4") is not null && NativeUpdateVersionPolicy.Parse("0.21.4.0") is null,
                "three numeric version components match companion updater contract");
            Check(NativeUpdateVersionPolicy.Parse("100000.0.0") is null && NativeUpdateVersionPolicy.Parse("99999.0.0") is not null,
                "version component bounds match companion updater contract");

            store.Save(true); store.RememberRejected(Sha);
            Check(store.Load() && store.IsRejected(Sha.ToUpperInvariant()), "rejected SHA persists without clearing opt-in");
            store.Save(false); store.Save(true);
            Check(store.IsRejected(Sha), "toggling automatic setting preserves rejected SHA");
            Check(!store.IsRejected(NewSha), "newer different SHA is eligible");
            Check(!NativeAutomaticUpdatePolicy.MayAttemptRelease(Sha, true, store.IsRejected), "automatic retry of rejected SHA blocked");
            Check(NativeAutomaticUpdatePolicy.MayAttemptRelease(Sha, false, _ => throw new Exception("manual retry must bypass history")), "explicit manual retry remains allowed");
            Check(NativeAutomaticUpdatePolicy.MayAttemptRelease(NewSha, true, store.IsRejected), "automatic new SHA allowed");
            Check(store.IsRejected(Sha), "manual retry does not erase protection for later failures");
            for (int i = 0; i < 40; i++) store.RememberRejected(i.ToString("x64"));
            using (var document = JsonDocument.Parse(File.ReadAllText(prefs)))
                Check(document.RootElement.GetProperty("rejected_sha256").GetArrayLength() == 32, "rejected release history is bounded");
            File.WriteAllText(prefs, "{invalid");
            Check(!store.Load() && store.IsRejected(NewSha), "corrupt preferences fail closed for automatic updates");
            Check(store.LoadChannel(NativeReleaseChannel.Stable) == NativeReleaseChannel.Stable, "corrupt preferences never opt stable installation into beta");
            File.WriteAllText(prefs, "{\"schema\":1,\"automatic_enabled\":true,\"selected_distribution\":\"unknown\"}");
            Check(!store.Load() && store.IsRejected(NewSha) && store.LoadChannel(NativeReleaseChannel.Stable) == NativeReleaseChannel.Stable,
                "unknown persisted channel fails closed without silently enabling beta");

            string rejection = JsonSerializer.Serialize(new { phase = "rolled-back", rejected_sha256 = Sha });
            Check(NativeAutomaticUpdatePolicy.RejectedSha256(rejection) == Sha, "health-rejected digest read from rollback state");
            Check(NativeAutomaticUpdatePolicy.RejectedSha256(JsonSerializer.Serialize(new { phase = "failed", sha256 = Sha })) is null,
                "network or preparation failure is not blacklisted");
            try { NativeAutomaticUpdatePolicy.RejectedSha256(new string('x', 65537)); throw new Exception("oversized accepted"); }
            catch (InvalidDataException) { }
            Check(true, "oversized rejection state is rejected");
            string statePath = Path.Combine(root, "last-result.json"); File.WriteAllText(statePath, rejection);
            Check(await NativeAutomaticUpdatePolicy.ReadRejectedStateAsync(statePath, CancellationToken.None) == Sha, "bounded persisted rejection read");
            File.WriteAllText(statePath, new string('x', 65537));
            try { await NativeAutomaticUpdatePolicy.ReadRejectedStateAsync(statePath, CancellationToken.None); throw new Exception("oversized file accepted"); }
            catch (InvalidDataException) { }
            Check(true, "oversized state file never allocated unboundedly");
            Console.WriteLine($"Automatic update safety: {passed} portable tests passed."); return 0;
        }
        finally { Directory.Delete(root, true); }
    }
}
