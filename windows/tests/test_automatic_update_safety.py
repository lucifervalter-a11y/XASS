"""WinUI hook contracts paired with executable portable .NET race tests."""
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]/"Xass.Native"
class AutomaticUpdateSourceTests(unittest.TestCase):
 def test_prepared_automatic_job_is_revalidated_before_shutdown(self):
  text=(ROOT/"MainWindow.Desktop.cs").read_text()
  section=text[text.index('    private async Task ApplyNativeUpdateAsync('):text.index('    private async Task CheckAutomaticNativeUpdateAsync()')]
  self.assertLess(section.index('prepared = await updateCoordinator.PrepareAsync'),section.index('NativeAutomaticUpdatePolicy.FinishPreparationAsync'))
  self.assertIn('FreshAutomaticNativeGuardsAllowAsync(cancel.Token)',section)
  self.assertIn('() => AutomaticNativeGuardsAllow(cancel.Token)',section)
  self.assertIn('NativeUpdateCoordinator.CancelPreparedAsync(prepared!)',section)
 def test_fresh_check_uses_actual_host_and_music_not_only_cached_ui(self):
  text=(ROOT/"MainWindow.Desktop.cs").read_text()
  section=text[text.index('    private async Task<bool> FreshAutomaticNativeGuardsAllowAsync('):text.index('    private static async Task<bool> IsAutomaticNativeUpdateRejectedAsync(')]
  for value in ('action = "host_status"','action = "snapshot"','Task.WhenAll(hostTask, musicTask)','"copying" or "committing" or "cleaning"'):
   self.assertIn(value,section)
 def test_user_quit_cancels_pending_install_before_stopping_any_owner(self):
  text=(ROOT/"MainWindow.Desktop.cs").read_text()
  section=text[text.index('    private async Task QuitDesktopAsync()'):]
  self.assertLess(section.index('updateCoordinator.CancelPendingAsync'),section.index('StopAllMicrophones()'))
  self.assertIn('if (!nativeUpdateCommitAuthorized)',section)
 def test_cancellation_marker_is_durable_without_async_scheduling_gap(self):
  text=(ROOT/"Services/NativeUpdateCoordinator.cs").read_text()
  section=text[text.index('internal static Task CancelPreparedAsync'):text.index('    private static void CopyRuntime')]
  self.assertIn('Flush(flushToDisk: true)',section);self.assertNotIn('Task.Run',section)
 def test_timeout_cancel_and_retry_controls_are_reset(self):
  text=(ROOT/"MainWindow.Desktop.cs").read_text()
  self.assertIn('availableNativeUpdate = null; installUpdate.IsEnabled = false;',text)
  self.assertGreaterEqual(text.count('updateProgress.Value = 0'),2)
  self.assertIn('catch (TimeoutException)',text);self.assertIn('nativeUpdateCommitAuthorized = false',text)
 def test_rejection_is_digest_based_and_persists_outside_last_result(self):
  text=(ROOT/"MainWindow.Desktop.cs").read_text()
  self.assertIn('NativeUpdatePreferences.RememberRejected(rejected)',text)
  self.assertIn('NativeUpdatePreferences.IsRejected(update.Sha256)',text)
  self.assertNotIn('DValue(last.RootElement, "revision") == update.Revision',text)
  prefs=(ROOT/"Services/NativeUpdatePreferences.cs").read_text()
  self.assertIn('state with { Enabled = enabled }',prefs)
  self.assertIn('state.Rejected.Count > 32',prefs)
if __name__=="__main__":unittest.main()
