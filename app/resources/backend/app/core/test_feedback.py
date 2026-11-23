"""离线自检：不联网、不需要设备、不需要令牌。

作者：TianXianbaobao
本插件为开源项目，请勿倒卖。禁止商用。

覆盖四层：
  * gate     —— 准入、节流、状态波形、强度封顶
  * observer —— 校验、去重、计数、卡片抑制
  * main     —— 端点校验、SDK 版本探测、出站白名单
  * 集成     —— 起一个假 DGHub 跑真实子进程
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import mod_install

from gate import (Gate, Message, DEFAULT_CAP, DEFAULT_DELTA_PLANT, DEFAULT_DELTA_ZOMBIE,
                  HOLD_SECONDS, MAX_AGE, STOP_AFTER, WAVE_SECONDS, cap, delta, duration,
                  mode, preset, channel)
from main import Link, decode, endpoint, snapshot, supports_state
from observer import Policy, Tail, parse

PRESET = "测试-固定预设"
V3 = dict(connected=True, device_type="v3", max_strength_a=99, max_strength_b=99)
CFG = dict(preset=PRESET, show_damage=True)


def armed(**over):
    """构造一个已进入会话的 gate。"""
    g = Gate()
    g.config({**CFG, **over})
    g.device_info(V3)
    assert g.begin(1000.0, 10.0), "fixture failed to arm"
    return g


def record(seq=1, ts=1000.5, sid="s", event="plant_damage", life=5, armor=0, shield=0,
           target="9"):
    """一条形状与游戏 writer 完全一致的记录。时间基准与 armed() 的 1000.0 对齐。"""
    return dict(schema=1, event=event, session=sid, seq=seq, ts=ts, target=target,
                target_type="PlantPot", life_lost=life, armor_lost=armor,
                shield_absorbed=shield)


class Normalisation(unittest.TestCase):
    def test_channel(self):
        for good in ("a", "b", "both"):
            self.assertEqual(channel(good), good)
        for bad in (None, "", "A", "both ", 1, ["a"]):
            self.assertIsNone(channel(bad), bad)

    def test_preset_rejects_randomness(self):
        for bad in (None, "", "*", "默认", "AUTOMATIC", "ＲＡＮＤＯＭ", "随机波形", "\nDemo", "a" * 100):
            self.assertIsNone(preset(bad), bad)
        self.assertEqual(preset(PRESET), PRESET)

    def test_delta(self):
        self.assertEqual(delta(10), 10)
        self.assertEqual(delta(500), 100)
        self.assertEqual(delta(-500), -100)
        self.assertEqual(delta(3.6), 4)
        for bad in ("10", None, [1], float("nan"), float("inf")):
            self.assertEqual(delta(bad), 0, bad)

    def test_duration_and_mode(self):
        self.assertEqual(duration(3), 3.0)
        self.assertEqual(duration(0), 0.1)
        self.assertEqual(duration(999), 300.0)
        self.assertEqual(duration(None), 3.0)
        self.assertEqual(mode("permanent"), "permanent")
        self.assertEqual(mode("bogus"), "rollback")

    def test_cap(self):
        self.assertEqual(cap(500), 100)
        self.assertEqual(cap(-20), 0)
        self.assertEqual(cap(37.6), 38)
        self.assertEqual(cap("80"), DEFAULT_CAP)


class MessageShape(unittest.TestCase):
    def test_wave(self):
        m = Message("wave", PRESET, "a", event="plant_damage").wire()
        self.assertEqual(m, {"op": "trigger", "action": "waveform", "preset": PRESET,
                             "channel": "a", "duration_s": WAVE_SECONDS,
                             "label": "植物受伤"})

    def test_state_open_and_close(self):
        on = Message("state", PRESET, "a", active=True).wire()
        self.assertEqual(on, {"op": "pulse_state", "state_id": "pvz-hybrid-rhythm",
                              "active": True, "preset": PRESET, "channel": "a",
                              "duration_s": HOLD_SECONDS})
        off = Message("state", PRESET, "a", active=False).wire()
        self.assertEqual(off, {"op": "pulse_state", "state_id": "pvz-hybrid-rhythm",
                               "active": False})

    def test_strength_carries_no_preset(self):
        m = Message("strength", PRESET, "a", event="zombie_damage", delta=5,
                    strength_mode="rollback", seconds=3).wire()
        self.assertEqual(m["action"], "strength")
        self.assertEqual(m["delta_pct"], 5)
        self.assertEqual(m["label"], "僵尸受伤 +5%")
        self.assertTrue({"preset", "action", "pct"}.isdisjoint(set(m) - {"action"}))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            Message("wave", "随机波形", "a").wire()
        with self.assertRaises(ValueError):
            Message("wave", PRESET, "z").wire()
        with self.assertRaises(ValueError):
            Message("strength", PRESET, "a", delta=0).wire()
        with self.assertRaises(ValueError):
            Message("bogus", PRESET, "a").wire()


class Arming(unittest.TestCase):
    def test_needs_every_prerequisite(self):
        cases = [dict(preset=""), dict(preset="随机波形"), dict(show_damage=False),
                 dict(idle_strength=1),
                 dict(notify_life=False, notify_armor=False,
                      notify_shield=False)]
        for over in cases:
            g = Gate()
            g.config({**CFG, **over})
            g.device_info(V3)
            self.assertFalse(g.begin(1000.0, 10.0), over)
            self.assertFalse(g.armed, over)

    def test_manifest_default_preset_lets_new_user_run(self):
        """新用户不手动选预设也必须能出波形 —— 否则装完插件永远不动。"""
        manifest = json.loads((Path(__file__).parent / "manifest.json")
                              .read_text(encoding="utf-8-sig"))
        defaults = {f["key"]: f.get("default")
                    for sec in manifest["config_schema"] for f in sec["fields"]
                    if "default" in f}
        self.assertTrue(isinstance(defaults.get("preset"), str) and defaults["preset"],
                        "manifest 里 preset 没有可用的默认值，新用户会一直不输出")

        g = Gate()
        g.config(defaults)                      # 模拟 DGHub 按默认值下发
        self.assertIsNotNone(g.preset, f"默认预设被校验拒绝: {defaults['preset']!r}")
        g.device_info(V3)
        self.assertTrue(g.begin(1000.0, 10.0), "默认配置下无法启用")

        g.tick(1000.1, 10.1)
        message = g.admit(record(1, ts=1000.1), g.generation, 1000.1, 10.1)
        self.assertIsNotNone(message, "默认配置下第一条伤害没有产生波形")
        wire = message.wire()
        self.assertEqual(wire["action"], "waveform")
        self.assertEqual(wire["preset"], defaults["preset"])

    def test_preset_default_is_a_real_builtin_name(self):
        """默认值必须是 DGHub 内置预设名（不能是随机或占位符）。"""
        manifest = json.loads((Path(__file__).parent / "manifest.json")
                              .read_text(encoding="utf-8-sig"))
        fields = {f["key"]: f for sec in manifest["config_schema"] for f in sec["fields"]}
        value = fields["preset"]["default"]
        self.assertIsNotNone(preset(value), f"默认预设在 gate 校验里无效: {value!r}")
        # 随机/默认一类必须被排除
        for forbidden in ("随机波形", "默认", "random", "*", ""):
            self.assertNotEqual(value, forbidden)

    def test_nonzero_baseline_allowed_only_with_strength(self):
        # 强度反馈关闭时，非零基线说明有别的环境动过插件的强度层，不接管。
        g = Gate()
        g.config({**CFG, "idle_strength": 25})
        g.device_info(V3)
        self.assertFalse(g.begin(1000.0, 10.0))

        # 强度反馈开启时，非零基线是永久累积的正常结果，必须放行 ——
        # 否则插件会把自己永久锁死。
        g2 = Gate()
        g2.config({**CFG, "idle_strength": 25, "strength_enable": True,
                   "strength_mode": "permanent"})
        g2.device_info(V3)
        self.assertTrue(g2.begin(1000.0, 10.0), "永久模式把基线推高后锁死了自己")

    def test_bad_device_reports(self):
        for bad in ({"connected": False}, {"device_type": "v2"},
                    {"max_strength_a": 0}, {"max_strength_a": None},
                    {"max_strength_a": 201}, {"max_strength_b": "0"}):
            g = Gate()
            g.config(CFG)
            g.device_info({**V3, **bad})
            self.assertFalse(g.begin(1000.0, 10.0), bad)

    def test_master_switch_is_the_consent_edge(self):
        g = Gate()
        g.config({**CFG, "show_damage": False})
        g.device_info(V3)
        self.assertFalse(g.armed)
        self.assertTrue(g.change("show_damage", True, 1000.0, 10.0))
        self.assertTrue(g.armed)

    def test_obsolete_arm_key_is_inert(self):
        g = armed()
        self.assertFalse(g.change("arm", False, 1001.0, 11.0))
        self.assertTrue(g.armed)
        self.assertFalse(g.change("arm", True, 1001.0, 11.0))
        self.assertTrue(g.armed)

    def test_route_caps(self):
        for ch, a, b, ok in (("a", 1, 0, True), ("b", 0, 1, True), ("both", 1, 1, True),
                             ("both", 1, 0, False), ("a", 0, 1, False), ("b", 1, 0, False)):
            g = Gate()
            g.config({**CFG, "channel": ch})
            g.device_info({**V3, "max_strength_a": a, "max_strength_b": b})
            self.assertIs(g.begin(1000.0, 10.0), ok, (ch, a, b))

    def test_device_repair_does_not_drop_session(self):
        g = armed()
        g.device_info({**V3, "max_strength_a": 150})   # 仅上报刷新
        self.assertTrue(g.armed)
        g.device_info({**V3, "max_strength_a": 0})     # 通道不可用
        self.assertFalse(g.armed)

    def test_sleep_or_block_ends_session(self):
        g = armed()
        g.tick(1000.0, 10.0)
        g.tick(1000.0 + 200, 10.0 + 200)
        self.assertFalse(g.armed)

    def test_jitter_keeps_session(self):
        g = armed()
        t = 0.0
        for gap in (0.1, 0.5, 2.5, 12.0, 45.0, 170.0):
            t += gap
            g.tick(1000.0 + t, 10.0 + t)
            self.assertTrue(g.armed, gap)


class Throttle(unittest.TestCase):
    def test_classic_cooldown(self):
        g = armed()
        self.assertIsNotNone(g.admit(record(1, ts=1000.6), g.generation, 1000.6, 10.6))
        for i in range(2, 8):
            t = 1000.6 + i * .1
            self.assertIsNone(g.admit(record(i, ts=t), g.generation, t, 10.6 + i * .1))
        self.assertIsNotNone(g.admit(record(9, ts=1006.0), g.generation, 1006.0, 16.0))

    def test_stale_and_future_records_rejected(self):
        g = armed()
        self.assertIsNone(g.admit(record(1, ts=900.0), g.generation, 1000.0, 10.0))
        self.assertIsNone(g.admit(record(2, ts=2000.0), g.generation, 1000.0, 10.0))

    def test_stale_generation_rejected(self):
        g = armed()
        self.assertIsNone(g.admit(record(1, ts=1000.6), g.generation - 1, 1000.6, 10.6))

    def test_filters(self):
        g = armed(notify_life=False, notify_armor=True)
        self.assertFalse(g.matches(record(life=5, armor=0)))
        self.assertTrue(g.matches(record(life=0, armor=3)))

    def test_zombie_needs_opt_in(self):
        g = armed()
        z = record(event="zombie_damage", armor=5)
        self.assertFalse(g.matches(z))
        g2 = armed(notify_zombie=True)
        self.assertTrue(g2.matches(z))

    def test_game_restart_resets_without_disarming(self):
        g = armed()
        g.admit(record(1, ts=1000.6), g.generation, 1000.6, 10.6)
        gen = g.generation
        self.assertIsNone(g.admit(record(1, sid="new", ts=1000.7), gen, 1000.7, 10.7))
        self.assertTrue(g.armed, "游戏重开不该停掉反馈")
        self.assertNotEqual(g.generation, gen)


class StateWave(unittest.TestCase):
    def feed(self, g, start, seconds, target="9", event="plant_damage"):
        """按 65ms 一条推入持续伤害，产出 (相对时刻, 消息)。

        注意 admit() 的返回值也要收进来 —— 状态波形的第一段是直接返回的，
        续播段才走 outbox。
        """
        t = 0.0
        seq = 0
        while t < seconds:
            t += 0.065
            seq += 1
            wall, mono = 1000.0 + start + t, 10.0 + start + t
            g.tick(wall, mono)
            message = g.admit(record(seq, ts=wall, target=target, event=event),
                              g.generation, wall, mono)
            if message is not None:
                yield (start + t, message)
            for msg in g.drain():
                yield (start + t, msg)

    def test_continuous_run_has_no_gap(self):
        g = armed(feedback_mode="rhythm")
        timeline = list(self.feed(g, 0.0, 20.0))
        states = [(t, m.active) for t, m in timeline if m.kind == "state"]
        self.assertGreaterEqual(len(states), 3, "状态波形从未续播")
        # off 之后必须紧跟 on，中间不能有静默窗口
        for i, (t, active) in enumerate(states[:-1]):
            if active is False:
                self.assertIs(states[i + 1][1], True)
                self.assertLessEqual(abs(states[i + 1][0] - t), 1e-6)
        live = peak = 0
        for _, active in states:
            live += 1 if active else -1
            peak = max(peak, live)
        self.assertEqual(peak, 1, "同时开了两段状态波形")

    def test_stops_after_damage_ends(self):
        g = armed(feedback_mode="rhythm")
        list(self.feed(g, 0.0, 3.0))
        self.assertTrue(g.state_on)
        g.tick(1003.0 + STOP_AFTER + 0.1, 13.0 + STOP_AFTER + 0.1)
        self.assertFalse(g.state_on)

    def test_handover_after_idle_opens_new_state(self):
        """旧那株停了之后再换目标：先关旧波形，再开新波形。"""
        g = armed(feedback_mode="rhythm")
        list(self.feed(g, 0.0, 2.0, target="57"))
        self.assertTrue(g.state_on)
        # 静置超过 STOP_AFTER，tick 会把旧波形关掉
        g.tick(1002.0 + STOP_AFTER + 0.1, 12.0 + STOP_AFTER + 0.1)
        self.assertFalse(g.state_on)
        self.assertTrue(any(m.kind == "state" and m.active is False for m in g.drain()),
                        "停咬后没有关掉状态波形")
        # 新目标受伤 → 开新波形（首次进入时由 admit 的返回值直接发出）
        t = 3.0
        wall, mono = 1000.0 + t, 10.0 + t
        g.tick(wall, mono)
        message = g.admit(record(1, ts=wall, target="other"), g.generation, wall, mono)
        opened = [m for m in ([message] + g.drain())
                  if m is not None and m.kind == "state" and m.active is True]
        self.assertTrue(opened, "新目标没有打开状态波形")
        self.assertTrue(g.state_on)

    def test_concurrent_targets_do_not_stack(self):
        """旧那株仍在承伤时，新目标不抢输出 —— 避免两段波形叠加。"""
        g = armed(feedback_mode="rhythm")
        list(self.feed(g, 0.0, 2.0, target="57"))
        self.assertTrue(g.state_on)
        # 57 号仍在密集承伤，此时另一个目标也受伤
        t = 2.0 + 0.065
        wall, mono = 1000.0 + t, 10.0 + t
        g.tick(wall, mono)
        message = g.admit(record(1, ts=wall, target="other"), g.generation, wall, mono)
        self.assertIsNone(message, "抢了正在承伤那一株的输出")
        self.assertEqual([m for m in g.drain() if m.kind == "state"], [])

    def test_disarm_stops_device(self):
        g = armed(feedback_mode="rhythm")
        list(self.feed(g, 0.0, 2.0))
        self.assertTrue(g.state_on)
        g.disarm("测试")
        pending = g.drain()
        self.assertTrue(any(m.kind == "state" and m.active is False for m in pending),
                        "disarm 没有发出停止消息")

    def test_sdk_fallback_uses_single_wave(self):
        g = armed(feedback_mode="rhythm")
        g.state_waves = False
        timeline = list(self.feed(g, 0.0, 1.0))
        kinds = {m.kind for _, m in timeline}
        self.assertIn("wave", kinds, "SDK < 1.2 时没有退回单次波形")
        self.assertNotIn("state", kinds)

    def test_fallback_never_emits_state_on_any_path(self):
        """降级路径下，续播 / 空闲停止 / disarm 都不许出现 pulse_state。"""
        g = armed(feedback_mode="rhythm")
        g.state_waves = False
        emitted = list(self.feed(g, 0.0, 8.0))       # 8 秒密集，必然跨越续播阈值
        g.tick(1008.0 + STOP_AFTER + 0.2, 18.0 + STOP_AFTER + 0.2)
        emitted += [(8.0, m) for m in g.drain()]
        g.disarm("测试")
        emitted += [(8.0, m) for m in g.drain()]

        kinds = [m.kind for _, m in emitted]
        self.assertNotIn("state", kinds, f"降级路径泄漏了 pulse_state: {sorted(set(kinds))}")
        self.assertIn("wave", kinds)
        self.assertFalse(g.state_on, "停咬后不该还认为在输出")

    def test_fallback_still_sustains_while_damaged(self):
        """旧 SDK 上同样要"续伤就持续"：波形必须首尾相接，不能每刀一次。"""
        g = armed(feedback_mode="rhythm")
        g.state_waves = False
        timeline = list(self.feed(g, 0.0, 8.0))
        waves = [(t, m) for t, m in timeline if m.kind == "wave"]
        self.assertGreaterEqual(len(waves), 3, "回退路径没有续播")
        # 每段时长应覆盖到下一段开始，形成连续输出
        for (_, earlier), (next_t, _next) in zip(waves, waves[1:]):
            self.assertGreaterEqual(earlier.seconds, 1.0,
                                    "回退波形太短，会出现断点")
        gaps = [b[0] - a[0] for a, b in zip(waves, waves[1:])]
        longest = max(m.seconds for _, m in waves)
        for gap in gaps:
            self.assertLessEqual(gap, longest + 1e-6,
                                 f"续播间隔 {gap:.3f}s 超过了单段时长 {longest}s，中间会断")
        # 8 秒承伤不该发出上百条
        self.assertLess(len(waves), 12, f"回退路径仍在刷屏：{len(waves)} 条")

    def test_fallback_stops_requesting_after_damage_ends(self):
        """停咬后不再续播；最后一段自然放完后结束。"""
        g = armed(feedback_mode="rhythm")
        g.state_waves = False
        list(self.feed(g, 0.0, 3.0))
        self.assertTrue(g.state_on)
        before = len(g.drain())
        # 空闲超过 STOP_AFTER
        g.tick(1003.0 + STOP_AFTER + 0.1, 13.0 + STOP_AFTER + 0.1)
        self.assertFalse(g.state_on, "停咬后仍认为在输出")
        # 回退路径没有 pulse_state 可发，停止是"不再续播"而非发 off
        off = [m for m in g.drain() if m.kind == "state"]
        self.assertEqual(off, [], "回退路径发出了 pulse_state 停止消息")

    def test_fallback_shorter_than_sdk12_stop(self):
        """SDK 1.2 能立即停；回退只能靠最后一段自然结束 —— 记录这个差异。"""
        g = armed(feedback_mode="rhythm")
        g.state_waves = False
        list(self.feed(g, 0.0, 3.0))
        waves = [m for _, m in self.feed(g, 3.0, 0.1) if m.kind == "wave"]
        tail = max((m.seconds for m in waves), default=HOLD_SECONDS)
        # 这就是最坏情况下的"拖尾"：回退路径停咬后最多再响这么久
        self.assertLessEqual(tail, HOLD_SECONDS, "回退拖尾超过了状态波形单段时长")


class Strength(unittest.TestCase):
    def test_defaults(self):
        g = armed(strength_enable=True)
        self.assertEqual((g.delta_plant, g.delta_zombie), (DEFAULT_DELTA_PLANT, DEFAULT_DELTA_ZOMBIE))

    def test_disabled_sends_nothing(self):
        g = armed()
        p = Policy()
        r = record(1, ts=1000.6)
        p.observe(r, 1000.6, 10.6, g)
        g.admit(r, g.generation, 1000.6, 10.6)
        self.assertEqual([m for m in g.drain() if m.kind == "strength"], [])
        self.assertEqual(g.strength_sent, 0)

    def test_plant_and_zombie_independent(self):
        g = armed(strength_enable=True, notify_zombie=True)
        p = Policy()
        r = record(1, ts=1000.6)
        p.observe(r, 1000.6, 10.6, g)
        g.admit(r, g.generation, 1000.6, 10.6)
        got = [m.wire() for m in g.drain() if m.kind == "strength"]
        self.assertEqual(got[0]["delta_pct"], 10)
        self.assertEqual(got[0]["label"], "植物受伤 +10%")

        z = record(2, ts=1006.0, event="zombie_damage")
        p.observe(z, 1006.0, 16.0, g)
        g.admit(z, g.generation, 1006.0, 16.0)
        got = [m.wire() for m in g.drain() if m.kind == "strength"]
        self.assertEqual(got[0]["delta_pct"], 5)
        self.assertEqual(got[0]["label"], "僵尸受伤 +5%")

    def test_zero_silences_one_kind(self):
        g = armed(strength_enable=True, strength_plant=0)
        p = Policy()
        r = record(1, ts=1000.6)
        p.observe(r, 1000.6, 10.6, g)
        g.admit(r, g.generation, 1000.6, 10.6)
        self.assertEqual([m for m in g.drain() if m.kind == "strength"], [])

    def test_rhythm_applies_once_per_run(self):
        g = armed(feedback_mode="rhythm", strength_enable=True, strength_mode="permanent")
        count = 0
        t = 0.0
        while t < 12.0:
            t += 0.065
            g.tick(1000.0 + t, 10.0 + t)
            g.admit(record(int(t * 100), ts=1000.0 + t), g.generation,
                    1000.0 + t, 10.0 + t)
            count += sum(1 for m in g.drain() if m.kind == "strength")
        self.assertEqual(count, 1, f"一个持续段加了 {count} 次强度")
        self.assertEqual(g.strength_total, 10)

    def test_cap_stops_exactly(self):
        g = armed(strength_enable=True, strength_mode="permanent", strength_cap=25)
        got = []
        t = 0.0
        for _ in range(8):
            t += 6.0
            g.tick(1000.0 + t, 10.0 + t)
            g.admit(record(int(t), ts=1000.0 + t), g.generation, 1000.0 + t, 10.0 + t)
            got += [m.delta for m in g.drain() if m.kind == "strength"]
        self.assertEqual(got, [10, 10, 5], "封顶没有精确停在 25%")
        self.assertEqual(g.accum, 25)
        self.assertEqual(g.clamped, 1)
        self.assertEqual(g.blocked, 5)

    def test_cap_zero_disables_growth(self):
        g = armed(strength_enable=True, strength_mode="permanent", strength_cap=0)
        t = 0.0
        for _ in range(4):
            t += 6.0
            g.tick(1000.0 + t, 10.0 + t)
            g.admit(record(int(t), ts=1000.0 + t), g.generation, 1000.0 + t, 10.0 + t)
            self.assertEqual([m for m in g.drain() if m.kind == "strength"], [])

    def test_cap_ignores_rollback_mode(self):
        g = armed(strength_enable=True, strength_mode="rollback", strength_cap=0)
        got = 0
        t = 0.0
        for _ in range(4):
            t += 6.0
            g.tick(1000.0 + t, 10.0 + t)
            g.admit(record(int(t), ts=1000.0 + t), g.generation, 1000.0 + t, 10.0 + t)
            got += sum(1 for m in g.drain() if m.kind == "strength")
        self.assertEqual(got, 4, "临时模式被上限误伤")

    def test_negative_delta_not_blocked(self):
        g = armed(strength_enable=True, strength_mode="permanent",
                  strength_cap=10, strength_plant=-5)
        got = 0
        t = 0.0
        for _ in range(3):
            t += 6.0
            g.tick(1000.0 + t, 10.0 + t)
            g.admit(record(int(t), ts=1000.0 + t), g.generation, 1000.0 + t, 10.0 + t)
            got += sum(1 for m in g.drain() if m.kind == "strength")
        self.assertEqual(got, 3, "负增量被上限拦住")

    # ---- 跨进程额度（BUG-1）-----------------------------------------

    def permanent_at(self, idle, cap=50, plant=10):
        """构造一个"带宿主基线"的 gate，模拟插件重启后的状态。"""
        g = Gate()
        g.config(dict(CFG, strength_enable=True, strength_mode="permanent",
                      strength_plant=plant, strength_cap=cap, idle_strength=idle))
        g.device_info(V3)
        self.assertTrue(g.begin(1000.0, 10.0), "fixture failed to arm")
        return g

    def drain_strength(self, g, rounds=6, step=6.0, start=1000.0, seq0=0, mono=None):
        """喂 rounds 次伤害，返回发出的强度增量。

        同一测试内多次调用时，必须让 mono 继续前进 —— tick() 把时间倒退
        视为运行连续性中断并 disarm。默认从 10.0 起步，多次调用请显式传入。
        """
        got = []
        wall, clock = start, (10.0 if mono is None else mono)
        for i in range(1, rounds + 1):
            wall += step; clock += step
            g.tick(wall, clock)
            g.admit(record(i + seq0, ts=wall), g.generation, wall, clock)
            got += [m.delta for m in g.drain() if m.kind == "strength"]
        return got

    def test_restart_at_cap_grants_nothing(self):
        """S1：重启后宿主基线已达上限，不得再发放额度。"""
        g = self.permanent_at(idle=50, cap=50)
        self.assertTrue(g.permanent_exhausted)
        got = self.drain_strength(g)
        self.assertEqual(got, [], f"重启后又发放了额度: {got}")
        self.assertGreater(g.blocked_restart, 0, "拦下次数没有记录")

    def test_restart_below_cap_respects_remaining(self):
        """S2：基线 45、上限 50 时只剩 5 的额度，不能放出 50。"""
        g = self.permanent_at(idle=45, cap=50)
        self.assertFalse(g.permanent_exhausted)
        got = self.drain_strength(g)
        self.assertEqual(sum(got), 5, f"放出 {sum(got)}，应为剩余额度 5")
        self.assertEqual(g.baseline_value + sum(got), 50, "没有正好封顶")
        self.assertEqual(g.clamped, 1)

    def test_restart_at_zero_is_not_penalised(self):
        """S3：首次运行不该被误伤。"""
        g = self.permanent_at(idle=0, cap=50)
        self.assertFalse(g.permanent_exhausted)
        got = self.drain_strength(g)
        self.assertEqual(got, [10, 10, 10, 10, 10])
        self.assertEqual(sum(got), 50)

    def test_host_not_applying_still_bounded(self):
        """S3a：宿主始终回传基线 0（请求未应用），本地累计仍须受限。"""
        g = self.permanent_at(idle=0, cap=50)
        got = []
        wall, mono = 1000.0, 10.0
        for i in range(1, 9):
            wall += 6.0; mono += 6.0
            g.tick(wall, mono)
            g.admit(record(i, ts=wall), g.generation, wall, mono)
            got += [m.delta for m in g.drain() if m.kind == "strength"]
            g.change("idle_strength", 0, wall + .1, mono + .1)   # 宿主没应用
            g.begin(wall + .2, mono + .2)
        self.assertLessEqual(sum(got), 50, f"宿主未应用时越界: {sum(got)}")

    def test_host_echoing_baseline_never_exceeds_cap(self):
        """S3b：宿主回传的基线包含本次增量，累计不得越界（宁可少发）。"""
        g = self.permanent_at(idle=0, cap=50)
        got, applied = [], 0
        wall, mono = 1000.0, 10.0
        for i in range(1, 9):
            wall += 6.0; mono += 6.0
            g.tick(wall, mono)
            g.admit(record(i, ts=wall), g.generation, wall, mono)
            batch = [m.delta for m in g.drain() if m.kind == "strength"]
            got += batch
            applied += sum(batch)
            g.change("idle_strength", applied, wall + .1, mono + .1)
            g.begin(wall + .2, mono + .2)
        self.assertLessEqual(applied, 50, f"终值越界: {applied}")

    def test_negative_delta_allowed_when_exhausted(self):
        """已用尽时仍允许减弱 —— 上限只管往上加。"""
        g = Gate()
        g.config(dict(CFG, strength_enable=True, strength_mode="permanent",
                      strength_plant=-5, strength_cap=50, idle_strength=50))
        g.device_info(V3)
        self.assertTrue(g.begin(1000.0, 10.0))
        self.assertTrue(g.permanent_exhausted)
        got = self.drain_strength(g, rounds=3)
        self.assertEqual(got, [-5, -5, -5], "负增量被上限误伤")

    def test_switching_to_rollback_clears_exhausted(self):
        """换回临时模式后，基线不再限制。"""
        g = self.permanent_at(idle=50, cap=50)
        self.assertTrue(g.permanent_exhausted)
        g.change("strength_mode", "rollback", 1001.0, 11.0)
        self.assertFalse(g.permanent_exhausted)
        self.assertTrue(g.begin(1002.0, 12.0))
        got = self.drain_strength(g, rounds=3, start=1002.0)
        self.assertEqual(got, [10, 10, 10], "临时模式被基线限制")

    def test_raising_cap_reenables_quota(self):
        """把上限调高后应当重新获得额度。"""
        g = self.permanent_at(idle=50, cap=50)
        self.assertTrue(g.permanent_exhausted)
        g.change("strength_cap", 80, 1001.0, 11.0)
        self.assertFalse(g.permanent_exhausted, "调高上限后仍被判用尽")
        self.assertTrue(g.begin(1002.0, 12.0))
        got = self.drain_strength(g, rounds=5, start=1002.0)
        self.assertEqual(sum(got), 30, f"应剩 30 额度，实际 {sum(got)}")

    def test_negative_delta_does_not_release_quota(self):
        """负增量不释放额度 —— 未被宿主确认的减弱不能变成新的增加空间。

        反例：cap=0 时连发三次 -20%，若把负增量计入 accum，
        accum 会变成 -60，remaining 算成 60，随后放出 +40 的正增量，
        上限被完全绕过。
        """
        g = Gate()
        g.config(dict(CFG, strength_enable=True, strength_mode="permanent",
                      strength_cap=0, strength_plant=-20))
        g.device_info(V3)
        self.assertTrue(g.begin(1000.0, 10.0))

        got = self.drain_strength(g, rounds=3)
        self.assertEqual(got, [-20, -20, -20], "负增量本身应被允许")
        self.assertEqual(g.accum, 0, f"负增量污染了额度计数: accum={g.accum}")

        # 切成正增量后，cap=0 不应放出任何额度
        g.change("strength_plant", 10, 1020.0, 30.0)
        self.assertTrue(g.begin(1021.0, 31.0))
        got = self.drain_strength(g, rounds=4, start=1021.0)
        self.assertEqual(got, [], f"cap=0 却放出了正增量: {got}")

    def test_negative_then_positive_respects_cap(self):
        """负增量之后转正，剩余额度仍按原上限扣除，不被放大。"""
        g = Gate()
        g.config(dict(CFG, strength_enable=True, strength_mode="permanent",
                      strength_cap=50, strength_plant=-30))
        g.device_info(V3)
        self.assertTrue(g.begin(1000.0, 10.0))
        neg = self.drain_strength(g, rounds=3, start=1000.0)   # 三次 -30，mono 到 28
        self.assertEqual(neg, [-30, -30, -30])
        self.assertEqual(g.accum, 0, "负增量污染了额度计数")

        g.change("strength_plant", 20, 1018.5, 28.5)
        self.assertTrue(g.begin(1019.0, 29.0))
        got = self.drain_strength(g, rounds=5, start=1019.0, seq0=3, mono=29.0)
        # cap=50，未被负增量放大，最多只能加 50
        self.assertEqual(sum(got), 50, f"额度被负增量放大: {sum(got)}")
        self.assertLessEqual(sum(got), g.strength_cap)
        # 逐次应为 20 / 20 / 10，最后一次正好封顶
        self.assertEqual(got, [20, 20, 10])


class Observer(unittest.TestCase):
    def gate(self, **over):
        return armed(**over)

    def test_accepts_valid(self):
        p, g = Policy(), self.gate()
        card, hit = p.observe(record(1, ts=100.0), 100.1, 10.1, g)
        self.assertIsNotNone(card)
        self.assertTrue(hit)
        self.assertEqual((p.accepted, p.eligible, p.displayed), (1, 1, 1))

    def test_rejects_bad_shapes(self):
        p, g = Policy(), self.gate()
        bad = [None, {"schema": 2}, {"schema": 1, "event": "other"},
               record(sid="has space"), record(seq=-1), record(seq=1.5),
               record(life=0, armor=0, shield=0), record(life=-1),
               record(ts=100.0 - 99), {"schema": 1, "event": "plant_damage"}]
        for r in bad:
            card, hit = p.observe(r, 100.1, 10.1, g)
            self.assertIsNone(card, r)
            self.assertFalse(hit, r)
        self.assertEqual(p.accepted, 0)

    def test_sequence_watermark(self):
        p, g = Policy(), self.gate()
        p.observe(record(5, ts=100.0), 100.1, 10.1, g)
        card, hit = p.observe(record(5, ts=100.2), 100.3, 10.3, g)
        self.assertIsNone(card, "重放没有被挡住")
        self.assertFalse(hit)
        self.assertEqual(p.accepted, 1)

    def test_filtered_still_counted(self):
        p, g = Policy(), self.gate(notify_life=False)
        card, hit = p.observe(record(1, ts=1000.6, life=5, armor=0), 1000.6, 10.6, g)
        self.assertIsNone(card)
        self.assertFalse(hit)
        self.assertEqual(p.accepted, 1, "被筛掉的记录仍应计数")
        self.assertEqual(p.received["plant_damage"], 1)

    def test_card_suppression(self):
        p, g = Policy(), self.gate()
        p.observe(record(1, ts=1000.6), 1000.6, 10.6, g)
        card, _ = p.observe(record(2, ts=1000.8, target="9"), 1000.8, 10.8, g)
        self.assertIsNone(card, "同株静默没有生效")
        card, _ = p.observe(record(3, ts=1001.0, target="10"), 1001.0, 11.0, g)
        self.assertIsNotNone(card, "不同目标被误伤")

    def test_master_off_stops_cards(self):
        p, g = Policy(), self.gate()
        p.configure("show_damage", False)
        card, hit = p.observe(record(1, ts=1000.6), 1000.6, 10.6, g)
        self.assertIsNone(card, "总开关关闭时不该出卡片")
        self.assertEqual(p.accepted, 1)
        self.assertEqual(g.count, 0)

    def test_tail_skips_history(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            path.write_text('{"old":1}\n', encoding="utf-8")
            tail = Tail(path)
            self.assertEqual(tail.read(), [], "启动时不该回放历史")
            with path.open("a", encoding="utf-8") as f:
                f.write('{"new":2}\n')
            self.assertEqual([parse(x) for x in tail.read()], [{"new": 2}])


class Wiring(unittest.TestCase):
    def test_endpoint_rejects_non_loopback(self):
        self.assertEqual(endpoint("127.0.0.1", "8000", "t"),
                         "ws://127.0.0.1:8000/ws/plugin?token=t")
        # localhost 归一化成 127.0.0.1
        self.assertEqual(endpoint("localhost", "8000", "t"),
                         "ws://127.0.0.1:8000/ws/plugin?token=t")
        for host in ("10.0.0.1", "example.com", "0.0.0.0"):
            with self.assertRaises(ValueError):
                endpoint(host, "8000", "t")
        with self.assertRaises(ValueError):
            endpoint("127.0.0.1", "8000", "")

    def test_sdk_probe(self):
        self.assertTrue(supports_state("1.2.0"))
        self.assertTrue(supports_state("1.3"))
        self.assertFalse(supports_state("1.0"))
        self.assertFalse(supports_state(None))
        self.assertFalse(supports_state("bogus"))

    def test_decode(self):
        self.assertEqual(decode('{"op":"ping"}'), {"op": "ping"})
        self.assertEqual(decode("not json"), {})
        self.assertEqual(decode("[1,2]"), {})
        self.assertEqual(decode(b"\xff"), {})

    def test_egress_whitelist(self):
        class Fake:
            async def send(self, raw):
                self.raw = raw

        link = Link(Fake())
        for op in ("set_strength", "adjust_strength", "pulse", "trigger", "set_config"):
            with self.assertRaises(ValueError):
                asyncio.run(link.send({"op": op}))
        with self.assertRaises(ValueError):
            asyncio.run(link.send(Message("wave", "随机波形", "a")))
        asyncio.run(link.send({"op": "pong", "t": 1}))
        self.assertEqual(json.loads(link.ws.raw), {"op": "pong", "t": 1})

    def test_snapshot_is_serialisable(self):
        g = armed(strength_enable=True)
        p = Policy()
        s = snapshot(Tail(__import__("pathlib").Path("nope")), p, g)
        json.dumps(s)
        self.assertIn("startup_check", s["fields"])
        self.assertTrue(s["fields"]["armed"])


def make_pmod(mod_id=None, version="0.2.1"):
    """造一个结构合法的最小 pmod（zip + mod.json），供安装器测试用。

    installed_by_id() 会去读包里的 mod.json 来认 ID，所以测试夹具必须是
    真正的 zip，不能用随便几个字节糊弄。
    """
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mod.json", json.dumps({
            "schemaVersion": 3,
            "id": mod_id or mod_install.MOD_ID,
            "name": "fixture",
            "version": version,
        }, ensure_ascii=False))
        archive.writestr("Runtime/ModAssembly.dll", b"MZ fixture")
    return buf.getvalue()


class ModInstaller(unittest.TestCase):
    """游戏端 Mod 的自动投放。全部在临时目录里跑，绝不碰真实游戏目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.mods = self.root / "Mods"
        self.mods.mkdir()
        self.source = self.root / "PVZHybrid-Watcher.pmod"
        self.source.write_bytes(make_pmod())
        # 本机可能真的有游戏在跑；测试里固定为"未运行"，避免依赖环境。
        # 「运行中不写文件」由专门的用例单独验证。
        self._real_running = mod_install.game_running
        mod_install.game_running = lambda: False
        self.addCleanup(setattr, mod_install, "game_running", self._real_running)

    def test_installs_and_registers(self):
        plan = mod_install.install(self.source, self.mods)
        self.assertEqual(plan.state, "need_restart")
        self.assertTrue(plan.changed)
        self.assertTrue(plan.needs_restart)
        target = self.mods / mod_install.MOD_FILENAME
        self.assertTrue(target.is_file())
        self.assertEqual(mod_install.sha256(target), mod_install.sha256(self.source))
        self.assertTrue(mod_install.is_enabled(self.mods))

    def test_idempotent(self):
        mod_install.install(self.source, self.mods)
        plan = mod_install.install(self.source, self.mods)
        self.assertEqual(plan.state, "ok")
        self.assertFalse(plan.changed, "重复安装不该再改动文件")

    def test_preserves_other_mods(self):
        (self.mods / "enabled_mods.json").write_text(json.dumps({
            "schemaVersion": 2,
            "enabledIds": ["another-mod"],
            "preferredOrder": ["another-mod"],
        }), encoding="utf-8")
        mod_install.install(self.source, self.mods)
        data = json.loads((self.mods / "enabled_mods.json").read_text(encoding="utf-8"))
        self.assertIn("another-mod", data["enabledIds"], "把用户其他 Mod 弄丢了")
        self.assertIn(mod_install.MOD_ID, data["enabledIds"])
        self.assertEqual(data["preferredOrder"], ["another-mod"])

    def test_corrupt_enabled_json_blocks_writes(self):
        """启用记录读不出来时必须停止，不能用空骨架覆盖原文件。"""
        original = "{not json"
        (self.mods / "enabled_mods.json").write_text(original, encoding="utf-8")
        plan = mod_install.install(self.source, self.mods)
        self.assertEqual(plan.state, "error")
        self.assertIn("停止写入", plan.detail)
        # 原文件必须原封不动
        self.assertEqual((self.mods / "enabled_mods.json").read_text(encoding="utf-8"),
                         original, "损坏的启用记录被覆盖了")
        # 也不该往里塞 pmod
        self.assertFalse((self.mods / mod_install.MOD_FILENAME).exists(),
                         "配置读不出来时仍然写了 pmod")

    def test_does_not_overwrite_different_version_by_default(self):
        (self.mods / mod_install.MOD_FILENAME).write_bytes(make_pmod(version="0.2.0"))
        before = (self.mods / mod_install.MOD_FILENAME).read_bytes()
        plan = mod_install.install(self.source, self.mods, allow_replace=False)
        self.assertEqual(plan.state, "ready")
        self.assertIn("未覆盖", plan.detail)
        self.assertEqual((self.mods / mod_install.MOD_FILENAME).read_bytes(), before)

    def test_replace_when_allowed(self):
        (self.mods / mod_install.MOD_FILENAME).write_bytes(make_pmod(version="0.2.0"))
        plan = mod_install.install(self.source, self.mods, allow_replace=True)
        self.assertEqual(plan.state, "need_restart")
        self.assertEqual(mod_install.sha256(self.mods / mod_install.MOD_FILENAME),
                         mod_install.sha256(self.source))

    def test_same_id_different_filename_is_not_duplicated(self):
        """已存在同 ID 但不同文件名的 Mod 时，不能再装第二份。"""
        other = self.mods / "PVZHybridDGHub-PlantDamage.pmod"
        other.write_bytes(make_pmod())              # 同 ID，不同文件名
        plan = mod_install.install(self.source, self.mods)
        pmods = sorted(p.name for p in self.mods.glob("*.pmod"))
        self.assertEqual(pmods, ["PVZHybridDGHub-PlantDamage.pmod"],
                         f"出现了重复的 Mod 文件: {pmods}")
        # 文件没动，但启用记录是新增的，所以仍会提示重启
        self.assertIn(plan.state, ("ok", "need_restart"))
        self.assertTrue(plan.needs_restart)
        self.assertTrue(mod_install.is_enabled(self.mods))

    def test_foreign_file_with_same_name_is_left_alone(self):
        """同名文件若不是本 Mod（ID 不符），不要覆盖它。"""
        target = self.mods / mod_install.MOD_FILENAME
        target.write_bytes(make_pmod(mod_id="someone-elses-mod"))
        before = target.read_bytes()
        plan = mod_install.install(self.source, self.mods)
        self.assertEqual(plan.state, "ready")
        self.assertEqual(target.read_bytes(), before, "覆盖了别人的同名 Mod")

    def test_installed_by_id_ignores_broken_archives(self):
        (self.mods / "broken.pmod").write_bytes(b"not a zip at all")
        (self.mods / "good.pmod").write_bytes(make_pmod())
        found = mod_install.installed_by_id(self.mods)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "good.pmod")

    def test_missing_source(self):
        plan = mod_install.install(self.root / "nope.pmod", self.mods)
        self.assertEqual(plan.state, "no_source")

    def test_missing_mods_dir(self):
        plan = mod_install.install(self.source, self.root / "nodir")
        self.assertEqual(plan.state, "error")
        self.assertIn("未找到游戏目录", plan.detail)

    def test_inspect_is_read_only(self):
        before = sorted(p.name for p in self.mods.iterdir())
        plan = mod_install.inspect(self.source, self.mods)
        self.assertEqual(plan.state, "ready")
        self.assertEqual(sorted(p.name for p in self.mods.iterdir()), before,
                         "inspect 不该写入任何文件")

    def test_inspect_reports_installed(self):
        mod_install.install(self.source, self.mods)
        plan = mod_install.inspect(self.source, self.mods)
        self.assertEqual(plan.state, "ok")

    def test_game_running_blocks_writes(self):
        """游戏运行中不写任何文件，只返回需要重启。"""
        mod_install.game_running = lambda: True
        plan = mod_install.install(self.source, self.mods)
        self.assertEqual(plan.state, "game_running")
        self.assertTrue(plan.needs_restart)
        self.assertEqual(list(self.mods.iterdir()), [], "游戏运行中仍然写了文件")

    def test_game_process_hints_cover_actual_name(self):
        # 本机实测进程名为「植物大战僵尸杂交版发布版0.32.1.Csharp」
        actual = "植物大战僵尸杂交版发布版0.32.1.Csharp"
        self.assertTrue(any(h in actual for h in mod_install.GAME_PROCESS_HINTS))

    def test_mods_dir_derives_from_appdata(self):
        path = mod_install.game_mods_dir(Path(r"C:\Users\X\AppData\Roaming"))
        self.assertEqual(path.name, "Mods")
        self.assertIn("app_userdata", str(path))
        self.assertIn(mod_install.GAME_FOLDER, str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
