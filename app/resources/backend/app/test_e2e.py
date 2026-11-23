"""Integration: actual original core, standalone Host/Device, simulated phones only."""
import asyncio
import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import check_core, panel
from device import CHANNELS, Device
from host import Host, ROOT, plugin
from test_device import app_v4, receive, send, slot
from waves import load_presets

LOOPBACK = "127.0.0.1"


class Phone:
    """Understands only the documented software protocol; cannot drive any hardware."""
    def __init__(self, model):
        self.model = model
        self.sent = asyncio.Queue()
        self.clears = asyncio.Queue()

    async def run(self, ws):
        try:
            async for raw in ws:
                pkt = json.loads(raw)
                if self.model == "v3":
                    text = pkt["message"]
                    if text.startswith("pulse-"):
                        self.sent.put_nowait((text[6].lower(), json.loads(text[8:])))
                    elif text.startswith("clear-"):
                        self.clears.put_nowait(CHANNELS[int(text[-1]) - 1])
                    elif text.startswith("strength-"):
                        raise AssertionError("standalone must never send phone strength commands")
                    continue
                if pkt["type"] != "message":
                    continue
                req = pkt["data"]
                method, data = req["m"], req.get("data", {})
                if method == "devices.get":
                    coyote = slot(a=12)
                    # Vendor V4 declares these limits as numbers; DGHub SDK core accepts only integers.
                    for channel in ("channelA", "channelB"):
                        state = coyote["slotState"][channel]
                        state["intensityMax"] = 25.5
                        state["comfortLimit"]["comfortMax"] = 25.5
                    result = {"devices": [coyote]}
                elif method == "device.op":
                    if data["t"] != 0 or data["s"] != "coyote" or not (100 <= data["d"] <= 200):
                        raise AssertionError("unauthorized operation or duration")
                    self.sent.put_nowait((CHANNELS[data["c"]], data["v"]))
                    result = {"type": 0, "reason": "completed", "channel": data["c"]}
                elif method == "device.op.clear":
                    self.clears.put_nowait(CHANNELS[data["c"]])
                    result = {}
                else:
                    raise AssertionError("unexpected V4 operation")
                await app_v4(ws, {"t": "resp", "reqId": req["reqId"], "result": result})
        except ConnectionClosed:
            pass

    @contextlib.asynccontextmanager
    async def attached(self, host):
        await host.action({"action": "pair", "ip": LOOPBACK, "model": self.model})
        share = host.view()["pair"]["url"]
        url = share.split("#")[2] if self.model == "v3" else parse_qs(urlsplit(share).query)["url"][0]
        if urlsplit(url).hostname != LOOPBACK:
            raise AssertionError("test must not use LAN")
        async with connect(url, proxy=None) as ws:
            if self.model == "v3":
                await receive(ws)
                bind = await receive(ws)
                if bind["message"] != "200":
                    raise AssertionError("V3 didn't bind")
                await send(ws, {**bind, "type": "msg", "message": "strength-12+0+25+25"})
            else:
                hello, attached = await receive(ws), await receive(ws)
                if (hello["type"], attached["type"]) != ("hello", "controller_attached"):
                    raise AssertionError("V4 didn't attach")
            task = asyncio.create_task(self.run(ws))
            try:
                yield
            finally:
                await ws.close()
                await asyncio.wait_for(task, 2)


class TestStandaloneEndToEnd(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        check_core()

    async def test_panel_auto_pair_uses_current_interface_for_qr(self):
        # Exercise the HTTP request posted by an untouched address selector.
        # Simulate a machine that joins its network after the panel was opened.
        with tempfile.TemporaryDirectory() as tmp, patch("device.lan_ipv4", return_value=[]) as addresses:
            host = Host(Path(tmp), Path(tmp)/"events.jsonl",
                        device_factory=lambda cb: Device(cb, _test_loopback=True))
            async with panel(host) as (base, key), ClientSession() as http:
                endpoint = base + "/api/action"
                headers = {"X-PVZ-Session": key}
                addresses.return_value = [LOOPBACK]
                for model, selection in (("v3", {"ip":"auto"}), ("v4", {"ip":""}), ("v4", {})):
                    async with http.post(endpoint, headers=headers,
                                         json={"action":"pair", "model":model, **selection}) as r:
                        self.assertEqual(r.status, 200)
                        state = await r.json()
                    pair = state["pair"]
                    self.assertTrue(pair["active"] and pair["qr"].startswith("data:image/png;base64,"))
                    self.assertEqual(pair["ip"], LOOPBACK)
                    raw = (pair["url"].split("#")[2] if model == "v3"
                           else parse_qs(urlsplit(pair["url"]).query)["url"][0])
                    ws = urlsplit(raw)
                    self.assertEqual((ws.scheme,ws.hostname), ("ws",LOOPBACK))
                    self.assertGreater(ws.port, 0)
                    self.assertFalse(state["enabled"] or state["output_enabled"] or state["device"]["connected"])
                    async with http.post(endpoint, headers=headers, json={"action":"unpair"}) as r:
                        self.assertEqual(r.status, 200)
                        self.assertFalse((await r.json())["pair"]["active"])

    async def wait_until(self, pred):
        async with asyncio.timeout(3):
            while not pred():
                await asyncio.sleep(0.02)

    async def quiet(self, phone):
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(phone.sent.get(), timeout=0.24)

    def write(self, seq):
        data = {"schema": 1, "event": "plant_damage", "session": "test-game",
                "seq": seq, "ts": time.time(), "target": str(seq),
                "target_type": "PlantPot", "life_lost": 0, "armor_lost": 3,
                "shield_absorbed": 0}
        with self.events.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")

    async def scenario(self, model):
        with tempfile.TemporaryDirectory(prefix="pvz-offline-e2e-") as name:
            doc = Path(name)
            self.events = doc / "events.jsonl"
            fake_mods = doc / "No real game Mods"
            with (patch.object(plugin.mod_install, "game_mods_dir", return_value=fake_mods),
                  patch.object(plugin.mod_install, "install", side_effect=AssertionError("write disabled")) as installer):
                host = Host(folder=doc / "state", event_path=self.events,
                            device_factory=lambda cb: Device(cb, _test_loopback=True))
                self.write(1)  # pre-existing valid history must not replay
                await host.action({"action": "config", "key": "auto_install_mod", "value": False})
                await host.action({"action": "enable"})
                self.assertTrue(host.view()["enabled"])
                self.assertFalse(host.view()["output_enabled"])
                self.assertEqual(host.view()["cards"], [])
                try:
                    first = Phone(model)
                    async with first.attached(host):
                        await self.wait_until(lambda: host.device_view()["ready"])
                        identity = host.device_view()["id"]
                        self.assertTrue(identity)
                        self.assertFalse(host.view()["output_enabled"])
                        self.assertEqual(host.device_view()["limits"]["a"], 25.5 if model == "v4" else 25)
                        self.assertIs(type(host.info()["max_strength_a"]), int)
                        self.assertEqual(host.info()["max_strength_a"], 25)
                        self.write(2)  # has visual evidence before output is authorized
                        await self.wait_until(lambda: len(host.cards) == 1)
                        await self.quiet(first)
                        await host.action({"action": "arm"})
                        self.assertTrue(host.view()["output_enabled"])
                        await self.quiet(first)  # seq=1 and seq=2 still must not replay
                        self.write(3)
                        channel, frames = await asyncio.wait_for(first.sent.get(), 3)
                        self.assertEqual(channel, "a")
                        self.assertEqual(frames, host.presets["呼吸"][:2])
                        self.assertEqual(host.device_view()["levels"]["a"], 12)
                    await self.wait_until(lambda: not host.device_view()["connected"])
                    self.assertFalse(host.view()["output_enabled"])
                    self.assertTrue(host.view()["enabled"])  # Plugin remains observation-only.

                    second = Phone(model)
                    async with second.attached(host):
                        await self.wait_until(lambda: host.device_view()["ready"])
                        self.assertNotEqual(host.device_view()["id"], identity)
                        self.assertFalse(host.view()["output_enabled"])
                        n = len(host.cards)
                        self.write(4)
                        await self.wait_until(lambda: len(host.cards) > n)
                        await self.quiet(second)
                        # Classic core cooldown is five seconds across phone reconnects.
                        # Wait from the observed packet (always at/after admission).
                        await asyncio.sleep(4.9)
                        await host.action({"action": "arm"})
                        await self.quiet(second)
                        self.write(5)
                        self.assertEqual((await asyncio.wait_for(second.sent.get(), 3))[0], "a")
                        await host.action({"action": "disable"})
                        self.assertEqual(await asyncio.wait_for(second.clears.get(), 3), "a")
                        self.assertFalse(host.view()["output_enabled"])
                        self.assertFalse(host.view()["enabled"])
                        self.assertTrue(host.device_view()["connected"])
                        self.write(6)
                        await self.quiet(second)
                        self.assertEqual(host.device_view()["levels"]["a"], 12)
                    self.assertFalse(fake_mods.exists())
                    installer.assert_not_called()
                finally:
                    await host.disable()
                    await host.device.shutdown()

    async def test_v3_event_to_real_device_wire(self):
        await self.scenario("v3")

    async def test_v4_event_to_real_device_wire(self):
        await self.scenario("v4")

    def test_every_stock_frame_matches_adapter_frequency_and_amplitude_constraints(self):
        presets = load_presets(ROOT)
        self.assertEqual(len(presets), 24)
        count = 0
        for label, frames in presets.items():
            for index, item in enumerate(frames):
                raw = bytes.fromhex(item)
                self.assertEqual(len(raw), 8)
                self.assertTrue(all(10 <= n <= 240 for n in raw[:4]), (label, index))
                self.assertTrue(all(0 <= n <= 100 for n in raw[4:]), (label, index))
                count += 1
        self.assertEqual(count, 763)


if __name__ == "__main__":
    unittest.main(verbosity=2)

