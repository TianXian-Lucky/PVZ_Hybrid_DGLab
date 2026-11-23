"""PVZ Hybrid standalone local dashboard. No access to DGHub files or secrets."""
import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
import webbrowser
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
from host import Host, ROOT

def check_core():
    for row in (ROOT / "CORE-SHA256.txt").read_text("ascii").splitlines():
        name, expected = row.split()
        if hashlib.sha256((ROOT / "core" / name).read_bytes()).hexdigest() != expected.lower():
            raise RuntimeError("原版文件校验失败：" + name)

def webapp(host, key, authority, *, read_only=False):
    sockets=set()
    @web.middleware
    async def protect(req, handler):
        if req.host != authority or (req.headers.get("Origin") not in (None, "http://"+authority)):
            raise web.HTTPForbidden()
        if req.path=="/api/presence":
            proto=[p.strip() for p in req.headers.get("Sec-WebSocket-Protocol","").split(",")]
            if ("pvz-presence-v1" not in proto or not any(secrets.compare_digest(p.encode(),("session."+key).encode()) for p in proto)):
                raise web.HTTPForbidden()
        elif req.path.startswith("/api/"):
            if not secrets.compare_digest(req.headers.get("X-PVZ-Session","").encode(), key.encode()):
                raise web.HTTPForbidden()
        resp = await handler(req)
        resp.headers.update({"Cache-Control":"no-store", "Referrer-Policy":"no-referrer",
            "X-Content-Type-Options":"nosniff", "X-Frame-Options":"DENY",
            "Content-Security-Policy":"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://"+authority+"; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"})
        return resp

    async def presence(req):
        if len(sockets)>=8: raise web.HTTPTooManyRequests()
        ws=web.WebSocketResponse(protocols=("pvz-presence-v1",),autoping=False,max_msg_size=128)
        await ws.prepare(req); sockets.add(ws)
        try:
            while not ws.closed and not host.quit.is_set():
                nonce=secrets.token_bytes(12)
                await ws.ping(nonce)
                packet=await ws.receive(timeout=4)
                if packet.type!=WSMsgType.PONG or packet.data!=nonce: break
                host.last_ui=time.monotonic()
                try: await asyncio.wait_for(host.quit.wait(),3)
                except TimeoutError: pass
        except (TimeoutError,ConnectionError): pass
        finally:
            sockets.discard(ws); await ws.close()
        return ws

    async def close_sockets(_):
        await asyncio.gather(*(s.close() for s in tuple(sockets)),return_exceptions=True)

    async def state(req):
        host.last_ui=time.monotonic()
        return web.json_response(host.view())

    async def action(req):
        if req.content_type != "application/json": raise web.HTTPUnsupportedMediaType()
        try:
            body=await req.json()
            if not isinstance(body,dict): raise ValueError("需要 JSON 对象")
            if not isinstance(body.get("action"),str): raise ValueError("缺少操作")
            if read_only and body["action"] != "quit":
                raise ValueError("桌面验收模式不允许启用游戏或设备。")
            host.last_ui=time.monotonic()
            await host.action(body)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"ok":False,"error":str(exc)[:240]},status=409)
        except (OSError,TimeoutError) as exc:
            return web.json_response({"ok":False,"error":"操作没有完成，请检查本机权限或网络："+type(exc).__name__},status=503)
        return web.json_response(host.view())

    async def asset(req):
        fn={"/":"index.html","/styles.css":"styles.css","/app.js":"app.js"}[req.path]
        return web.Response(body=(ROOT/"web"/fn).read_bytes(),content_type={"index.html":"text/html","styles.css":"text/css","app.js":"application/javascript"}[fn],charset="UTF-8")
    app=web.Application(middlewares=[protect],client_max_size=196608)
    for u in ("/","/styles.css","/app.js"): app.router.add_get(u,asset)
    app.router.add_get("/api/state",state)
    app.router.add_get("/api/presence",presence)
    app.on_shutdown.append(close_sockets)
    app.router.add_post("/api/action",action)
    return app

@contextlib.asynccontextmanager
async def panel(host, *, read_only=False):
    key=secrets.token_urlsafe(32)
    sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    sock.bind(("127.0.0.1",0)); authority="127.0.0.1:"+str(sock.getsockname()[1])
    runner=web.AppRunner(webapp(host,key,authority,read_only=read_only),access_log=None)
    watchdog=None
    try:
        await runner.setup()
        await web.SockSite(runner,sock).start()
        watchdog=asyncio.create_task(host.watchdog())
        yield "http://"+authority, key
    finally:
        if watchdog:
            watchdog.cancel(); await asyncio.gather(watchdog,return_exceptions=True)
        try:
            await host.disable()
            await host.device.unpair()
        finally:
            await runner.cleanup(); sock.close()

async def smoke():
    check_core()
    with tempfile.TemporaryDirectory() as tmp:
        h=Host(folder=Path(tmp))
        async with panel(h) as (url,key), ClientSession() as c:
            async with c.get(url+"/") as r:
                assert r.status==200 and "受伤反馈" in await r.text()
            for path in ("app.js","styles.css"):
                async with c.get(url+"/"+path) as r: assert r.status==200 and len(await r.read())>1000
            async with c.get(url+"/api/state") as r: assert r.status==403
            async with c.get(url+"/api/state",headers={"X-PVZ-Session":key}) as r:
                v=await r.json(); assert r.status==200 and not v["enabled"] and not v["device"]["connected"]
                assert len(v["manifest"]["config_schema"])==4 and len(v["presets"])>=24
    print("PASS: portable runtime, original hashes, loopback panel, authenticated API, 4 UI sections, 24 stock presets. No live device or game.")

def tests():
    check_core()
    suite=unittest.TestSuite()
    for module in ("test_feedback","test_device","test_host","test_e2e"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()

async def launch(no_browser):
    check_core()
    host=Host()
    async with panel(host) as (base,key):
        url=base+"/#session="+key
        print("独立版已启动。请保留此窗口，在面板内选择「退出」可关闭。", flush=True)
        print("仅供本机打开，请勿分享："+url,flush=True)
        if not no_browser: webbrowser.open_new(url)
        await host.quit.wait()

def follow_desktop_owner(host):
    """The GUI exclusively owns this pipe. EOF (including GUI crash) stops the host."""
    loop=asyncio.get_running_loop()
    fd=sys.stdin.fileno()
    def watch():
        try:
            # This privatised channel has only one operation: finish. Other input
            # also fails closed. os.read avoids buffered-stdin locks at shutdown.
            os.read(fd,32)
        except OSError:
            pass
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(host.quit.set)
    threading.Thread(target=watch,name="DesktopOwner",daemon=True).start()

async def launch_desktop(verify=False):
    check_core()
    # Never use an existing player's settings or event stream for the packaged
    # native-window smoke. Device/Mod actions are rejected in this mode.
    with (tempfile.TemporaryDirectory() if verify else contextlib.nullcontext(None)) as tmp:
        host=Host(folder=Path(tmp),event_path=Path(tmp)/"events.jsonl") if verify else Host()
        async with panel(host,read_only=verify) as (base,key):
            follow_desktop_owner(host)
            # Private stdout pipe only, never a command-line argument / log file.
            print(json.dumps({"kind":"panel","url":base+"/#session="+key}),flush=True)
            await host.quit.wait()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group()
    g.add_argument("--self-test",action="store_true"); g.add_argument("--smoke-test",action="store_true")
    g.add_argument("--desktop",action="store_true",help=argparse.SUPPRESS)
    g.add_argument("--desktop-verify",action="store_true",help=argparse.SUPPRESS)
    p.add_argument("--no-browser",action="store_true")
    args=p.parse_args()
    if args.smoke_test: asyncio.run(smoke()); return 0
    if args.self_test: return 0 if tests() else 1
    if args.desktop_verify: asyncio.run(launch_desktop(True)); return 0
    handle=None
    if os.name=="nt":
        dll=ctypes.WinDLL("kernel32",use_last_error=True)
        dll.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p]
        dll.CreateMutexW.restype=ctypes.c_void_p
        dll.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=dll.CreateMutexW(None,False,"Local\\PVZHybridFeedbackStandalone")
        if not handle: raise OSError("无法获取单实例锁")
        if ctypes.get_last_error()==183:
            dll.CloseHandle(handle)
            if not args.desktop: print("独立版已经在运行。请先退出原先启动的版本。")
            return 2
    try:
        if args.desktop: asyncio.run(launch_desktop())
        else: asyncio.run(launch(args.no_browser))
    except KeyboardInterrupt: pass
    finally:
        if handle: dll.CloseHandle(handle)
    return 0

if __name__=="__main__":
    sys.exit(main())

