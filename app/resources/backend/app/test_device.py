"""Loopback-only fake phones; no physical hardware or production LAN listeners."""
import asyncio
import json
import socket
import unittest
from unittest.mock import call, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession
from websockets.asyncio.client import ClientConnection, connect
from websockets.frames import Opcode
from websockets.exceptions import ConnectionClosed, InvalidStatus

from device import Device, _LanAddress, _bind_pair_socket, _rank_lan

PULSE = {"kind": "pulse", "channel": "a", "frames": ["0A0A0A0A01010101", "0A0A0A0A02020202"]}
IP = "127.0.0.1"


def snap(*devices):
    return {"t": "ev", "ev": "devices.snapshot", "devices": list(devices)}


def slot(sid="coyote", *, a=8, b=0):
    def ch():
        return {"isMuted": False, "intensityMax": 25, "comfortLimit": {
            "absoluteMax": 50, "comfortMax": 25, "overheat": False}}
    return {"slotId": sid, "name": f"郊狼 {sid}", "type": "COYOTE_030", "props": {
        "connectState": "connected", "channelAStatus": 0, "channelBStatus": 0,
        "intensityA": a, "intensityB": b}, "slotState": {"hasDevice": True, "channelA": ch(), "channelB": ch()}}


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


async def send(ws, obj):
    await ws.send(json.dumps(obj))


async def app_v4(ws, payload, **envelope):
    await send(ws, {"type": "message", "data": payload, **envelope})


class DisconnectablePongClient(ClientConnection):
    """Only used by the test: simulate a half-open socket losing native Pong replies."""
    suppress_pong = False

    def __init__(self, protocol, **kwargs):
        super().__init__(protocol, **kwargs)
        send_frame = protocol.send_frame

        def gated_send(frame):
            if not (frame.opcode == Opcode.PONG and self.suppress_pong):
                return send_frame(frame)
            return None

        protocol.send_frame = gated_send


class TestDevices(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("device.V3_PORT", 0))
        self.updates = 0
        self.device = Device(self.changed, _test_loopback=True)
        self.addAsyncCleanup(self.device.shutdown)

    def changed(self):
        self.updates += 1

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    def state(self):
        return self.device.view()["device"]

    async def qr(self, model):
        view = await self.device.pair(IP, model)
        self.assertTrue(view["pair"]["active"])
        self.assertTrue(view["pair"]["qr"].startswith("data:image/png;base64,"))
        url = view["pair"]["url"]
        if model == "v3":
            self.assertTrue(url.startswith("https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"))
            return url.split("#")[2]
        split = urlsplit(url)
        self.assertEqual((split.netloc, split.path), ("dungeon-lab.cn", "/s/"))
        params = parse_qs(split.query)
        self.assertEqual(params["v"], ["1"])
        self.assertEqual(params["action"], ["socket"])
        return params["url"][0]

    async def v3_handshake(self, ws):
        initial = await receive(ws)
        bind = await receive(ws)
        self.assertEqual(initial, {"type": "bind", "clientId": bind["targetId"], "targetId": "", "message": "targetId"})
        self.assertEqual(bind["message"], "200")
        await self.until(lambda: self.state()["connected"])
        return {k: bind[k] for k in ("clientId", "targetId")}

    async def v4_handshake(self, ws, url):
        hello = await receive(ws)
        att = await receive(ws)
        req = await receive(ws)
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(att, {"type": "controller_attached", "clientId": parse_qs(urlsplit(url).query)["tid"][0]})
        self.assertEqual(req["data"]["m"], "devices.get")
        return (hello["clientId"], req["data"]["reqId"])

    async def v3_state(self, ws, ids, data):
        await send(ws, {"type": "msg", "message": "strength-" + data, **ids})

    def test_lan_preference_uses_link_gateway_metric_and_no_unusable_addrs(self):
        rows = [
            _LanAddress("172.30.224.1", False, False, 5, 40),  # Hyper-V
            _LanAddress("10.200.0.9", False, True, 1, 11),    # VPN default
            _LanAddress("192.168.10.3", True, False, 2, 24),  # physical private network
            _LanAddress("192.168.1.2", True, True, 25, 21),
            _LanAddress("192.168.2.2", True, True, 35, 25),
            _LanAddress("192.168.1.2", False, True, 1, 30),   # duplicate from weaker route
            _LanAddress("127.0.0.1", True, True, 0, 1),
            _LanAddress("169.254.2.3", True, True, 0, 2),
            _LanAddress("8.8.8.8", True, True, 0, 3),
        ]
        self.assertEqual(_rank_lan(rows), ["192.168.1.2", "192.168.2.2", "192.168.10.3",
                                           "10.200.0.9", "172.30.224.1"])

    async def test_auto_reenumerates_and_binds_real_loopback_for_v3_and_v4(self):
        for model, request in (("v3", None), ("v4", "auto"), ("v4", "")):
            with self.subTest(model=model, request=request):
                with patch("device.lan_ipv4", side_effect=[["192.168.100.6"], [IP]]) as discover:
                    dev = Device(_test_loopback=True)
                    self.addAsyncCleanup(dev.shutdown)
                    self.assertEqual(dev.view()["pair"]["ips"], ["192.168.100.6"])
                    paired = (await dev.pair(request, model))["pair"]
                self.assertEqual(discover.call_count, 2)
                self.assertEqual(paired["ip"], IP)
                self.assertEqual(paired["ips"], [IP])
                self.assertTrue(paired["active"])
                url = (paired["url"].split("#")[2] if model == "v3"
                       else parse_qs(urlsplit(paired["url"]).query)["url"][0])
                self.assertEqual(urlsplit(url).hostname, IP)
                async with connect(url, proxy=None) as ws:
                    if model == "v3":
                        self.assertEqual((await receive(ws))["type"], "bind")
                        self.assertEqual((await receive(ws))["message"], "200")
                    else:
                        self.assertEqual((await receive(ws))["type"], "hello")
                        self.assertEqual((await receive(ws))["type"], "controller_attached")
                    self.assertFalse(dev.view()["device"]["ready"])
                await dev.unpair()
                state = dev.view()["pair"]
                self.assertFalse(state["active"])
                self.assertEqual((state["ip"], state["url"], state["qr"]), ("", "", ""))

    async def test_auto_distinguishes_no_lan_from_stale_and_rejects_non_private(self):
        bad = ["0.0.0.0", "127.0.0.1", "8.8.8.8", "169.254.2.3", "::1"]
        with patch("device.lan_ipv4", return_value=bad), patch("device._bind_pair_socket") as bind:
            dev = Device()
            self.addAsyncCleanup(dev.shutdown)
            for model in ("v3", "v4"):
                with self.assertRaisesRegex(ValueError, "没找到可用的局域网"):
                    await dev.pair("auto", model)
            for addr in bad:
                with self.assertRaisesRegex(ValueError, "请选择有效"):
                    await dev.pair(addr, "v4")
            with self.assertRaisesRegex(ValueError, "已不在本机有效网卡"):
                await dev.pair("192.168.3.4", "v4")
            self.assertFalse(dev.view()["pair"]["active"])
            bind.assert_not_called()

    async def test_auto_retries_bind_only_but_manual_never_falls_back(self):
        first = "192.168.100.8"
        def bind(addr, preferred):
            if addr == first:
                raise OSError("fixture address vanished")
            return _bind_pair_socket(addr, preferred)

        with patch("device.lan_ipv4", return_value=[first, IP]):
            for model in ("v3", "v4"):
                with self.subTest(model=model):
                    with patch("device._bind_pair_socket", side_effect=bind) as listener:
                        state = (await self.device.pair("auto", model))["pair"]
                        self.assertEqual(state["ip"], IP)
                        self.assertEqual(listener.call_args_list, [call(first, 0), call(IP, 0)])
                        await self.device.unpair()
                        listener.reset_mock()
                        with self.assertRaisesRegex(ValueError, "所选 IPv4 无法监听"):
                            await self.device.pair(first, model)
                        listener.assert_called_once_with(first, 0)
                        self.assertFalse(self.device.view()["pair"]["active"])
                    with patch("device._bind_pair_socket", side_effect=bind) as listener:
                        state = (await self.device.pair(IP, model))["pair"]
                        listener.assert_called_once_with(IP, 0)
                        self.assertEqual(state["ip"], IP)
                        await self.device.unpair()
            with (patch("device._bind_pair_socket", wraps=_bind_pair_socket) as listener,
                  patch("device.lan_ipv4", return_value=[IP, first]),
                  patch("device.serve", side_effect=RuntimeError("fixture server failed"))):
                with self.assertRaisesRegex(RuntimeError, "fixture server failed"):
                    await self.device.pair("auto", "v4")
                listener.assert_called_once_with(IP, 0)
                self.assertEqual(self.device.view()["pair"]["ip"], "")

    async def test_v3_paired_requires_telemetry_and_never_sets_strength(self):
        self.assertFalse(await self.device.apply(PULSE))
        url = await self.qr("v3")
        async with connect(url) as ws:
            ids = await self.v3_handshake(ws)
            self.assertFalse(self.state()["ready"])
            self.assertEqual(self.state()["levels"], {"a": None, "b": None})
            self.assertFalse(await self.device.apply(PULSE))
            await self.v3_state(ws, ids, "8+0+20+20")
            await self.until(lambda: self.state()["ready"])
            count = self.updates
            await self.v3_state(ws, ids, "8+0+20+20")
            await asyncio.sleep(0.03)
            self.assertEqual(self.updates, count)
            self.assertEqual(self.state()["channels"], {"a": True, "b": False})
            self.assertFalse(await self.device.apply({**PULSE, "channel": "b"}))
            self.assertFalse(await self.device.apply({**PULSE, "frames": ["0A0A0A0AFFFFFFFF"]}))
            self.assertTrue(await self.device.apply(PULSE))
            pkt = await receive(ws)
            self.assertEqual(pkt, {"type": "msg", "message": 'pulse-A:["0A0A0A0A01010101","0A0A0A0A02020202"]', **ids})
            await self.v3_state(ws, ids, "0+0+20+20")
            self.assertEqual((await receive(ws))["message"], "clear-1")
            await self.until(lambda: not self.state()["ready"])
            self.assertFalse(await self.device.apply(PULSE))

    async def test_v3_preferred_port_and_collision(self):
        with socket.socket() as taken:
            taken.bind((IP, 0)); taken.listen(1)
            port = taken.getsockname()[1]
            with patch("device.V3_PORT", port):
                url = await self.qr("v3")
                self.assertNotEqual(urlsplit(url).port, port)
                self.assertEqual(urlsplit(url).port, self.device.view()["pair"]["port"])
                await self.device.unpair()
        with patch("device.V3_PORT", port):
            self.assertEqual(urlsplit(await self.qr("v3")).port, port)

    async def test_v3_skips_preoccupied_wildcard_even_when_bind_might_succeed(self):
        with socket.socket() as sample:
            sample.bind((IP, 0))
            preferred = sample.getsockname()[1]
        with patch("device.V3_PORT", preferred), patch("device._preferred_free", return_value=False) as verify:
            self.assertNotEqual(urlsplit(await self.qr("v3")).port, preferred)
            verify.assert_called_once_with(IP, preferred)

    async def test_phone_network_check_is_not_a_pair_and_diagnostics_are_safe(self):
        url = await self.qr("v3")
        base = url.rsplit("/", 1)[0]
        httpbase = base.replace("ws:", "http:", 1)
        async with ClientSession() as http, http.get(httpbase + "/check") as reply:
            self.assertEqual(reply.status, 200)
            self.assertIn("已连到电脑", await reply.text())
            self.assertEqual(reply.headers["Cache-Control"], "no-store")
        self.assertIn("测试已到达", self.device.view()["pair"]["diagnostic"])
        self.assertTrue(self.device.view()["pair"]["active"])
        self.assertFalse(self.state()["connected"])
        with self.assertRaises(InvalidStatus):
            async with connect(url, origin="https://example.test", proxy=None):
                pass
        self.assertIn("来源校验未通过", self.device.view()["pair"]["diagnostic"])
        async with connect(base + "/dummy", proxy=None) as wrong:
            with self.assertRaises(ConnectionClosed):
                await receive(wrong)
        self.assertIn("链接不匹配", self.device.view()["pair"]["diagnostic"])
        async with connect(url, origin=httpbase, proxy=None) as ws:
            await self.v3_handshake(ws)
            self.assertFalse(self.state()["ready"])
            self.assertFalse(self.device.view()["pair"]["port"])
        await self.device.unpair()
        self.assertFalse(self.device.view()["pair"]["diagnostic"])

    async def test_v3_secret_and_single_phone(self):
        url = await self.qr("v3")
        base = url.rsplit("/", 1)[0]
        async with connect(base + "/incorrect") as wrong:
            with self.assertRaises(ConnectionClosed):
                await receive(wrong)
        with self.assertRaises(InvalidStatus):
            async with connect(url, origin="https://example.test"):
                pass
        async with connect(url) as ws:
            ids = await self.v3_handshake(ws)
            original = self.state()["id"]
            self.assertFalse(self.device.view()["pair"]["active"])
            async with connect(url) as duplicate:
                with self.assertRaises(ConnectionClosed):
                    await receive(duplicate)
            self.assertEqual(self.state()["id"], original)
            await self.v3_state(ws, {**ids, "targetId": "wrong"}, "9+0+20+20")
            with self.assertRaises(ConnectionClosed):
                await receive(ws)
            await self.until(lambda: not self.state()["connected"])
        self.assertFalse(await self.device.apply(PULSE))

    async def test_v3_legacy_root_explicit_bind(self):
        url = await self.qr("v3")
        root, secret = url.rsplit("/", 1)
        async with connect(root + "/") as ws:
            initial = await receive(ws)
            self.assertFalse(self.state()["connected"])
            ids = {"clientId": secret, "targetId": initial["clientId"]}
            await send(ws, {"type": "bind", "message": "bind", **ids})
            self.assertEqual(await receive(ws), {"type": "bind", "message": "200", **ids})
            await self.until(lambda: self.state()["connected"])
            self.assertFalse(self.state()["ready"])

    async def test_v3_legacy_app_speaks_first_utf8_bind_and_inert_heartbeat(self):
        for route in ("/", "/{id}", "/?tid={id}", "/?targetId={id}"):
            with self.subTest(route=route):
                base, secret = (await self.qr("v3")).rsplit("/", 1)
                async with connect(base + route.format(id=secret), proxy=None) as ws:
                    initial = await receive(ws)
                    ids = {"clientId": secret, "targetId": initial["clientId"]}
                    await ws.send(json.dumps({"type":"bind", "message":"DGLAB", **ids}).encode())
                    self.assertEqual(await receive(ws), {"type":"bind", "message":"200", **ids})
                    await send(ws, {"type":"heartbeat", "message":"200",
                                    "clientId":initial["clientId"], "targetId":secret})
                    self.assertFalse(self.state()["ready"])
                    await ws.send(json.dumps({"type":"msg", "message":"strength-4+0+22+20", **ids}).encode())
                    await self.until(lambda: self.state()["ready"])
                    self.assertEqual(self.state()["levels"]["a"], 4)
                await self.device.unpair()

    async def test_v3_keeps_last_report_on_pongs_only_for_same_live_session(self):
        with patch("device.V3_PING_INTERVAL", 0.04), patch("device.V3_PING_TIMEOUT", 0.12):
            url = await self.qr("v3")
            async with connect(url, create_connection=DisconnectablePongClient, proxy=None) as ws:
                ids = await self.v3_handshake(ws)
                await self.until(lambda: self.device._v3_alive_at > 0)
                self.assertFalse(self.state()["ready"])
                self.assertFalse(await self.device.apply(PULSE))
                await self.v3_state(ws, ids, "5+0+20+20")
                await self.until(lambda: self.state()["ready"])
                original_identity = self.state()["id"]
                reported, pong = self.device._state_at, self.device._v3_alive_at
                await asyncio.sleep(0.21)
                self.assertEqual(self.device._state_at, reported)  # no fake telemetry refresh
                self.assertGreater(self.device._v3_alive_at, pong)
                self.assertEqual(self.state()["id"], original_identity)
                self.assertTrue(self.state()["ready"])
                self.assertTrue(await self.device.apply(PULSE))
                self.assertTrue((await receive(ws))["message"].startswith("pulse-A:"))
                await self.device.stop()
                self.assertEqual((await receive(ws))["message"], "clear-1")

                self.assertTrue(await self.device.apply(PULSE))
                self.assertTrue((await receive(ws))["message"].startswith("pulse-A:"))
                ws.suppress_pong = True
                self.assertEqual((await receive(ws))["message"], "clear-1")
                with self.assertRaises(ConnectionClosed):
                    await receive(ws)
                await self.until(lambda: not self.state()["connected"])
                self.assertFalse(await self.device.apply(PULSE))

            fresh_url = await self.qr("v3")
            async with connect(fresh_url, proxy=None) as ws:
                ids = await self.v3_handshake(ws)
                await self.until(lambda: self.device._v3_alive_at > 0)
                self.assertNotEqual(self.state()["id"], original_identity)
                self.assertEqual(self.state()["levels"], {"a": None, "b": None})
                self.assertFalse(self.state()["ready"])
                await self.v3_state(ws, ids, "0+0+20+20")
                await self.until(lambda: self.state()["levels"]["a"] == 0)
                await asyncio.sleep(0.1)
                self.assertFalse(self.state()["ready"])
                self.assertFalse(await self.device.apply(PULSE))
                await self.v3_state(ws, ids, "5+0+20+20")
                await self.until(lambda: self.state()["ready"])

    async def test_v4_device_rpc_pulse_and_pause_on_fault(self):
        url = await self.qr("v4")
        async with connect(url) as ws:
            _, req_id = await self.v4_handshake(ws, url)
            self.assertFalse(await self.device.apply(PULSE))
            await app_v4(ws, {"t": "resp", "reqId": "wrong", "result": {"devices": [slot()]}})
            await asyncio.sleep(0.04)
            self.assertFalse(self.state()["ready"])
            await app_v4(ws, {"t": "resp", "reqId": req_id, "result": {"devices": [slot()]}})
            await self.until(lambda: self.state()["ready"])
            self.assertEqual(self.state()["levels"], {"a": 8, "b": 0})
            self.assertEqual(self.state()["limits"], {"a": 25, "b": 25})
            self.assertTrue(await self.device.apply(PULSE))
            pkt = (await receive(ws))["data"]
            self.assertEqual(pkt["m"], "device.op")
            self.assertEqual(pkt["data"], {"s": "coyote", "t": 0, "c": 0, "p": 1,
                                           "d": 200, "v": PULSE["frames"], "ver": 3})
            self.assertTrue(await self.device.apply(PULSE))
            self.assertEqual((await receive(ws))["data"]["m"], "device.op")
            self.assertFalse(await self.device.apply(PULSE))  # finite backlog
            await app_v4(ws, {"t": "ev", "ev": "slots.patch", "slots": [
                {"slotId": "coyote", "props": {"channelAStatus": 3}}]})
            self.assertEqual((await receive(ws))["data"]["m"], "device.op.clear")
            await self.until(lambda: not self.state()["ready"])
            self.assertFalse(await self.device.apply(PULSE))

    async def test_v4_never_switches_silently_and_respects_mute(self):
        url = await self.qr("v4")
        async with connect(url) as ws:
            await self.v4_handshake(ws, url)
            await app_v4(ws, snap(slot("left"), slot("right")))
            await self.until(lambda: len(self.state()["slots"]) == 2)
            self.assertFalse(self.state()["ready"])
            await self.device.select("left")
            self.assertTrue(self.state()["ready"])
            self.assertEqual(self.state()["slot"], "left")
            await app_v4(ws, {"t": "ev", "ev": "slots.patch", "slots": [
                {"slotId": "left", "slotState": {"channelA": {"isMuted": True}}}]})
            await self.until(lambda: not self.state()["ready"])
            await app_v4(ws, {"t": "ev", "ev": "devices.patch", "removed": ["left"]})
            await self.until(lambda: self.state()["slot"] is None)
            self.assertFalse(self.state()["ready"])
            await self.device.select("right")
            self.assertTrue(self.state()["ready"])
            self.assertEqual(self.state()["slot"], "right")
            await self.device.unpair()
            self.assertFalse(self.state()["connected"])
            self.assertEqual((await receive(ws))["type"], "controller_disconnected")

    async def test_v4_distinguishes_local_clear_and_phone_cancellation(self):
        url = await self.qr("v4")
        async with connect(url) as ws:
            await self.v4_handshake(ws, url)
            await app_v4(ws, snap(slot()))
            await self.until(lambda: self.state()["ready"])
            self.assertTrue(await self.device.apply(PULSE))
            first = (await receive(ws))["data"]
            await self.device.stop()
            cleared = (await receive(ws))["data"]
            await app_v4(ws, {"t": "resp", "reqId": cleared["reqId"], "result": {}})
            await app_v4(ws, {"t": "resp", "reqId": first["reqId"],
                              "result": {"type": 0, "reason": "cleared", "channel": 0}})
            self.assertTrue(await self.device.apply(PULSE))
            second = (await receive(ws))["data"]
            await app_v4(ws, {"t": "resp", "reqId": second["reqId"],
                              "result": {"type": 0, "reason": "cancelled", "channel": 0}})
            self.assertEqual((await receive(ws))["data"]["m"], "device.op.clear")
            with self.assertRaises(ConnectionClosed):
                await receive(ws)
            await self.until(lambda: not self.state()["connected"])
            self.assertFalse(await self.device.apply(PULSE))

    async def test_v4_requires_all_limits_and_rejects_wrong_peer(self):
        url = await self.qr("v4")
        async with connect(url) as ws:
            await self.v4_handshake(ws, url)
            invalid = slot()
            del invalid["slotState"]["channelA"]["comfortLimit"]["absoluteMax"]
            await app_v4(ws, snap(invalid))
            await self.until(lambda: self.state()["slot"] is not None)
            self.assertFalse(self.state()["ready"])
            self.assertFalse(await self.device.apply(PULSE))
            await app_v4(ws, snap(slot()), clientId="not-the-phone")
            with self.assertRaises(ConnectionClosed):
                await receive(ws)
            await self.until(lambda: not self.state()["connected"])


if __name__ == "__main__":
    unittest.main(verbosity=2)



