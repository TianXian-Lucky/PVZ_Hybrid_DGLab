"""DGHub 外部插件入口。

只做三件事：连宿主、转发消息、跟随事件文件。
所有决策逻辑在 gate（状态机）与 observer（校验/去重）里。

作者：TianXianbaobao
本插件为开源项目，请勿倒卖。禁止商用。
原项目地址以发布页为准；如为付费购买所得，请申请退款并举报。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

from gate import COMPONENTS, EVENTS, Gate, Message
from observer import Policy, Tail, parse
import mod_install

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
MOD_SOURCE = HERE / mod_install.MOD_FILENAME      # 随插件分发的 .pmod
EVENTS_FILE = Path(os.environ.get("LOCALAPPDATA", "")) / "PVZHybridDGHub" / "events.jsonl"
TICK = 0.10              # 轮询间隔
HANDSHAKE_TIMEOUT = 8.0
SEND_TIMEOUT = 1.0
STATUS_DEBOUNCE = 2.0
ALLOWED_OPS = frozenset(("hello", "event", "status", "pong"))


# ---- 连接 ------------------------------------------------------------
def endpoint(host: str, port: str, token: str) -> str:
    """只允许回环地址；token 不落盘、不打印、不进日志。"""
    if host == "localhost":
        host = "127.0.0.1"
    ip = ipaddress.ip_address(host)
    if not ip.is_loopback or not token:
        raise ValueError("需要由 DGHub 在回环地址上启动")
    if not 1 <= int(port) <= 65535:
        raise ValueError("端口越界")
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"ws://{host}:{int(port)}/ws/plugin?token={quote(token, safe='')}"


def supports_state(version: str | None) -> bool:
    """pulse_state 需要 SDK >= 1.2。按数值比较，不按字符串。"""
    try:
        parts = [int(p) for p in str(version or "").split(".")[:3]]
    except (TypeError, ValueError):
        return False
    parts += [0] * (3 - len(parts))
    return tuple(parts) >= (1, 2, 0)


def decode(raw) -> dict:
    try:
        if not isinstance(raw, (str, bytes)) or len(raw) > 65536:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError, RecursionError):
        return {}


class Link:
    """出站唯一通道。波形/强度/状态三类消息都只能经 Message.wire() 产生。"""

    def __init__(self, ws):
        self.ws = ws

    async def send(self, item):
        if isinstance(item, Message):
            item = item.wire()
        elif type(item) is not dict or item.get("op") not in ALLOWED_OPS:
            raise ValueError("不支持出站消息")
        async with asyncio.timeout(SEND_TIMEOUT):
            await self.ws.send(json.dumps(item, ensure_ascii=False, allow_nan=False))

    async def drain(self, gate, extra: Message | None = None):
        """先补发门控队列，再发本次结果 —— 保证「先停后开」的顺序。"""
        for pending in gate.drain():
            await self.send(pending)
        if extra is not None:
            await self.send(extra)


# ---- 状态上报 --------------------------------------------------------
def snapshot(tail: Tail, policy: Policy, gate: Gate, mod_plan=None) -> dict:
    source_states = {"unreadable": ("暂时无法读取事件文件", "warn"),
                     "missing": ("等待游戏创建事件文件", "pending"),
                     "present": ("正在读取游戏记录", "ok")}
    if tail.state == "present" and not policy.accepted:
        source, state = "已找到文件，等待新的受伤记录", "pending"
    else:
        source, state = source_states[tail.state]

    mode_name = "持续受伤节奏" if gate.mode == "rhythm" else "经典单次"
    if gate.mode == "rhythm" and not gate.state_waves:
        mode_name += "（宿主 SDK < 1.2，退回单次波形）"

    plant_types = "、".join(label for key, _, label in COMPONENTS if gate.filters[key])
    chosen = "；".join(s for s in (
        f"植物：{plant_types}" if plant_types else "",
        "僵尸受伤" if gate.filters["notify_zombie"] else "") if s)

    counts = (f"收到 {policy.accepted} 次，命中 {policy.eligible} 次，"
              f"显示 {policy.displayed} 张")

    if not gate.enabled:
        activity = "仅统计（总开关关闭）"
    elif gate.armed:
        activity = "受伤反馈已就绪"
    else:
        activity = "仅显示（条件未就绪）"

    if gate.channel is None:
        route = "通道无效；请重新选择 A+B、A 或 B。"
    elif gate.device is None:
        route = f"已选 {gate.channel_label}，等待设备状态。"
    elif not gate.route_ready:
        route = f"已选 {gate.channel_label}，所选通道上限为零；不会改发其他通道。"
    else:
        route = f"已选 {gate.channel_label}，V3 状态与通道上限已确认。"

    if gate.strength_on:
        if gate.strength_mode == "permanent":
            if gate.permanent_exhausted:
                # 重启后基线已达上限：说明额度可能已被上一次运行用尽。
                tail_text = (f"永久累积 已达上限 {gate.strength_cap}%"
                             f"（宿主基线 {gate.baseline_value}%），本次不再增加")
                if gate.blocked_restart:
                    tail_text += f"；拦下 {gate.blocked_restart} 次"
            else:
                tail_text = f"永久累积 {gate.accum}/{gate.strength_cap}%"
                if gate.blocked:
                    tail_text += f"（已触顶，拦下 {gate.blocked} 次）"
        else:
            tail_text = f"临时 {gate.strength_seconds:g} 秒后回落"
        strength = (f"植物 {gate.delta_plant:+d}% / 僵尸 {gate.delta_zombie:+d}%，"
                    f"{tail_text}，已发 {gate.strength_sent} 次"
                    f"（本次累计 {gate.strength_total:+d}%）")
        if gate.permanent_exhausted:
            strength_state = "warn"
        else:
            strength_state = "ok" if gate.strength_sent else "pending"
    else:
        strength = "强度反馈已关闭，不发送任何强度指令"
        strength_state = "warn"

    return {"op": "status", "fields": {
        "display_status": (f"{activity} · {mode_name} · 输出 {gate.channel_label}"
                           f" · {counts} · 已请求 {gate.count} 次"),
        "feedback_mode": gate.mode,
        "channel": gate.channel,
        **gate.filters,
        "armed": gate.armed,
        "config_received": gate.configured,
        "device_info_received": gate.device_seen,
        "v3_ready": gate.route_ready,
        "preset_selected": bool(gate.preset),
        "session_requests": gate.count,
        "state_wave_active": gate.state_on,
        "received": policy.accepted,
        "eligible": policy.eligible,
        "shown": policy.displayed,
        "plant_received": policy.received[EVENTS[0]],
        "plant_eligible": policy.hit[EVENTS[0]],
        "zombie_received": policy.received[EVENTS[1]],
        "zombie_eligible": policy.hit[EVENTS[1]],
        "total_hp_lost": policy.total_life,
        "total_armor_lost": policy.total_armor,
        "total_shield_absorbed": policy.total_shield,
        "strength_enabled": gate.strength_on,
        "strength_plant": gate.delta_plant,
        "strength_zombie": gate.delta_zombie,
        "strength_mode": gate.strength_mode,
        "strength_duration": gate.strength_seconds,
        "strength_cap": gate.strength_cap,
        "strength_accum": gate.accum,
        "strength_baseline": gate.baseline_value,
        "strength_exhausted": gate.permanent_exhausted,
        "strength_blocked_restart": gate.blocked_restart,
        "strength_sent": gate.strength_sent,
        "strength_total": gate.strength_total,
        "strength_blocked": gate.blocked,
        "strength_clamped": gate.clamped,
        "startup_check": {"title": "植物与僵尸受伤反馈 · 启动检查", "steps": [
            {"key": "plugin", "title": "主程序与配置",
             "state": "ok" if gate.configured else "pending",
             "detail": "插件已运行，配置已加载" if gate.configured else "等待初始配置"},
            {"key": "mod", "title": "游戏端 Mod",
             "state": {"ok": "ok", "need_restart": "warn", "game_running": "warn",
                       "ready": "pending", "no_source": "warn",
                       "error": "fail"}.get(mod_plan.state if mod_plan else "ready", "pending"),
             "detail": mod_plan.detail if mod_plan else "尚未检查",
             "hint": ("Mod 装好后必须重启游戏才会加载。"
                      if mod_plan and mod_plan.needs_restart
                      else "自动安装可在「游戏 Mod」分组关闭。")},
            {"key": "source", "title": "游戏数据（未筛选）", "state": state,
             "detail": (f"{source}。植物 {policy.received[EVENTS[0]]} 次，"
                        f"僵尸 {policy.received[EVENTS[1]]} 次。合计：生命 -{policy.total_life:g}、"
                        f"护甲 -{policy.total_armor:g}、格盾吸收 {policy.total_shield:g}。"),
             "hint": "僵尸记录需要游戏观察器 0.2.0 或更新。历史记录不会回放。"},
            {"key": "filters", "title": "触发事件",
             "state": "ok" if chosen else "warn",
             "detail": (f"勾选：{chosen}。{counts}。命中：植物 {policy.hit[EVENTS[0]]} 次，"
                        f"僵尸 {policy.hit[EVENTS[1]]} 次。" if chosen
                        else f"未勾选任何类型，仅保留统计。{counts}。"),
             "hint": "一击混合伤害只计一次。植物与僵尸共用反馈间隔，卡片可能合并。"},
            {"key": "preset", "title": "固定波形预设",
             "state": "ok" if gate.preset else "pending",
             "detail": (f"已选择固定预设：{gate.preset}" if gate.preset
                        else "未选择有效固定预设，不会请求波形"),
             "hint": "随机/默认一类预设会被拒绝。若本机没有所选预设，设备也不会响。"},
            {"key": "v3", "title": "输出通道与 V3 状态",
             "state": "ok" if gate.route_ready else "warn" if gate.device_seen else "pending",
             "detail": route,
             "hint": "请在 DGHub 确认目标与所选通道的手动强度；选择通道本身不会试播。"},
            {"key": "strength", "title": "强度反馈", "state": strength_state,
             "detail": strength,
             "hint": "增量叠加在 DGHub 为该插件维护的强度层上，不覆盖设备端手动设置。"},
            {"key": "interlock", "title": "本次插件反馈",
             "state": "ok" if gate.armed else "pending",
             "detail": (f"{activity}；{mode_name}，输出 {gate.channel_label}，"
                        f"已请求 {gate.count} 次（不限次数、不限时长）。{gate.reason}"),
             "hint": "条件齐备会自动进入反馈；关闭总开关即停止。"}]}}}


# ---- 主流程 ----------------------------------------------------------
async def run(ws, token: str, event_path: Path):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
    link = Link(ws)
    await link.send({"op": "hello", "token": token, "manifest": manifest})
    ack = decode(await asyncio.wait_for(ws.recv(), HANDSHAKE_TIMEOUT))
    if ack.get("op") != "hello_ack" or ack.get("accepted") is not True:
        raise RuntimeError("DGHub 拒绝了握手")
    gate = Gate()
    gate.state_waves = supports_state(ack.get("sdk_version"))
    policy = Policy()
    tail = Tail(event_path)
    ready = asyncio.Event()
    # 游戏端 Mod 的安装状态。初始为"尚未检查"，收到配置后按开关决定是否投放。
    mod_plan = [None]
    await link.send(snapshot(tail, policy, gate, mod_plan[0]))

    def install_mod(data) -> None:
        """按配置决定是否把自带 .pmod 投放到游戏 Mod 目录。

        只在收到配置时尝试一次；游戏在运行则不写入，只提示重启。
        任何异常都降级成提示，绝不影响插件本身。
        """
        try:
            enabled = (data or {}).get("auto_install_mod", True) is True
            if not enabled:
                mod_plan[0] = mod_install.inspect(MOD_SOURCE)
                return
            plan = mod_install.install(MOD_SOURCE)
            if plan.state in ("ready",) and "未覆盖" in plan.detail:
                # 已存在不同版本：不擅自替换，交给用户决定
                pass
            mod_plan[0] = plan
        except Exception as exc:                      # noqa: BLE001 - 兜底
            mod_plan[0] = mod_install.Plan("error", f"自动安装失败：{type(exc).__name__}")

    def reset_tail():
        """重新锚定到文件末尾：绝不补播本次会话开始前的记录。"""
        nonlocal tail
        tail = Tail(event_path)
        if tail.state == "unreadable":
            gate.disarm("无法读取游戏事件")

    async def autostart():
        """DGHub 拉起本插件 = 用户启用了它，条件齐备即进入会话。"""
        if gate.armed or not gate.can_begin:
            return False
        if gate.begin(time.time(), time.monotonic()):
            reset_tail()
            return True
        return False

    async def control(extra: Message | None = None):
        await link.drain(gate, extra)
        await link.send(snapshot(tail, policy, gate, mod_plan[0]))

    async def receive():
        async for raw in ws:
            data = decode(raw)
            op = data.get("op")
            if op == "stop":
                gate.disarm("主程序请求停止")
                return
            if op == "ping":
                stamp = data.get("t")
                if type(stamp) in (int, float) and math.isfinite(stamp) and abs(stamp) < 1e15:
                    await link.send({"op": "pong", "t": stamp})
            elif op == "config":
                install_mod(data.get("data"))
                gate.config(data.get("data"))
                policy.configure("show_damage", gate.enabled)
                if gate.configured:
                    ready.set()
                await autostart()
                await control()
            elif op == "config_changed":
                key, value = data.get("key"), data.get("value")
                if key == "auto_install_mod" and value is True:
                    install_mod({"auto_install_mod": True})
                elif key == "auto_install_mod":
                    mod_plan[0] = mod_install.inspect(MOD_SOURCE)
                if gate.change(key, value, time.time(), time.monotonic()):
                    reset_tail()
                elif key != "show_damage":
                    await autostart()
                policy.configure("show_damage", gate.enabled)
                await control()
            elif op == "device_info":
                gate.device_info(data)
                await autostart()
                await control()
            elif op is None or op in ("profile_changed", "target_changed", "limits_changed"):
                gate.invalidate("主程序状态或目标变化")
                await autostart()
                await control()
        gate.disarm("主程序连接已关闭")

    async def follow():
        # SDK 保证会推一次全量配置；在此之前不拿默认值出卡片。
        await ready.wait()
        signature = (tail.state, policy.accepted, policy.displayed, gate.armed)
        reported = time.monotonic()
        while True:
            gate.tick(time.time(), time.monotonic())
            if gate.pending:
                await link.drain(gate)
            generation = gate.generation
            was_present = tail.state
            lines = tail.read()
            if gate.armed and (tail.state == "unreadable"
                               or (was_present == "present" and tail.state == "missing")):
                gate.disarm("游戏事件文件不可用")
            for line in lines:
                record = parse(line)
                now, mono = time.time(), time.monotonic()
                card, eligible = policy.observe(record, now, mono, gate)
                if eligible:
                    message = gate.admit(record, generation, now, mono)
                    if message is not None or gate.pending:
                        await link.drain(gate, message)
                elif gate.pending:
                    await link.drain(gate)
                if card:
                    await link.send(card)
            current = (tail.state, policy.accepted, policy.displayed, gate.armed)
            if current != signature and (time.monotonic() - reported >= STATUS_DEBOUNCE
                                         or current[-1] != signature[-1]):
                await link.send(snapshot(tail, policy, gate, mod_plan[0]))
                signature, reported = current, time.monotonic()
            await asyncio.sleep(TICK)

    tasks = [asyncio.create_task(receive()), asyncio.create_task(follow())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        gate.disarm("连接结束")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def live():
    import inspect
    import websockets

    token = os.environ["DGHUB_TOKEN"]
    uri = endpoint(os.environ["DGHUB_HOST"], os.environ["DGHUB_PORT"], token)
    options = dict(max_size=65536, max_queue=16, open_timeout=8, close_timeout=2,
                   ping_interval=5, ping_timeout=2)
    if "proxy" in inspect.signature(websockets.connect).parameters:
        options["proxy"] = None
    async with websockets.connect(uri, **options) as ws:
        await run(ws, token, EVENTS_FILE)


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        import unittest
        import test_feedback
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromModule(test_feedback))
        return 0 if result.wasSuccessful() else 1
    try:
        asyncio.run(live())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"DGHub 反馈插件退出：{type(exc).__name__}。"
              "请通过 DGHub 启用，或用 --self-test 离线自检。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

