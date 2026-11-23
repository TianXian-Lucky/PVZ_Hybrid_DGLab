"""Standalone host for the unchanged plugin. No DGHub installation or credentials needed."""
import asyncio
import hmac
import json
import math
import os
import secrets
import re
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))
import main as plugin
from gate import preset as fixed_preset
from waves import load_presets, frame_at, preview

class Pipe:
    """A private in-process implementation of the plugin's SDK message transport."""
    def __init__(self, host):
        self.host, self.q, self.ack = host, asyncio.Queue(), None
        self.key, self.hello = secrets.token_urlsafe(24), False

    async def send(self, raw):
        msg = json.loads(raw)
        if not self.hello:
            if msg.get("op") != "hello" or not hmac.compare_digest(msg.get("token",""), self.key):
                raise ValueError("本地宿主握手失败")
            self.hello = True
            return
        if self.host.pipe is self:
            await self.host.message(msg)

    async def recv(self):
        if not self.hello: raise RuntimeError("missing handshake")
        return '{"op":"hello_ack","accepted":true,"sdk_version":"1.2"}'

    def __aiter__(self): return self

    async def __anext__(self):
        if self.ack and not self.ack.done(): self.ack.set_result(True)
        msg, self.ack = await self.q.get()
        return json.dumps(msg, ensure_ascii=False)

    async def notify(self, msg, wait=False):
        f = asyncio.get_running_loop().create_future() if wait else None
        await self.q.put((msg,f))
        if f: await asyncio.wait_for(f, 6)

class Host:
    def __init__(self, folder=None, event_path=None, device_factory=None):
        from device import Device
        self.manifest = json.loads((ROOT / "core" / "manifest.json").read_text("utf-8-sig"))
        self.fields = {f["key"]: f for s in self.manifest["config_schema"] for f in s["fields"]}
        self.config = {k:f["default"] for k,f in self.fields.items()}
        self.presets = load_presets(ROOT)
        self.folder = Path(folder) if folder is not None else ROOT / "state"
        self.path = self.folder / "settings.json"
        self.user_presets = {}
        self.events = event_path or plugin.EVENTS_FILE
        self.error = ""; self.storage_fault = False
        self.baseline = 0; self.temp = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                val = data["baseline"]
                self.user_presets = self.parse_presets(data.get("presets",{}), allow_empty=True)
                self.presets.update(self.user_presets)
                if type(val) != int or not -100 <= val <= 100: raise ValueError()
                cfg = data["config"]
                if not isinstance(cfg, dict): raise ValueError()
                self.config.update({k:self.validate(k,v) for k,v in cfg.items()})
                self.baseline = val
            except (ValueError, KeyError, TypeError, OSError):
                self.error = "本地配置无法读取，已禁止启用且不会覆盖。退出后备份并移开 state/settings.json 可恢复默认设置。"
                self.storage_fault = True
        self.pipe = None; self.runner = None; self.worker = None; self.permit = ""
        self.status = {}; self.logs = deque(maxlen=180); self.cards = deque(maxlen=80)
        self.actions = asyncio.Lock(); self.wave_lock = asyncio.Lock()
        self.last_ui = time.monotonic(); self.device_sig = None
        self.quit = asyncio.Event()
        self.device = (device_factory or Device)(self.device_changed)
        self.log("独立宿主已启动；尚未启用插件或开放手机配对。")

    def log(self, text):
        self.logs.append(time.strftime("%H:%M:%S") + "  " + str(text)[:580])

    def validate(self, key, value):
        f = self.fields.get(key)
        if not f: raise ValueError("未知配置项")
        typ = f["type"]
        if typ == "bool" and type(value) is bool: return value
        if typ == "select" and value in [x["value"] for x in f["options"]]: return value
        if typ == "channel" and value in ("a","b","both"): return value
        if typ == "preset" and isinstance(value,str) and value in self.presets: return value
        if typ in ("duration","number") and type(value) in (int,float) and math.isfinite(value):
            if f.get("min",0.1) <= value <= f.get("max",300) and (typ=="duration" or int(value)==value):
                return float(value) if typ=="duration" else int(value)
        raise ValueError("配置值无效：" + f["label"])

    def parse_presets(self, data, allow_empty=False):
        if not isinstance(data,dict) or (not data and not allow_empty) or len(data)+len(self.user_presets)>16:
            raise ValueError("最多可添加 16 项自定义固定预设")
        parsed = {}
        for name, rows in data.items():
            if (not isinstance(name,str) or not name.strip() or len(name)>48 or name in self.presets
                or any(ord(c)<32 for c in name)):
                raise ValueError("预设名称无效或已存在")
            if fixed_preset(name)!=name: raise ValueError("请选择有效且固定的预设名称")
            if not isinstance(rows,list) or not 1<=len(rows)<=512: raise ValueError("每项预设须含 1–512 帧")
            for f in rows:
                if not isinstance(f,str) or not re.fullmatch("[0-9A-Fa-f]{16}",f): raise ValueError("波形帧格式错误")
                b=bytes.fromhex(f)
                if min(b[:4])<10 or max(b[:4])>240 or max(b[4:])>100: raise ValueError("波形帧数值超出协议范围")
            parsed[name]=[f.upper() for f in rows]
        return parsed

    def save(self):
        if self.storage_fault: raise ValueError(self.error)
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w",encoding="utf-8") as handle:
            json.dump({"config":self.config,"baseline":self.baseline,"presets":self.user_presets},handle,ensure_ascii=False,indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def device_view(self): return self.device.view()["device"]

    def route(self):
        return ("a","b") if self.config["channel"]=="both" else (self.config["channel"],)

    def ready(self, d):
        return bool(d.get("connected") and d.get("ready") and d.get("id") and
                    all(d["channels"].get(c) and int(d["limits"].get(c) or 0)>0 for c in self.route()))

    def allowed(self):
        d = self.device_view()
        return bool(self.pipe and self.permit and self.permit==d.get("id") and self.ready(d)
                    and self.config["show_damage"] and time.monotonic()-self.last_ui < 15)

    def info(self):
        d = self.device_view()
        ok = self.allowed()
        # SDK v1 calls its two-channel Coyote capability profile "v3".
        # Only a verified compatible Coyote channel pair can be projected into it.
        return {"op":"device_info","device_type":"v3","id":d.get("id",""),"connected":ok,
                **{"max_strength_"+c:int(d["limits"].get(c) or 0) if d["channels"].get(c) else 0 for c in ("a","b")}}

    def device_changed(self):
        d = self.device_view()
        if self.permit and (self.permit != d.get("id") or not self.ready(d)):
            self.permit = ""; self.temp.clear()
            self.log("设备身份、通道或状态失效：本次输出许可已撤销。")
            if self.worker: self.worker.cancel()
            asyncio.create_task(self.device.stop())
        if self.pipe:
            msg = self.info()
            sig = (id(self.pipe), str(msg))
            if sig != self.device_sig:
                self.device_sig = sig
                self.pipe.q.put_nowait((msg,None))

    async def clear_wave(self):
        async with self.wave_lock:
            await self._clear_wave()

    async def _clear_wave(self):
        t,self.worker = self.worker,None
        if t:
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        await self.device.stop()

    def gain(self, channel, at=None):
        if not self.config["strength_enable"]: return 0
        value, expiry = self.temp.get(channel,(self.baseline,0))
        return value if (time.monotonic() if at is None else at)<expiry else self.baseline

    def strength(self,msg):
        if not self.config["strength_enable"]: return
        delta = msg["delta_pct"]
        if msg["strength_mode"]=="permanent":
            old = self.baseline
            # Persist before allowing the following wave. Reductions are not extra quota.
            self.baseline = max(0,min(100, self.config["strength_cap"] if delta>0 else 100, old + delta))
            try: self.save()
            except (OSError, ValueError):
                self.baseline=old; self.permit=""; self.temp.clear()
                self.storage_fault=True; self.error="强度基线未能保存。输出已停，请检查文件写入权限并重启。"
                self.device_changed()
                raise
            self.temp.clear()
        else:
            for c in self.route():
                self.temp[c] = (max(-100,min(100,self.baseline + delta)),time.monotonic()+msg["duration_s"])

    async def play(self, preset, channels, duration):
        frames = self.presets[preset]; total = max(1,math.ceil(duration*40-1e-8))
        loop = asyncio.get_running_loop(); start = loop.time(); i=0
        try:
            while 4*i < total and self.allowed():
                delay = start + i*0.1 - loop.time()
                if delay > 0: await asyncio.sleep(delay)
                if not self.allowed() or loop.time() - (start+i*.1) > .15: break
                n=min(2, math.ceil((total-i*4)/4))
                for c in channels:
                    if not self.allowed(): return
                    packet=[]
                    base_time=time.monotonic()
                    for j in range(i,i+n):
                        gains=[self.gain(c,base_time+(j-i)*.1+s*.025) for s in range(4)]
                        packet.append(frame_at(frames,j,gains,min(4,total-j*4)))
                    if not await self.device.apply({"kind":"pulse","channel":c,"frames":packet}):
                        self.permit=""; self.temp.clear(); self.log("设备没有接受波形，已停止，需重新允许输出。")
                        self.device_changed(); await self.device.stop(); return
                i += n
        except asyncio.CancelledError: raise
        except Exception as e:
            self.permit=""; self.temp.clear(); self.error="设备发送失败："+type(e).__name__
            self.log(self.error); self.device_changed(); await self.device.stop()

    async def message(self,msg):
        op = msg.get("op")
        if op=="status":
            self.status=msg.get("fields",{}); return
        if op=="event":
            self.cards.appendleft({"title":msg.get("label"),"detail":msg.get("name"),"time":time.strftime("%H:%M:%S")})
            return
        if op=="pulse_state" and not msg.get("active"):
            await self.clear_wave(); return
        if not self.allowed() or msg.get("channel")!=self.config["channel"]: return
        if op=="trigger" and msg.get("action")=="strength":
            self.strength(msg); return
        if (op=="pulse_state" or (op=="trigger" and msg.get("action")=="waveform")) and msg.get("preset") in self.presets:
            async with self.wave_lock:
                await self._clear_wave()
                if self.allowed():
                    self.worker=asyncio.create_task(self.play(msg["preset"],self.route(),min(2.0,msg["duration_s"])))

    async def run_plugin(self,pipe):
        try: await plugin.run(pipe, pipe.key, self.events)
        except asyncio.CancelledError: raise
        except Exception as e: self.error="插件退出："+type(e).__name__; self.log(self.error)
        finally:
            if self.pipe is pipe:
                self.pipe=None; self.permit=""; self.temp.clear()
                await self.clear_wave()

    async def enable(self):
        if self.storage_fault: raise ValueError(self.error)
        if self.pipe: return
        d = self.device_view()
        self.permit = d["id"] if self.ready(d) else ""
        self.status={}; self.error=""; self.temp.clear()
        self.pipe=pipe=Pipe(self); self.runner=asyncio.create_task(self.run_plugin(pipe))
        try:
            await pipe.notify({"op":"config","data":{**self.config,"idle_strength":self.baseline}},True)
            await pipe.notify(self.info(),True)
        except BaseException:
            await self.disable(); raise
        self.log("插件已启用。" + ("本次设备反馈已获准。" if self.permit else "当前仅观察，设备就绪后请手动允许反馈。"))

    async def disable(self):
        self.permit=""; self.pipe=None; self.temp.clear(); self.device_sig=None
        task,self.runner=self.runner,None
        if task:
            task.cancel(); await asyncio.gather(task,return_exceptions=True)
        await self.clear_wave()
        self.status={}; self.log("插件反馈已停止。")

    async def action(self, data):
        async with self.actions:
            name=data.get("action")
            if name=="enable": await self.enable()
            elif name in ("stop","disable"): await self.disable()
            elif name=="arm":
                d=self.device_view()
                if not self.pipe or not self.ready(d) or not self.config["show_damage"]:
                    raise ValueError("请先启用插件、打开总开关并在手机确认所选通道。")
                self.permit=d["id"]; await self.pipe.notify(self.info(),True); self.log("已手动允许本次设备反馈。")
            elif name=="config":
                k=data.get("key"); v=self.validate(k,data.get("value"))
                if v == self.config[k]: return
                old=self.config[k]; self.config[k]=v
                try: self.save()
                except BaseException:
                    self.config[k]=old
                    await self.disable()
                    raise
                if self.pipe:
                    old_id=self.permit; self.permit=""; self.temp.clear()
                    await self.clear_wave()
                    # Do not grant a new route/target any inherited output permission.
                    d=self.device_view()
                    if k not in ("channel","show_damage") and old_id==d.get("id") and self.ready(d):
                        self.permit=old_id
                    await self.pipe.notify({"op":"config_changed","key":k,"value":v},True)
                    await self.pipe.notify(self.info(),True)
                self.log("已保存："+self.fields[k]["label"])
            elif name=="import_presets":
                extra=self.parse_presets(data.get("value"))
                self.user_presets.update(extra)
                try: self.save()
                except BaseException:
                    for k in extra: self.user_presets.pop(k)
                    raise
                self.presets.update(extra); self.log(f"已添加 {len(extra)} 项固定预设。")
            elif name=="reset_baseline":
                await self.disable()
                prev,self.baseline=self.baseline,0
                try: self.save()
                except BaseException: self.baseline=prev; raise
                self.log("已停止反馈并重置插件累积，不会改变手机档位。")
            elif name in ("pair","unpair","slot"):
                self.permit=""; self.temp.clear(); await self.clear_wave(); self.device_changed()
                if name=="pair": await self.device.pair(data.get("ip"),data.get("model"))
                elif name=="slot": await self.device.select(data.get("value"))
                else: await self.device.unpair()
                self.log({"pair":"等待手机扫码。","slot":"已选择设备槽位。","unpair":"手机连接已断开。"}[name])
            elif name=="quit":
                await self.disable(); await self.device.unpair(); self.quit.set()
            else: raise ValueError("未知操作")

    def view(self):
        return {"manifest":self.manifest,"config":self.config,"enabled":bool(self.pipe),"output_enabled":bool(self.permit and self.allowed()),
                "status":self.status,"cards":list(self.cards),"logs":list(self.logs),"error":self.error,
                "presets":list(self.presets),"preview":preview(self.presets[self.config["preset"]]),
                "strength_baseline":self.baseline, **self.device.view()}

    async def watchdog(self):
        while True:
            await asyncio.sleep(.5)
            if self.permit and not self.allowed():
                self.permit=""; self.temp.clear(); await self.clear_wave(); self.device_changed()
                self.log("面板心跳或设备条件失效，已暂停反馈。")

