"""Synthetic capability and legacy-agent coexistence tests; no real processes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"pc_client"));sys.path.insert(0,str(ROOT/"windows"))
from native_agent_identity import probe_agent,INCOMPATIBLE

REV="3"*40
class NativeIdentityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.source=self.root/"source";self.source.mkdir()
  (self.source/"build-info.json").write_text(json.dumps({"revision":REV}))
  self.report={"process_id":444,"updated_at":1000,"native_audio_ownership":1,"native_agent_revision":REV}
  self.process=Mock(pid=444);self.process.is_running.return_value=True;self.process.create_time.return_value=900
  self.process.cmdline.return_value=["XASS.NativeHelper.exe","--role","background-agent","--agent-child","--source",str(self.source),"--data",str(self.root)]
 def probe(self):
  (self.root/".agent-status.json").write_text(json.dumps(self.report))
  with patch("psutil.Process",return_value=self.process):return probe_agent(self.root,self.source,now=1020)
 def test_native_role_matching_revision_and_capability_is_compatible(self):
  self.assertTrue(self.probe()["compatible"])
 def test_legacy_executable_stays_blocked_even_if_file_claims_capability(self):
  self.process.cmdline.return_value=["XASS.exe","--agent"]
  result=self.probe();self.assertTrue(result["active"]);self.assertFalse(result["compatible"]);self.assertEqual(result["reason"],INCOMPATIBLE)
 def test_native_role_with_different_runtime_context_is_blocked(self):
  self.process.cmdline.return_value[-1]=str(self.root/"other-data")
  self.assertFalse(self.probe()["compatible"])
 def test_older_native_revision_is_blocked(self):
  self.report["native_agent_revision"]="4"*40;self.assertFalse(self.probe()["compatible"])
 def test_missing_or_bool_capability_is_blocked(self):
  for capability in (None,True,2):
   self.report["native_audio_ownership"]=capability;self.assertFalse(self.probe()["compatible"])
 def test_stale_pid_binding_never_blocks_unrelated_process(self):
  self.report["updated_at"]=800;self.assertFalse(self.probe()["active"])
 def test_unrelated_command_is_not_an_agent(self):
  self.process.cmdline.return_value=["browser.exe","--agent-something"];self.assertFalse(self.probe()["active"])
 def test_future_report_is_not_trusted(self):
  self.report["updated_at"]=2000;self.assertFalse(self.probe()["active"])
 def test_access_denied_is_safely_unverified(self):
  import psutil
  (self.root/".agent-status.json").write_text(json.dumps(self.report))
  with patch("psutil.Process",side_effect=psutil.AccessDenied(444)):
   result=probe_agent(self.root,self.source,now=1020)
  self.assertTrue(result["active"]);self.assertFalse(result["compatible"])
 def test_host_never_stops_incompatible_external_agent_on_quit(self):
  from background_agent import DesktopHost
  import threading
  host=DesktopHost.__new__(DesktopHost);host.data=self.root;host.source=self.source;host.process=None;host.external=(444,900)
  host.lock=threading.RLock();host.wanted=True;host.state="running"
  with patch("native_agent_identity.probe_agent",return_value={"active":True,"compatible":False}),patch("psutil.Process") as process:
   host.stop()
  process.assert_not_called();self.assertEqual(host.state,"legacy-external")
 def test_host_rejects_restart_and_archive_while_legacy_writer_active(self):
  from background_agent import DesktopHost
  host=DesktopHost.__new__(DesktopHost);host.data=self.root;host.source=self.source
  with patch("native_agent_identity.probe_agent",return_value={"active":True,"compatible":False}):
   for action in ("host_restart","host_stop","host_archive_move","host_archive_cleanup"):
    with self.assertRaises(ValueError):host.request(action,{})
 def test_music_schema_exposes_compatibility_and_guards_actual_start(self):
  from desktop_music_service import MusicService
  service=MusicService.__new__(MusicService);service.identity={"active":True,"compatible":False,"reason":INCOMPATIBLE}
  service._identity=Mock(return_value=service.identity)
  self.assertTrue(service._legacy_blocked(force=True))
  service.queue=[]
  with self.assertRaises(ValueError):service._start(0)

if __name__=="__main__":unittest.main()
