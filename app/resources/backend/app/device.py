"""Direct, single-phone DG-LAB Socket adapter. No Bluetooth or strength writes.

Protocol references (DG-LAB):
  https://github.com/dungeonlab-open/dglab-websocket-simple/blob/main/socket/v2/README.md
  https://github.com/dungeonlab-open/dglab-websocket-server/blob/main/README.md
  https://github.com/dungeonlab-open/dglab-kit/blob/main/README.md
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import errno
import hmac
import io
import ipaddress
import json
import math
import re
import secrets
import socket
import struct
import sys
import time
import uuid
from contextlib import suppress
from http import HTTPStatus
from typing import NamedTuple
from urllib.parse import quote

import qrcode
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

CHANNELS = ("a", "b")
PRIVATE = tuple(map(ipaddress.IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))
STRENGTH = re.compile(r"strength-([0-9]{1,3})\+([0-9]{1,3})\+([0-9]{1,3})\+([0-9]{1,3})")
FRAME = re.compile(r"[0-9a-fA-F]{16}")
V4_TTL = 12.0
V3_PING_INTERVAL = 4.0
V3_PING_TIMEOUT = 3.0
WATCHDOG_GAP = 4.0
PAIR_SECONDS = 120
V3_PORT = 5678
MAX_BACKLOG = 0.4
MAX_FRAMES = 2


def _private(address, loopback=False):
    try:
        ip = ipaddress.IPv4Address(address)
    except (TypeError, ValueError):
        return False
    return (loopback and ip.is_loopback) or any(ip in net for net in PRIVATE)


class _LanAddress(NamedTuple):
    ip: str
    lan_link: bool
    default_gateway: bool
    metric: int
    index: int


# Some Virtual Ethernet or VPN drivers advertise the same ifType as physical NICs.
_SOFTWARE_NIC = re.compile(
    r"virtual|vEthernet|Hyper[- ]?V|VMware|VBox|Hypervisor|Loopback|WireGuard|Wintun|TAP[- ]Windows|"
    r"tunnel|Tailscale|ZeroTier|Cloudflare WARP|NordLynx|Hamachi|\bVPN\b|WAN Miniport|Bluetooth",
    re.IGNORECASE,
)


def _rank_lan(rows):
    def priority(row):
        return (not row.lan_link, not row.default_gateway, row.metric, row.index,
                int(ipaddress.IPv4Address(row.ip)))

    return list(dict.fromkeys(row.ip for row in sorted(
        (row for row in rows if _private(row.ip)), key=priority)))[:64]


def _windows_lan():
    """Read local IP Helper adapter metadata only; no DNS, shell, packets or route changes."""
    u32, u64, pointer = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p

    class SocketAddress(ctypes.Structure):
        _fields_ = [("address", pointer), ("length", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        # Initial union has Length (low word) and Flags (high word).
        _fields_ = [("header", u64), ("next", pointer), ("socket", SocketAddress),
                    ("prefix_origin", u32), ("suffix_origin", u32), ("dad", u32),
                    ("valid_lifetime", u32), ("preferred_lifetime", u32),
                    ("lease_lifetime", u32), ("prefix_length", ctypes.c_ubyte)]

    class Adapter(ctypes.Structure):
        # Prefix of IP_ADAPTER_ADDRESSES_LH through Ipv4Metric; both unions
        # use their documented storage type to preserve native alignment.
        _fields_ = [("header", u64), ("next", pointer), ("name", pointer),
                    ("unicast", pointer), ("anycast", pointer), ("multicast", pointer),
                    ("dns_server", pointer), ("dns_suffix", pointer),
                    ("description", ctypes.c_wchar_p), ("friendly", ctypes.c_wchar_p),
                    ("mac", ctypes.c_ubyte * 8), ("mac_len", u32), ("flags", u32),
                    ("mtu", u32), ("if_type", u32), ("status", u32), ("ipv6_index", u32),
                    ("zones", u32 * 16), ("prefix", pointer),
                    ("tx_speed", u64), ("rx_speed", u64), ("wins", pointer),
                    ("gateway", pointer), ("metric", u32)]

    get_adapters = ctypes.WinDLL("iphlpapi.dll", winmode=0x800).GetAdaptersAddresses
    get_adapters.argtypes = (u32, u32, pointer, pointer, ctypes.POINTER(u32))
    get_adapters.restype = u32
    length = u32(15_000)
    skip_extras_and_include_gateway = 0x0002 | 0x0004 | 0x0008 | 0x0080
    for _ in range(3):
        if not 0 < length.value <= 2_000_000:
            return []
        buf = ctypes.create_string_buffer(length.value)
        result = get_adapters(socket.AF_INET, skip_extras_and_include_gateway, None,
                              buf, ctypes.byref(length))
        if result == 0:
            break
        if result != 111:  # ERROR_BUFFER_OVERFLOW; retry the newly reported size only.
            return []
    else:
        return []

    first = ctypes.addressof(buf)
    last = first + ctypes.sizeof(buf)
    mask = 0xffffffff

    def inside(address, size):
        return address is not None and first <= address <= last - size

    output = []
    address = first
    for _ in range(256):
        if not inside(address, ctypes.sizeof(Adapter)):
            break
        adapter = Adapter.from_address(address)
        address = adapter.next
        if (adapter.header & mask) < Adapter.metric.offset + 4:
            continue
        if adapter.status != 1 or adapter.if_type == 24 or (adapter.flags & 0x8):
            continue  # IfOperStatusUp only, not software loopback or receive-only.
        index = adapter.header >> 32
        lan_link = adapter.if_type in (6, 71) and not _SOFTWARE_NIC.search(
            (adapter.description or "") + " " + (adapter.friendly or ""))
        unicast = adapter.unicast
        for _ in range(64):
            if not inside(unicast, ctypes.sizeof(Unicast)):
                break
            entry = Unicast.from_address(unicast)
            unicast = entry.next
            ipaddr = entry.socket.address
            if (entry.header & mask) < Unicast.valid_lifetime.offset + 4 or (entry.header >> 32) & 2:
                continue  # Ignore short records and transient / cluster addresses.
            if entry.dad != 4 or not entry.valid_lifetime or entry.socket.length < 16 or not inside(ipaddr, 16):
                continue  # Preferred, assigned addresses only; never tentative or duplicated.
            raw = ctypes.string_at(ipaddr, 16)
            if int.from_bytes(raw[:2], "little") != socket.AF_INET:
                continue
            ip = socket.inet_ntoa(raw[4:8])
            if _private(ip):
                output.append(_LanAddress(ip, bool(lan_link), bool(adapter.gateway),
                                          adapter.metric, index))
    return output


def lan_ipv4():
    """Active RFC 1918 addresses in LAN preference order (Windows desktop build)."""
    if sys.platform != "win32":
        # Preserve the standalone development backend's hostname-based fallback.
        try:
            results = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
            return sorted({item[4][0] for item in results if _private(item[4][0])})
        except OSError:
            return []
    try:
        return _rank_lan(_windows_lan())
    except (AttributeError, OSError):
        return []  # Fail closed if Windows cannot enumerate assigned interfaces.


def _preferred_free(ip, port):
    if sys.platform != "win32":
        return True
    # Windows can allow a specific bind beside an existing wildcard listener.
    # Read the local table so we don't take traffic away from a running DGHub.
    try:
        table = ctypes.WinDLL("iphlpapi.dll", winmode=0x800).GetTcpTable
        size = ctypes.c_uint32(16384)
        for _ in range(3):
            if not 4 <= size.value <= 2_000_000:
                break
            buf = ctypes.create_string_buffer(size.value)
            ret = table(buf, ctypes.byref(size), 0)
            if ret == 122:  # ERROR_INSUFFICIENT_BUFFER
                continue
            if ret:
                break
            rows = struct.unpack_from("<I", buf)[0]
            if rows > (len(buf) - 4) // 20:
                break
            addr = int.from_bytes(socket.inet_aton(ip), "little")
            for i in range(rows):
                state, local, raw = struct.unpack_from("<III", buf, 4 + 20*i)
                if state == 2 and local in (0, addr) and (raw & 0xffff) == socket.htons(port):
                    return False
            return True
    except (OSError, AttributeError):
        pass
    return False  # If Windows cannot confirm the port is free, use an OS-assigned port.


def _bind_pair_socket(ip, preferred=0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((ip, preferred if preferred and _preferred_free(ip, preferred) else 0))
        except OSError as exc:
            if not preferred or exc.errno not in (errno.EADDRINUSE, errno.EACCES, 10013, 10048):
                raise
            sock.bind((ip, 0))  # DGHub or another service already owns the preferred port.
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def _json(raw, *, legacy=False):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def reject_constant(_):
        raise ValueError("non-standard number")

    if legacy and isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    if not isinstance(raw, str):
        raise ValueError("not a JSON text frame")
    result = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    if not isinstance(result, dict):
        raise ValueError("not a JSON object")
    return result


def _qr(url):
    buf = io.BytesIO()
    qrcode.make(url, box_size=5, border=4).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _num(value):
    return (type(value) in (int, float) and 0 <= value <= 200 and
            (type(value) is int or math.isfinite(value)))


def _merge(left, right):
    out = dict(left)
    for key, value in right.items():
        out[key] = _merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def _slot_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 128 and
            not any(ord(c) < 32 or ord(c) == 127 for c in value))


class Device:
    """Methods run on one asyncio loop. Callback has no arguments."""

    def __init__(self, callback=lambda: None, *, _test_loopback=False):
        self.callback = callback
        self._loopback = _test_loopback
        self._ips = lan_ipv4()
        self._lifecycle = asyncio.Lock()
        self._wire = asyncio.Lock()
        self._listener = self._watch = self._peer = None
        self._model = self._ip = self._url = self._qr = self._secret = ""
        self._port, self._diagnostic = 0, ""
        self._pair_deadline = 0.0
        self._session = self._phone = ""
        self._epoch = 0
        self._connected = False
        self._state_at = 0.0
        self._v3_alive_at = 0.0
        self._watchdog_clock = (0.0, 0.0)
        self._detail = "未配对"
        self._slots = {}
        self._selection = None
        self._active_slot = None
        self._levels = dict.fromkeys(CHANNELS)
        self._limits = dict.fromkeys(CHANNELS)
        self._available = dict.fromkeys(CHANNELS, False)
        self._pending_get = {}
        self._pending_output = {}
        self._telemetry_epoch = 0
        self._queued = {}
        self._last_view = None

    def view(self):
        active = bool(self._qr)
        target = self._active_slot or ("legacy" if self._model == "v3" else "no-slot")
        return {
            "pair": {"active": active, "qr": self._qr, "url": self._url,
                     "ip": self._ip, "ips": list(self._ips), "port": self._port if active else 0,
                     "diagnostic": self._diagnostic},
            "device": {
                "connected": self._connected,
                "ready": self._fresh() and any(self._available.values()),
                "id": (f"{self._session}:{target}" if self._connected else ""),
                "model": self._model, "detail": self._detail,
                "limits": dict(self._limits), "levels": dict(self._levels),
                "channels": dict(self._available) if self._fresh() else dict.fromkeys(CHANNELS, False),
                "slot": self._active_slot,
                "slots": [
                    {"id": sid, "name": data["name"],
                     "connected": data["slotState"].get("hasDevice") is True}
                    for sid, data in self._slots.items() if data["type"] == "COYOTE_030"
                ],
            },
        }

    def _emit(self):
        view = self.view()
        if view != self._last_view:
            self._last_view = view
            self.callback()

    def _fresh(self):
        if not self._connected or self._state_at <= 0:
            return False
        now = time.monotonic()
        if self._model != "v3":
            return now - self._state_at <= V4_TTL
        # Legacy App only reports changes. Keep a valid report while this exact
        # authenticated phone remains verifiably live; pongs never create or alter a report.
        mono, wall = self._watchdog_clock
        return (self._v3_alive_at > 0 and
                now - self._v3_alive_at <= V3_PING_INTERVAL + V3_PING_TIMEOUT + .5 and
                now - mono <= WATCHDOG_GAP and time.time() - wall <= WATCHDOG_GAP)

    async def pair(self, ip, model):
        model = {"3": "v3", "3.x": "v3", "4": "v4", "4.x": "v4"}.get(str(model).lower(), str(model).lower())
        if model not in ("v3", "v4"):
            raise ValueError("请选择官方 App 3.x 或 4.x")
        if ip is not None and not isinstance(ip, str):
            raise ValueError("请选择有效的本机私有 IPv4 地址")
        ip = (ip or "").strip()
        auto = not ip or ip.lower() == "auto"
        if not auto and not _private(ip, self._loopback):
            raise ValueError("请选择有效的本机私有 IPv4 地址，不支持公网或回环地址")
        async with self._lifecycle:
            try:
                async with asyncio.timeout(3):
                    current = await asyncio.to_thread(lan_ipv4)
            except TimeoutError:
                raise ValueError("读取本机网卡超时，请检查网络连接后重试") from None
            self._ips = list(dict.fromkeys(addr for addr in current
                                           if isinstance(addr, str) and _private(addr, self._loopback)))[:64]
            self._emit()
            if auto:
                if not self._ips:
                    raise ValueError("没找到可用的局域网 IPv4。请先连接 Wi-Fi 或网线，并让手机和电脑处于同一局域网")
                candidates = self._ips
            else:
                testing_local = self._loopback and ipaddress.IPv4Address(ip).is_loopback
                if ip not in self._ips and not testing_local:
                    raise ValueError("所选 IPv4 已不在本机有效网卡上，请选择「自动」或重新选地址")
                candidates = (ip,)

            await self._unpair("未配对")
            self._model = model
            for candidate in candidates:
                try:
                    listen_socket = _bind_pair_socket(candidate, V3_PORT if model == "v3" else 0)
                except OSError:
                    continue  # Explicit mode contains one candidate; only auto may try another.
                self._ip = ip = candidate
                break
            else:
                message = ("检测到了局域网 IPv4，但无法监听。请检查网络权限，或手动选择可用地址"
                           if auto else "所选 IPv4 无法监听。请检查网卡状态，或选择「自动」")
                self._detail = "配对监听未启动"
                self._emit()
                raise ValueError(message)
            try:
                self._secret = str(uuid.uuid4()) if model == "v3" else secrets.token_hex(16)
                port = self._port = listen_socket.getsockname()[1]
                local_origins = [None, f"http://{ip}:{port}", f"ws://{ip}:{port}"]
                self._listener = await serve(
                    self._accept, sock=listen_socket, origins=local_origins, max_size=32768,
                    process_request=self._pair_request,
                    max_queue=8, compression=None, open_timeout=5,
                    close_timeout=1, ping_interval=5 if model == "v4" else None, ping_timeout=5,
                )
                if model == "v3":
                    url = f"https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#ws://{ip}:{port}/{self._secret}"
                else:
                    wsurl = f"ws://{ip}:{port}/?tid={self._secret}"
                    url = "https://dungeon-lab.cn/s/?v=1&action=socket&url=" + quote(wsurl, safe="")
                self._qr, self._url = _qr(url), url
                self._pair_deadline = time.monotonic() + PAIR_SECONDS
                self._detail = "等待手机扫码（两分钟内有效）"
                self._diagnostic = "监听已就绪；尚未收到 App 的连接请求。"
                self._watchdog_clock = (time.monotonic(), time.time())
                self._watch = asyncio.create_task(self._watchdog())
            except BaseException:
                if self._listener is None:
                    listen_socket.close()
                await self._unpair("配对监听未启动")
                raise
            self._emit()
            return self.view()

    def _note(self, text):
        if self._diagnostic != text:
            self._diagnostic = text
            self._emit()

    def _pair_request(self, ws, request):
        remote = ws.remote_address
        if not remote or not _private(remote[0], self._loopback):
            return ws.respond(HTTPStatus.FORBIDDEN, "LAN only\n")
        pending = bool(self._qr) and time.monotonic() <= self._pair_deadline
        if request.path == "/check":
            if not pending or "Upgrade" in request.headers:
                return ws.respond(HTTPStatus.NOT_FOUND, "No active network check\n")
            if self._diagnostic == "监听已就绪；尚未收到 App 的连接请求。":
                self._note("连通性测试已到达此端口；尚未收到 App 的连接请求。")
            result = ws.respond(HTTPStatus.OK, "已连到电脑的配对端口。请返回 DG-LAB App 扫码；此页面不会配对或启用反馈。\n")
            result.headers["Cache-Control"] = "no-store"
            return result
        origins = request.headers.get_all("Origin")
        if origins and origins not in ([f"http://{self._ip}:{self._port}"], [f"ws://{self._ip}:{self._port}"]):
            if pending:
                self._note("连接已到达，但 WebSocket 的来源校验未通过。请使用 App 内的 Socket 扫码。")
            return ws.respond(HTTPStatus.FORBIDDEN, "Untrusted origin\n")
        if pending and "Upgrade" in request.headers:
            self._note("已收到 WebSocket 连接请求，正在校验配对。")
        return None

    async def unpair(self):
        async with self._lifecycle:
            await self._unpair("未配对")

    async def shutdown(self):
        await self.unpair()

    async def _unpair(self, detail, *, keep_note=False):
        # Invalidate any apply() waiting for the wire before clearing.
        self._state_at = 0
        await self.stop()
        self._secret = ""
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
            with suppress(asyncio.CancelledError):
                await watch
        if self._peer:
            with suppress(ConnectionClosed, TimeoutError):
                async with asyncio.timeout(2):
                    if self._connected:
                        if self._model == "v3":
                            await self._send(self._peer, self._legacy("break", "209"))
                        else:
                            await self._send(self._peer, {"type": "controller_disconnected", "clientId": self._secret_at_connect})
                    await self._peer.close(1000, "unpaired")
        server, self._listener = self._listener, None
        if server:
            server.close()
            await server.wait_closed()
        self._peer = None
        self._reset_device(detail)
        self._url = self._qr = self._ip = ""
        self._port = 0
        if not keep_note:
            self._diagnostic = ""
        self._emit()

    def _reset_device(self, detail):
        self._epoch += 1
        self._connected = False
        self._state_at = self._v3_alive_at = 0
        self._session = self._phone = ""
        self._slots.clear()
        self._selection = self._active_slot = None
        self._limits = dict.fromkeys(CHANNELS)
        self._levels = dict.fromkeys(CHANNELS)
        self._available = dict.fromkeys(CHANNELS, False)
        self._pending_get.clear()
        self._pending_output.clear()
        self._telemetry_epoch = 0
        self._queued.clear()
        self._detail = detail

    async def _send(self, ws, data):
        async with asyncio.timeout(0.5):
            await ws.send(json.dumps(data, separators=(",", ":"), ensure_ascii=True))

    def _legacy(self, kind, message):
        return {"type": kind, "message": message, "clientId": self._secret_at_connect, "targetId": self._phone}

    def _rpc(self, method, data=None):
        key = secrets.token_hex(12)
        rpc = {"t": "req", "reqId": key, "m": method}
        if data is not None:
            rpc["data"] = data
        return key, {"type": "message", "data": rpc}

    async def _get_devices(self, ws):
        key, pkt = self._rpc("devices.get")
        self._pending_get = {key: self._telemetry_epoch}
        await self._send(ws, pkt)

    async def _accept(self, ws):
        path = ws.request.path
        model, secret = self._model, self._secret
        remote = ws.remote_address
        if (not secret or not self._qr or self._peer is not None or not remote or
                not _private(remote[0], self._loopback) or time.monotonic() > self._pair_deadline):
            await ws.close(1008, "pairing unavailable")
            return
        expected = (f"/{secret}", f"/?tid={secret}", f"/?targetId={secret}") if model == "v3" else (f"/?tid={secret}",)
        path_verified = any(hmac.compare_digest(path.encode("utf-8"), route.encode("ascii")) for route in expected)
        # Some legacy apps connect to / and deliver the secret in a separate bind.
        if not path_verified and not (model == "v3" and path == "/"):
            self._note("连接已到达，但配对链接不匹配。请重新生成并扫新码。")
            await ws.close(1008, "invalid pairing")
            return
        self._peer = ws
        self._phone = str(uuid.uuid4()) if model == "v3" else secrets.token_hex(16)
        self._secret_at_connect = secret
        v3_probe = None
        first_report = None
        try:
            if model == "v3":
                await self._send(ws, {"type": "bind", "clientId": self._phone, "targetId": "",
                                      "message": "targetId"})
                if path_verified:
                    # Older clients send their own bind after the server assigns a phone ID.
                    # Newer path-authenticated clients wait for the server's 200 instead.
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.35)
                    except TimeoutError:
                        pass
                    else:
                        msg = _json(raw, legacy=True)
                        if msg.get("type") != "bind":
                            first_report = msg
                        elif not self._legacy_ids(msg):
                            self._note("App 已到达，但 3.x 绑定消息与本次配对不匹配。")
                            await ws.close(1008, "invalid binding")
                            return
                else:
                    self._note("已连到 App，正在等待 3.x 的绑定消息。")
                    # Legacy clients may ask the user to confirm before replying.
                    bound = _json(await asyncio.wait_for(ws.recv(), timeout=max(0, min(30, self._pair_deadline-time.monotonic()))), legacy=True)
                    if bound.get("type") != "bind" or not self._legacy_ids(bound) or not isinstance(bound.get("message"), str) or not bound["message"]:
                        self._note("App 已到达，但 3.x 绑定消息与本次配对不匹配。")
                        await ws.close(1008, "invalid binding")
                        return
                await self._send(ws, self._legacy("bind", "200"))
            else:
                await self._send(ws, {"type": "hello", "clientId": self._phone})
                await self._send(ws, {"type": "controller_attached", "clientId": secret})
            self._secret = self._qr = self._url = ""
            self._connected = True
            self._diagnostic = "App 已完成绑定，网络和扫码连接正常。"
            self._session = secrets.token_hex(12)  # State identity, never the pairing secret.
            self._detail = "手机已配对；等待 App 上报设备和通道状态"
            self._emit()
            if model == "v4":
                await self._get_devices(ws)
            else:
                if first_report is not None:
                    await self._handle_v3(ws, first_report)
                v3_probe = asyncio.create_task(self._probe_v3(ws))
            async for raw in ws:
                data = _json(raw, legacy=model == "v3")
                if model == "v3":
                    await self._handle_v3(ws, data)
                else:
                    await self._handle_v4(ws, data)
        except (ValueError, RecursionError, UnicodeError):
            self._note("已收到 App 消息，但格式与所选协议不符。")
            self._state_at = 0
            await self.stop()
            with suppress(ConnectionClosed, TimeoutError):
                await ws.close(1008, "invalid protocol message")
        except TimeoutError:
            if not self._connected:
                self._note("App 已连接，但未在时限内完成绑定；请重新扫码并确认。")
        except (ConnectionClosed, OSError):
            if not self._connected:
                self._note("App 在绑定完成前断开了连接。")
        finally:
            if v3_probe is not None:
                v3_probe.cancel()
                await asyncio.gather(v3_probe, return_exceptions=True)
            if self._peer is ws:
                paired = self._connected
                self._peer = None
                self._reset_device("手机连接已断开；请重新配对" if paired else "等待手机扫码（两分钟内有效）")
                self._emit()

    async def _probe_v3(self, ws):
        while self._peer is ws and self._connected:
            try:
                async with asyncio.timeout(V3_PING_TIMEOUT):
                    pong = await ws.ping()
                    await pong
            except (ConnectionClosed, TimeoutError, OSError):
                if self._peer is ws:
                    self._v3_alive_at = self._state_at = 0
                    self._detail = "手机未响应 WebSocket 探活；已暂停，须重新配对"
                    await self.stop()
                    with suppress(ConnectionClosed, TimeoutError):
                        async with asyncio.timeout(2):
                            await ws.close(1011, "phone keepalive timeout")
                return
            if self._peer is not ws or not self._connected:
                return
            was_fresh = self._fresh()
            self._v3_alive_at = time.monotonic()
            if not was_fresh and self._fresh():
                await self._state(self._levels, self._limits, self._available, None)
            await asyncio.sleep(V3_PING_INTERVAL)

    def _legacy_ids(self, data):
        return data.get("clientId") == self._secret_at_connect and data.get("targetId") == self._phone

    async def _handle_v3(self, ws, data):
        message = data.get("message")
        if data.get("type") == "heartbeat":
            # Heartbeats never grant permission or update the channel state/liveness.
            return
        if not self._legacy_ids(data) or not isinstance(data.get("type"), str) or not isinstance(message, str) or not 0 < len(message) <= 1950:
            raise ValueError("wrong legacy peer or envelope")
        if data["type"] == "bind":
            await self._send(ws, self._legacy("bind", "200"))
        elif data["type"] == "break":
            self._state_at = 0
            await self.stop()
            await ws.close(1000, "phone disconnected")
        elif message.startswith("strength-"):
            match = STRENGTH.fullmatch(message)
            if match is None:
                raise ValueError("invalid strength report")
            a, b, ma, mb = map(int, match.groups())
            if not (0 <= a <= 200 and 0 <= b <= 200 and 0 <= ma <= 200 and 0 <= mb <= 200):
                raise ValueError("invalid strength values")
            self._state_at = time.monotonic()
            await self._state({"a": a, "b": b}, {"a": ma, "b": mb},
                              {"a": 0 < a <= ma, "b": 0 < b <= mb}, None)
        # Heartbeat, app feedback buttons, and other valid non-state frames don't arm output.

    @staticmethod
    def _v4_devices(devices):
        if not isinstance(devices, list) or len(devices) > 32:
            raise ValueError("invalid V4 device list")
        out = {}
        for item in devices:
            if not isinstance(item, dict):
                raise ValueError("not a device descriptor")
            sid, name, typ = item.get("slotId"), item.get("name"), item.get("type")
            props, states = item.get("props", {}), item.get("slotState", {})
            if (not _slot_id(sid) or sid in out or not isinstance(name, str) or len(name) > 256 or
                    not isinstance(typ, str) or not isinstance(props, dict) or not isinstance(states, dict)):
                raise ValueError("invalid device descriptor")
            out[sid] = {"name": name, "type": typ, "props": props, "slotState": states}
        return out

    async def _handle_v4(self, ws, data):
        if data.get("type") == "ping":
            await self._send(ws, {"type": "pong", "ts": int(time.time() * 1000)})
            return
        if data.get("type") in ("pong", "heartbeat"):
            return
        if data.get("type") != "message" or ("clientId" in data and data["clientId"] != self._phone):
            raise ValueError("invalid V4 peer or envelope")
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise ValueError("invalid V4 payload")
        kind = payload.get("t")
        if kind == "resp":
            key = payload.get("reqId")
            if not isinstance(key, str):
                raise ValueError("invalid V4 response ID")
            submitted = self._pending_output.pop(key, None)
            if submitted:
                method, _, expected_channel, locally_cleared = submitted
                result = payload.get("result")
                allowed_reasons = ("completed", "cleared") if locally_cleared else ("completed",)
                if "error" in payload or not isinstance(result, dict):
                    await self._output_failed(ws)
                elif method == "pulse":
                    channel = result.get("channel", CHANNELS.index(expected_channel))
                    if (type(result.get("type")) is not int or result["type"] != 0 or
                            result.get("reason") not in allowed_reasons or
                            type(channel) is not int or channel != CHANNELS.index(expected_channel)):
                        await self._output_failed(ws)
                return
            version = self._pending_get.pop(key, None)
            if version is None or version != self._telemetry_epoch:
                return  # Never let an older requested snapshot overwrite a newer pushed state.
            result = payload.get("result")
            if not isinstance(result, dict) or "error" in payload:
                self._state_at = 0
                await self._state(dict.fromkeys(CHANNELS), dict.fromkeys(CHANNELS),
                                  dict.fromkeys(CHANNELS, False), self._active_slot)
                return
            self._slots = self._v4_devices(result.get("devices"))
            self._state_at = time.monotonic()
        elif kind == "ev":
            event = payload.get("ev")
            if event in ("devices.snapshot", "devices.patch", "slots.patch"):
                self._telemetry_epoch += 1
            if event == "devices.snapshot":
                self._slots = self._v4_devices(payload.get("devices"))
                self._state_at = time.monotonic()
            elif event == "devices.patch":
                added, removed = self._v4_devices(payload.get("added", [])), payload.get("removed", [])
                if (not isinstance(removed, list) or len(removed) > 32 or
                        any(not _slot_id(sid) for sid in removed)):
                    raise ValueError("invalid V4 removal list")
                for sid in removed:
                    self._slots.pop(sid, None)
                self._slots.update(added)
                if len(self._slots) > 32:
                    raise ValueError("too many V4 slots")
            elif event == "slots.patch":
                items = payload.get("slots")
                if not isinstance(items, list) or len(items) > 32:
                    raise ValueError("invalid V4 state update")
                for item in items:
                    if not isinstance(item, dict) or not _slot_id(item.get("slotId")):
                        raise ValueError("invalid V4 slot patch")
                    slot = self._slots.get(item["slotId"])
                    for key in ("props", "slotState"):
                        patch = item.get(key, {})
                        if not isinstance(patch, dict):
                            raise ValueError("invalid V4 slot patch fields")
                        if slot:
                            slot[key] = _merge(slot[key], patch)
            else:
                return  # Custom buttons don't arm the controller.
            if self._state_at:
                self._state_at = time.monotonic()
        else:
            raise ValueError("invalid V4 app message")
        await self._read_v4()

    async def select(self, slot_id):
        if (self._model != "v4" or not _slot_id(slot_id) or
                slot_id not in self._slots or self._slots[slot_id]["type"] != "COYOTE_030"):
            raise ValueError("该设备不可用于 V4 反馈")
        self._selection = slot_id
        await self._read_v4()

    async def _read_v4(self):
        levels, limits, available = dict.fromkeys(CHANNELS), dict.fromkeys(CHANNELS), dict.fromkeys(CHANNELS, False)
        candidates = [sid for sid, data in self._slots.items()
                      if data["type"] == "COYOTE_030" and data["slotState"].get("hasDevice") is True
                      and data["props"].get("connectState") == "connected"]
        if self._selection is not None:
            sid = self._selection if self._selection in candidates else None
        elif self._active_slot in candidates:
            sid = self._active_slot
        elif len(candidates) == 1 and self._active_slot is None:
            sid = candidates[0]
            self._selection = sid  # Never silently switch to another unit after losing this one.
        else:
            sid = None
        if sid:
            props, state = self._slots[sid]["props"], self._slots[sid]["slotState"]
            for channel in CHANNELS:
                c = channel.upper()
                level = props.get("intensity" + c)
                status = props.get(f"channel{c}Status")
                info = state.get("channel" + c)
                levels[channel] = level if _num(level) else None
                if not isinstance(info, dict):
                    continue
                comfort = info.get("comfortLimit")
                if not isinstance(comfort, dict):
                    continue
                caps = [info.get("intensityMax"), comfort.get("comfortMax"), comfort.get("absoluteMax")]
                if not all(map(_num, caps)):
                    continue
                limit = min(caps)
                limits[channel] = limit
                available[channel] = (
                    _num(level) and 0 < level <= limit and type(status) is int and status in (0, 2)
                    and info.get("isMuted") is False and comfort.get("overheat") is False
                )
        await self._state(levels, limits, available, sid)

    async def _state(self, levels, limits, available, sid):
        old_slot, old = self._active_slot, self._available
        constrained = {c for c in CHANNELS if
                       old[c] and (not available[c] or
                       (limits[c] is not None and self._limits[c] is not None and limits[c] < self._limits[c]))}
        if old_slot != sid:
            self._epoch += 1
            constrained = set(CHANNELS)
        self._levels, self._limits, self._available, self._active_slot = levels, limits, available, sid
        if self._fresh() and any(available.values()):
            self._detail = "App 上报有效；强度沿用手机设置"
        elif not self._fresh():
            self._detail = "未收到有效或足够新的 App 状态；输出已暂停"
        elif self._model == "v4" and len(self._slots) > 1 and sid is None:
            self._detail = "请选择已连接的目标设备；不会自动改用另一台"
        else:
            self._detail = "等待设备就绪、解除静音或在 App 中手动设置有效通道"
        if constrained:
            await self._clear(constrained)
        self._emit()

    async def apply(self, command):
        if not isinstance(command, dict):
            return False
        channel, kind = command.get("channel"), command.get("kind")
        if channel not in CHANNELS:
            return False
        if kind == "clear":
            await self._clear({channel})
            return True
        frames = command.get("frames")
        if (kind != "pulse" or not isinstance(frames, (list, tuple)) or not 1 <= len(frames) <= MAX_FRAMES or
                any(not isinstance(frame, str) or not FRAME.fullmatch(frame) for frame in frames)):
            return False
        for frame in frames:
            raw = bytes.fromhex(frame)
            if any(not 10 <= x <= 240 for x in raw[:4]) or any(x > 100 for x in raw[4:]):
                return False
        duration = command.get("duration_ms", 100 * len(frames))
        if type(duration) is not int or not 100 <= duration <= 100 * len(frames):
            return False
        count = duration // 100
        frames = [f.upper() for f in frames[:count]]
        epoch = self._epoch
        async with self._wire:
            if (epoch != self._epoch or not self._fresh() or not self._available[channel] or
                    self._peer is None):
                return False
            ws, slot, now = self._peer, self._active_slot, time.monotonic()
            prior = self._queued.get(channel)
            end = max(now, prior[1] if prior else now) + count * 0.1
            if end - now > MAX_BACKLOG:
                return False
            if self._model == "v3":
                pkt = self._legacy("msg", f"pulse-{channel.upper()}:" + json.dumps(frames, separators=(",", ":")))
            else:
                key, pkt = self._rpc("device.op", {"s": slot, "t": 0, "c": CHANNELS.index(channel),
                                                "p": 1, "d": count * 100, "v": frames, "ver": 3})
                self._pending_output[key] = ("pulse", now + 3, channel, False)
            # Track before sending: a timed-out send may already have delivered bytes.
            self._queued[channel] = (slot, end)
            try:
                await self._send(ws, pkt)
                return True
            except (ConnectionClosed, TimeoutError, OSError):
                self._state_at = 0
                self._detail = "输出连接中断；已暂停"
        await self.stop()
        return False

    async def _clear(self, channels):
        self._epoch += 1
        async with self._wire:
            ws = self._peer
            for channel in sorted(channels):
                item = self._queued.pop(channel, None)
                if item is None or ws is None or not self._connected:
                    continue  # Don't touch a stream on which this adapter sent nothing.
                if self._model == "v3":
                    pkt = self._legacy("msg", f"clear-{CHANNELS.index(channel) + 1}")
                else:
                    key, pkt = self._rpc("device.op.clear", {"s": item[0], "c": CHANNELS.index(channel)})
                    for old_key, (kind, deadline, old_channel, cleared) in self._pending_output.items():
                        if kind == "pulse" and old_channel == channel and not cleared:
                            self._pending_output[old_key] = (kind, deadline, old_channel, True)
                    self._pending_output[key] = ("clear", time.monotonic() + 3, channel, False)
                with suppress(ConnectionClosed, TimeoutError, OSError):
                    await self._send(ws, pkt)

    async def stop(self):
        await self._clear(set(CHANNELS))
        self._emit()

    async def _output_failed(self, ws):
        self._state_at = 0
        self._detail = "App 未确认波形控制；已停止，须重新配对"
        await self.stop()
        with suppress(ConnectionClosed, TimeoutError):
            async with asyncio.timeout(2):
                await ws.close(1011, "operation not confirmed")

    async def _watchdog(self):
        last_mono, last_wall = time.monotonic(), time.time()
        next_poll, next_beat = last_mono + 4, last_mono + 15
        while True:
            await asyncio.sleep(0.5)
            now, wall = time.monotonic(), time.time()
            if not self._connected:
                if not self._qr or now > self._pair_deadline:
                    async with self._lifecycle:
                        await self._unpair("配对码已过期或手机已断开；请重新生成", keep_note=True)
                    return
            elif now - last_mono > WATCHDOG_GAP or wall - last_wall > WATCHDOG_GAP:
                self._state_at = 0
                await self.stop()
                async with self._lifecycle:
                    await self._unpair("后台执行中断；已停止并断开，须重新配对")
                return
            else:
                ws = self._peer
                if self._model == "v4" and not self._fresh() and any(self._available.values()):
                    await self._state(dict.fromkeys(CHANNELS), dict.fromkeys(CHANNELS),
                                      dict.fromkeys(CHANNELS, False), self._active_slot)
                # Finite packets expire on the app. Never replay an old or failed pulse.
                self._queued = {c: item for c, item in self._queued.items() if item[1] + 0.5 > now}
                if ws:
                    if any(rec[1] < now for rec in self._pending_output.values()):
                        await self._output_failed(ws)
                        continue
                    try:
                        if self._model == "v4" and now >= next_poll:
                            next_poll = now + 4
                            await self._get_devices(ws)
                        if now >= next_beat:
                            next_beat = now + 15
                            await self._send(ws, {"type": "heartbeat"} if self._model == "v4" else
                                             {"type": "heartbeat", "clientId": self._phone,
                                              "targetId": self._secret_at_connect, "message": "200"})
                    except (ConnectionClosed, TimeoutError, OSError):
                        self._state_at = 0
                        await self.stop()
                        with suppress(ConnectionClosed, TimeoutError):
                            async with asyncio.timeout(2):
                                await ws.close(1011, "link lost")
            last_mono, last_wall = now, wall
            self._watchdog_clock = (now, wall)






