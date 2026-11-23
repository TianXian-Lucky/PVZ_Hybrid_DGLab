"""Host-only tests: no game, no output device, no LAN pairing."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from host import Host
from waves import frame_at

class Fake:
    def __init__(self,callback):
        self.callback=callback; self.sent=[]; self.stops=0
        self.d={"id":"fixture","ready":True,"connected":True,"channels":{"a":True,"b":True},"limits":{"a":18,"b":11}}
    def view(self): return {"device":self.d,"pair":{"active":False}}
    async def stop(self): self.stops+=1
    async def unpair(self): pass
    async def apply(self,cmd): self.sent.append(cmd); return True

class Standalone(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup); self.tmp=Path(t.name)
        self.h=Host(self.tmp, self.tmp/"events.jsonl", Fake)
        self.h.config["auto_install_mod"]=False
        self.addAsyncCleanup(self.h.disable)

    def test_gain_clips_but_never_changes_frequency_or_zeroes(self):
        raw=["0A141E2800285064"]
        self.assertEqual(frame_at(raw,0,50),"0A141E28003C6464")
        self.assertEqual(frame_at(raw,0,-50,2),"0A141E2800140000")
        self.assertEqual(frame_at(raw,0,0),raw[0])

    def test_temporary_layer_expires_separately_and_is_routed(self):
        h=self.h; h.config["strength_enable"]=True; h.baseline=17
        h.strength({"delta_pct":10,"strength_mode":"rollback","duration_s":3})
        self.assertEqual((h.gain("a"),h.gain("b")),(27,17))
        h.temp["a"]=(27,time.monotonic()-1)
        self.assertEqual(h.gain("a"),17)
        h.config["strength_enable"]=False; self.assertEqual(h.gain("a"),0)

    async def test_persistent_cap_and_explicit_reset_across_restarts(self):
        h=self.h; h.config.update(strength_enable=True,strength_cap=20)
        for n in range(4): h.strength({"delta_pct":10,"strength_mode":"permanent"})
        self.assertEqual(h.baseline,20)
        self.assertEqual(Host(self.tmp,device_factory=Fake).baseline,20)
        await h.action({"action":"reset_baseline"})
        self.assertEqual(Host(self.tmp,device_factory=Fake).baseline,0)
        self.assertFalse(h.permit)

    def test_config_corruption_fails_closed(self):
        (self.tmp/"settings.json").write_text("not JSON",encoding="utf-8")
        h=Host(self.tmp,device_factory=Fake)
        self.assertTrue(h.storage_fault)
        with self.assertRaises(ValueError): h.save()
        self.assertEqual(h.path.read_text(),"not JSON")

    async def test_import_validity_and_persistence(self):
        h=self.h; frames=["0A141E2800285064"]
        await h.action({"action":"import_presets","value":{"自选固定":frames}})
        await h.action({"action":"config","key":"preset","value":"自选固定"})
        h2=Host(self.tmp,device_factory=Fake)
        self.assertEqual(h2.config["preset"],"自选固定")
        self.assertEqual(h2.presets["自选固定"],frames)
        for invalid in ({"坏帧":["0000000000000000"]},{"呼吸":frames},{"自选固定":frames}):
            with self.assertRaises(ValueError): await h.action({"action":"import_presets","value":invalid})

    async def test_invalid_api_setting_does_not_write(self):
        for key,value in (("strength_plant",float("nan")),("strength_mode","garbage"),
                          ("idle_strength",5),("show_damage",1),("preset","缺失")):
            with self.assertRaises(ValueError):
                await self.h.action({"action":"config","key":key,"value":value})
        self.assertFalse(self.h.path.exists())

    async def test_heartbeat_loss_revokes_permission(self):
        h=self.h; await h.enable(); self.assertTrue(h.allowed())
        h.last_ui=time.monotonic()-20
        job=asyncio.create_task(h.watchdog())
        try:
            await asyncio.sleep(.65); self.assertFalse(h.permit)
            h.last_ui=time.monotonic(); self.assertFalse(h.allowed())
        finally:
            job.cancel(); await asyncio.gather(job,return_exceptions=True)

    async def test_route_change_requires_fresh_consent(self):
        h=self.h; await h.enable(); self.assertTrue(h.allowed())
        await h.action({"action":"config","key":"channel","value":"both"})
        self.assertFalse(h.permit); self.assertFalse(h.device.sent)
        await h.action({"action":"arm"})
        self.assertTrue(h.allowed())

    async def test_sdk_session_reads_only_new_records_and_has_no_global_import_requirement(self):
        h=self.h
        await h.enable()
        self.assertTrue(h.status["config_received"])
        self.assertEqual(h.status["session_requests"],0)
        self.assertTrue(h.status["armed"])
        await h.disable()
        self.assertFalse(h.allowed())
        self.assertEqual(h.device.sent,[])

if __name__=="__main__":
    unittest.main()

