"""杂交版伤害反馈 · 纯状态机。

只做决策，不做任何 I/O：不读文件、不发网络、不碰设备。
输入是已经过 observer 校验的伤害记录与时钟；输出是待发消息。

作者：TianXianbaobao
本插件为开源项目，请勿倒卖。禁止商用。
原项目地址以发布页为准；如为付费购买所得，请申请退款并举报。
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

# ---- 输出节流 --------------------------------------------------------
COOLDOWN = 5.0              # 经典模式两次波形的最小间隔
MAX_SAMPLE_AGE = 1.0        # 经典模式接受的记录最大时延
WAVE_SECONDS = 0.35         # 经典模式每段波形时长

# ---- 持续节奏（SDK 1.2 状态波形）------------------------------------
HOLD_SECONDS = 2.0          # 每段状态波形时长
REFRESH_AT = 0.7            # 剩余不足该值时无缝续播
STOP_AFTER = 0.75           # 停咬超过该值即结束状态波形
MAX_AGE = 0.45              # 节奏模式接受的记录最大时延
MAX_GAP = 0.75              # 同株相邻记录允许的最大间隔
STATE_ID = "pvz-hybrid-rhythm"

# ---- 默认值 ----------------------------------------------------------
# 只在宿主下发的配置里**缺少** preset 键时启用。
# 若用户显式把 preset 设成空值（清空了下拉框），尊重其选择，不偷偷改回来 ——
# 那种情况下插件停在"未就绪"是用户可以看见并纠正的。
DEFAULT_PRESET = "呼吸"

# ---- 强度反馈 --------------------------------------------------------
DEFAULT_DELTA_PLANT = 10
DEFAULT_DELTA_ZOMBIE = 5
DEFAULT_DURATION = 3.0
MIN_DURATION = 0.1
MAX_DURATION = 300.0
DEFAULT_CAP = 50
MIN_CAP = 0
MAX_CAP = 100
MODES = ("rollback", "permanent")

# ---- 运行连续性 ------------------------------------------------------
CONTINUITY_GAP = 180.0      # 单次轮询间隔超过此值视为休眠/长时间阻塞
DRIFT_WINDOW = 30.0         # 墙钟漂移累计窗口
DRIFT_LIMIT = 5.0           # 窗口内允许的最大漂移

# ---- 事件定义 --------------------------------------------------------
PLANT = "plant_damage"
ZOMBIE = "zombie_damage"
EVENTS = (PLANT, ZOMBIE)
EVENT_LABELS = {PLANT: "植物受伤", ZOMBIE: "僵尸受伤"}
# (配置键, 记录字段, 显示名)
COMPONENTS = (("notify_life", "life_lost", "生命"),
              ("notify_armor", "armor_lost", "护甲"),
              ("notify_shield", "shield_absorbed", "格盾"))
CHANNELS = {"a": "A", "b": "B", "both": "A+B"}


# ---- 输入归一化 ------------------------------------------------------
def channel(value):
    """只接受 SDK 定义的三值，绝不猜测或回退到某个在用通道。"""
    return value if type(value) is str and value in CHANNELS else None


def preset(value):
    """固定预设名。随机/默认一类拒绝 —— 反馈必须可预期。"""
    if type(value) is not str or not (0 < len(value) <= 96) or value != value.strip():
        return None
    if not all(ch.isprintable() for ch in value):
        return None
    key = unicodedata.normalize("NFKC", value).casefold()
    if (any(k in key for k in ("random", "shuffle", "随机", "隨機"))
            or key in ("*", "none", "null", "default", "auto", "automatic",
                       "默认", "預設", "自动")):
        return None
    return value


def _finite(value):
    return type(value) in (int, float) and math.isfinite(float(value))


def delta(value):
    """强度增量，收敛到宿主 delta_pct 的合法整数区间。"""
    if not _finite(value):
        return 0
    return max(-100, min(100, int(round(float(value)))))


def duration(value):
    if not _finite(value):
        return DEFAULT_DURATION
    return max(MIN_DURATION, min(MAX_DURATION, float(value)))


def mode(value):
    return value if type(value) is str and value in MODES else "rollback"


def cap(value):
    """永久累积上限。0 表示禁止累积。"""
    if not _finite(value):
        return DEFAULT_CAP
    return max(MIN_CAP, min(MAX_CAP, int(round(float(value)))))


# ---- 出站消息 --------------------------------------------------------
@dataclass(frozen=True)
class Message:
    """三条出站路径之一：波形 / 强度 / 状态波形。

    字段集固定，wire() 负责最后的合法性检查并生成宿主能接受的 JSON。
    """
    kind: str                   # "wave" | "strength" | "state"
    preset: str
    channel: str
    event: str = PLANT
    rhythm: bool = False
    active: bool = True
    delta: int = 0
    strength_mode: str = "rollback"
    # wave 用它作为时长；None 表示按经典单次波形（WAVE_SECONDS）。
    # strength 用它作为回滚时长；None 表示用 DEFAULT_DURATION。
    seconds: float | None = None

    def wire(self) -> dict:
        if channel(self.channel) is None:
            raise ValueError("invalid channel")
        if self.kind == "state":
            if not self.active:
                # 结束消息不带 preset/channel，宿主沿用开始时的路由。
                return {"op": "pulse_state", "state_id": STATE_ID, "active": False}
            if preset(self.preset) is None:
                raise ValueError("invalid preset")
            return {"op": "pulse_state", "state_id": STATE_ID, "active": True,
                    "preset": self.preset, "channel": self.channel,
                    "duration_s": HOLD_SECONDS}
        if self.kind == "strength":
            amount = delta(self.delta)
            if amount == 0:
                raise ValueError("zero strength delta")
            label = EVENT_LABELS.get(self.event, "受伤")
            return {"op": "trigger", "action": "strength",
                    "delta_pct": amount, "strength_mode": mode(self.strength_mode),
                    "duration_s": duration(self.seconds),
                    "channel": self.channel, "label": f"{label} {amount:+d}%"}
        if self.kind == "wave":
            if preset(self.preset) is None or self.event not in EVENTS:
                raise ValueError("invalid preset or event")
            # 时长可覆盖：旧 SDK 上没有 pulse_state，只能靠足够长的单次波形
            # 来覆盖一个续播周期，否则"持续"就无从谈起。
            seconds = self.seconds if self.seconds and self.seconds > 0 else WAVE_SECONDS
            return {"op": "trigger", "action": "waveform", "preset": self.preset,
                    "channel": self.channel, "duration_s": seconds,
                    "label": EVENT_LABELS[self.event]}
        raise ValueError("unknown message kind")


class Gate:
    """会话准入 + 节流 + 状态波形生命周期 + 强度封顶。"""

    def __init__(self):
        # 配置
        self.configured = False
        self.enabled = True
        self.filters = {key: True for key, _, _ in COMPONENTS}
        self.filters["notify_zombie"] = False
        self.baseline_zero = True
        self.preset = None
        self.mode = "classic"
        self.channel = "a"
        self.state_waves = True     # 宿主 SDK >= 1.2 时为 True
        # 强度
        self.strength_on = False
        self.delta_plant = DEFAULT_DELTA_PLANT
        self.delta_zombie = DEFAULT_DELTA_ZOMBIE
        self.strength_mode = "rollback"
        self.strength_seconds = DEFAULT_DURATION
        self.strength_cap = DEFAULT_CAP
        self.accum = 0              # 本次运行已请求的净增量（进程内）
        self.baseline_value = 0     # 宿主持久化的基线（idle_strength 的回传值）
        self.permanent_exhausted = False  # 启动时判定：上次运行可能已用尽额度
        self.strength_sent = 0
        self.strength_total = 0
        self.clamped = 0            # 因触顶被截断
        self.blocked = 0            # 因触顶被拦下
        self.blocked_restart = 0    # 因重启后判定已用尽而拦下
        # 会话
        self.armed = False
        self.device = None
        self.device_seen = False
        self.generation = 0
        self.reason = "等待配置与 V3 设备"
        self.count = 0
        self.game_session = None
        # 时序
        self.started_wall = 0.0
        self.prev_mono = None
        self.drift_wall = self.drift_mono = 0.0
        self.last_wave = float("-inf")
        # 状态波形
        self.state_on = False
        self.state_until = float("-inf")
        self.bout_target = None
        self.last_ts = self.last_mono = float("-inf")
        # 需按顺序补发的消息（停止 / 续播 / 强度）
        self._outbox: list[Message] = []

    # ---- 出站队列 ----------------------------------------------------
    def drain(self) -> list[Message]:
        """取出待发消息。调用方先发 drain()，再发 admit() 的返回值。"""
        pending, self._outbox = self._outbox, []
        return pending

    @property
    def pending(self) -> bool:
        return bool(self._outbox)

    def _send_later(self, message: Message | None):
        if message is not None:
            self._outbox.append(message)

    # ---- 状态波形 ----------------------------------------------------
    def _stop_state(self):
        if not self.state_on:
            return
        self.state_on = False
        self.state_until = float("-inf")
        if self.state_waves:
            # SDK < 1.2 时宿主不认识 pulse_state，也从未建立过状态波形，
            # 此时发停止消息只会让宿主把整条消息判为格式错误。
            self._outbox.append(Message("state", self.preset, self.channel, active=False))

    # ---- 强度 --------------------------------------------------------
    def _delta_for(self, event: str) -> int:
        return self.delta_zombie if event == ZOMBIE else self.delta_plant

    def _strength_message(self, event: str) -> Message | None:
        """强度请求；永久模式受上限约束，触发顶则返回 None。

        上限有三道判断，从强到弱：
          1. 启动时判定已用尽（permanent_exhausted）—— 挡住跨进程重复发放
          2. 本次运行累计已达上限（accum）—— 挡住同进程内的越界
          3. 单次增量超出剩余额度则截断 —— 保证最后一次正好封顶

        关于负增量：额度只被"向上加"消耗，**不会被减弱操作释放**。

        原因：插件不知道宿主是否真的应用了某个请求。若把负增量从 accum 里
        扣掉，等于假设"减弱已生效"，而这未经确认。一旦宿主没应用（设备忙、
        通道被覆盖、请求被丢弃），插件就会凭空多出一份额度并发出更强的请求。

        实测反例：cap=0 时连发三次 -20%，accum 变成 -60，remaining 算成 60，
        随后放出了 +40 的正增量 —— 上限被完全绕过。

        因此 accum 只累计正向增量，作为"已占用额度"的保守上界。
        """
        if not self.strength_on:
            return None
        amount = self._delta_for(event)
        if amount == 0:
            return None
        if self.strength_mode == "permanent":
            # 第一道：重启后基线已达上限，不再发放正向增量。
            # 负增量（减弱）不受限 —— 上限只管往上加。
            if self.permanent_exhausted and amount > 0:
                self.blocked_restart += 1
                return None
            # 第二道：剩余额度必须扣掉宿主基线已占用的部分。
            remaining = self.strength_cap - self.baseline_value - self.accum
            if remaining <= 0 and amount > 0:
                self.blocked += 1
                return None
            # 第三道：只补到正好封顶，不冲过头。
            if amount > remaining:
                amount = remaining
                self.clamped += 1
            # 只累计正向增量：负增量不释放额度（见上方说明）。
            if amount > 0:
                self.accum += amount
        self.strength_sent += 1
        self.strength_total += amount
        return Message("strength", self.preset, self.channel, event=event, delta=amount,
                       strength_mode=self.strength_mode, seconds=self.strength_seconds)

    def _queue_strength(self, event: str):
        self._send_later(self._strength_message(event))

    # ---- 配置 --------------------------------------------------------
    def config(self, data):
        self.disarm("已读取配置；等待插件启用")
        self.configured = isinstance(data, dict)
        if not self.configured:
            self.preset = None
            self.channel = None
            return
        self.enabled = data.get("show_damage", True) is True
        self.filters = {key: data.get(key, True) is True for key, _, _ in COMPONENTS}
        self.filters["notify_zombie"] = data.get("notify_zombie", False) is True
        self.preset = preset(data.get("preset", DEFAULT_PRESET))
        # 旧版本没有 channel 字段，按 A 处理（历史上只会往 A 发）。
        self.channel = channel(data.get("channel", "a")) or None
        self.mode = "rhythm" if data.get("feedback_mode") == "rhythm" else "classic"
        self.baseline_zero = (
            _finite(data.get("idle_strength", 0)) and data.get("idle_strength", 0) == 0)
        self.strength_on = data.get("strength_enable", False) is True
        self.delta_plant = delta(data.get("strength_plant", DEFAULT_DELTA_PLANT))
        self.delta_zombie = delta(data.get("strength_zombie", DEFAULT_DELTA_ZOMBIE))
        self.strength_mode = mode(data.get("strength_mode"))
        self.strength_seconds = duration(data.get("strength_duration", DEFAULT_DURATION))
        self.strength_cap = cap(data.get("strength_cap", DEFAULT_CAP))
        self._judge_baseline(data.get("idle_strength", 0))

    def _judge_baseline(self, raw):
        """依据宿主回传的 idle_strength 判断永久额度还剩多少。

        permanent 请求会改写并持久化这个值，插件重启后 accum 归零，
        若仍按 accum 计算剩余额度就会重新发放一份完整额度。

        剩余额度 = 上限 - 基线 - 本次运行已加。基线本身占用的部分必须扣掉，
        否则基线 45 / 上限 50 时仍会放出 50 的额度（实测会冲到 95）。

        插件无法区分"上次加满"与"用户手动设了高位"（宿主的增量语义不提供
        来源信息），因此对基线一律按"已占用"处理 —— 宁可少加，也不越界。
        用户手动归零 idle_strength 或改用 rollback 后即可恢复。
        """
        self.baseline_value = (int(round(float(raw)))
                               if _finite(raw) else 0)
        if self.strength_mode != "permanent" or self.baseline_value <= 0:
            self.permanent_exhausted = False
            return
        # 基线已到或超过上限：没有任何剩余额度。
        self.permanent_exhausted = self.baseline_value >= self.strength_cap

    def change(self, key, value, wall, mono) -> bool:
        """返回 True 表示本次变更开启了一个新会话。"""
        if key == "arm":
            return False                    # 0.5.0 起废弃的旧键
        self.tick(wall, mono)
        if key == "show_damage":
            self.enabled = value is True
            if not self.enabled:
                self.disarm("总开关已关闭")
                return False
            return self.begin(wall, mono)
        self.disarm("配置已更改；需要重新启用")
        if key == "preset":
            self.preset = preset(value)
        elif key == "feedback_mode":
            self.mode = "rhythm" if value == "rhythm" else "classic"
        elif key == "channel":
            self.channel = channel(value)
        elif key in self.filters:
            self.filters[key] = value is True
        elif key == "idle_strength":
            self.baseline_zero = _finite(value) and value == 0
            # 宿主回传的基线变了，重新判断永久额度是否已用尽。
            self._judge_baseline(value)
        elif key == "strength_enable":
            self.strength_on = value is True
        elif key == "strength_plant":
            self.delta_plant = delta(value)
        elif key == "strength_zombie":
            self.delta_zombie = delta(value)
        elif key == "strength_mode":
            self.strength_mode = mode(value)
            # 切换模式后重新判断：permanent 需要基线检查，rollback 不需要。
            self._judge_baseline(self.baseline_value)
        elif key == "strength_duration":
            self.strength_seconds = duration(value)
        elif key == "strength_cap":
            self.strength_cap = cap(value)
            # 上限变了，用当前基线重新判断是否已用尽。
            self._judge_baseline(self.baseline_value)
        return False

    # ---- 设备 --------------------------------------------------------
    def device_info(self, msg):
        a, b = msg.get("max_strength_a"), msg.get("max_strength_b")
        ok = (msg.get("connected") is True and msg.get("device_type") == "v3"
              and type(a) is int and 0 <= a <= 200
              and type(b) is int and 0 <= b <= 200)
        fingerprint = None
        if ok:
            # SDK v1 可能不报身份字段；把能拿到的都纳入指纹，
            # 这样热插拔换设备不会被误当成同一台。
            fingerprint = (a, b, *(str(msg.get(k, ""))[:512] for k in (
                "id", "target_id", "device_id", "profile_id", "client_id",
                "target", "profile")))
        if fingerprint != self.device or fingerprint is None:
            if fingerprint is None:
                self.disarm("未连接满足条件的 V3 设备")
            elif self.armed and self.device is not None and fingerprint[2:] != self.device[2:]:
                self.disarm("目标或设备身份变化；需要重新启用")
            elif self.armed and not self._route_ok(fingerprint):
                self.disarm("所选通道上限不可用；需要重新启用")
        self.device = fingerprint
        self.device_seen = True

    def invalidate(self, why):
        self.device = None
        self.disarm(why)

    def _route_ok(self, fingerprint) -> bool:
        a, b = fingerprint[0], fingerprint[1]
        return ((self.channel == "a" and a > 0) or (self.channel == "b" and b > 0)
                or (self.channel == "both" and a > 0 and b > 0))

    # ---- 会话 --------------------------------------------------------
    @property
    def route_ready(self) -> bool:
        return self.device is not None and self._route_ok(self.device)

    @property
    def channel_label(self) -> str:
        return CHANNELS.get(self.channel, "无有效通道")

    @property
    def can_begin(self) -> bool:
        return (not self.armed and self.configured and self.enabled and self.route_ready
                and self._baseline_ok and bool(self.preset) and any(self.filters.values()))

    @property
    def _baseline_ok(self) -> bool:
        """插件自己的强度基线检查。

        强度反馈关闭时要求基线为 0 —— 那种情况下插件本来就不该有强度层，
        非零基线说明有别的东西动过它，不接管。
        强度反馈开启时基线非零是**预期状态**（永久累积会把基线推高），
        此时必须放行，否则插件会把自己永久锁死。
        """
        return self.strength_on or self.baseline_zero

    def begin(self, wall, mono) -> bool:
        if self.armed:
            return False
        if not any(self.filters.values()):
            self.disarm("未勾选任何触发事件")
            return False
        if not (self.configured and self.route_ready and self.enabled
                and self._baseline_ok and self.preset):
            self.disarm("条件不足：需总开关、V3 设备、可用通道、固定预设与可接管的强度基线")
            return False
        self.prev_mono = mono
        self.drift_wall, self.drift_mono = wall, mono
        self.generation += 1
        self.started_wall = wall
        self.armed = True
        self.count = 0
        self.game_session = None
        self.bout_target = None
        self.state_on = False
        self.state_until = float("-inf")
        self.reason = f"已启用（{self.channel_label} 固定预设）"
        return True

    def disarm(self, why):
        self._stop_state()
        self.generation += 1
        self.armed = False
        self.reason = why
        self.game_session = None
        self.bout_target = None

    def tick(self, wall, mono):
        """连续性守卫 + 状态波形存活判定。"""
        if self.armed:
            if self.prev_mono is not None:
                step = mono - self.prev_mono
                if step < 0 or step > CONTINUITY_GAP:
                    self.disarm("运行连续性中断（休眠或长时间阻塞）")
            if self.armed:
                if mono < self.drift_mono:
                    self.drift_wall, self.drift_mono = wall, mono
                elif mono - self.drift_mono >= DRIFT_WINDOW:
                    if abs((wall - self.drift_wall) - (mono - self.drift_mono)) > DRIFT_LIMIT:
                        self.disarm("系统时钟被大幅调整")
                    else:
                        self.drift_wall, self.drift_mono = wall, mono
        if self.state_on and (mono > self.state_until
                              or mono - self.last_mono > STOP_AFTER):
            self._stop_state()
        self.prev_mono = mono

    # ---- 准入 --------------------------------------------------------
    def matches(self, record) -> bool:
        """只看筛选；schema/新鲜度/重放校验在 observer。"""
        if type(record) is not dict:
            return False
        event = record.get("event")
        if event not in EVENTS:
            return False
        zombie = event == ZOMBIE
        if zombie and not self.filters["notify_zombie"]:
            return False
        for key, field, _ in COMPONENTS:
            loss = record.get(field, 0)
            if (zombie or self.filters[key]) and _finite(loss) and 0 < loss <= 1e15:
                return True
        return False

    def _open_state(self, event: str) -> Message:
        """打开一段"持续输出"。

        SDK >= 1.2：用 pulse_state，宿主循环播放预设，天然连续。
        SDK <  1.2：没有状态波形可用，只能发一段"足够长"的单次波形，
        让它在下一个续播点之前一直响着 —— 时长取 HOLD_SECONDS，
        和状态波形保持一致的时间轴，这样两边的续播节奏完全一样。
        """
        if not self.state_waves:
            self.state_on = True                    # 仍然视为"输出中"，供续播判断
            self.state_until = self.last_mono + HOLD_SECONDS
            self.count += 1
            return Message("wave", self.preset, self.channel, event=event,
                           rhythm=True, seconds=HOLD_SECONDS)
        self.state_on = True
        self.state_until = self.last_mono + HOLD_SECONDS
        self.count += 1
        return Message("state", self.preset, self.channel, event=event, active=True)

    def _restart_state(self, event: str) -> Message | None:
        """先关旧状态波形，再开新的。

        需要先关时，两条消息必须严格 off → on：因为调用方只能从返回值拿到
        一条消息，所以把两条都放进 outbox 并返回 None（只 drain）。
        不需要先关时（首次进入）直接返回 on，由调用方立即发送。
        """
        if not self.state_on:
            return self._open_state(event)
        self._stop_state()                          # off 进 outbox
        self._send_later(self._open_state(event))   # on 紧随其后
        return None

    def _admit_rhythm(self, target, ts, mono, event) -> Message | None:
        """同株持续承伤时维持状态波形；换株/断档时先停后开。"""
        if self.bout_target != target:
            # 换目标：只在旧那株确实还在承伤时才让位（不抢输出）。
            # 旧那株已经停了（超过 MAX_GAP）就立刻接手。
            if self.state_on and mono - self.last_mono <= MAX_GAP:
                return None
            self._seed(target, ts, mono)
            self._queue_strength(event)
            return self._restart_state(event)
        if ts <= self.last_ts:
            return None                         # 同一生产时间的重复记录不延长
        if ts - self.last_ts > MAX_GAP or mono - self.last_mono > MAX_GAP:
            # 断档后重新密集出现：重新起一段，不播间断期间
            self._seed(target, ts, mono)
            self._queue_strength(event)
            return self._restart_state(event)
        self.last_ts, self.last_mono = ts, mono
        if not self.state_on:
            self._queue_strength(event)
            return self._restart_state(event)
        if mono >= self.state_until - REFRESH_AT:
            # 同一 state_id 重复 active=true 不会重播，必须结束再开始。
            # 强度不随续播重复叠加 —— 否则永久模式会在连续啃咬下失控。
            return self._restart_state(event)
        return None

    def _seed(self, target, ts, mono):
        self.bout_target = target
        self.last_ts = self.last_mono = ts
        self.last_mono = mono

    def admit(self, record, generation, wall, mono) -> Message | None:
        """仅由 observer 校验并推进序号水位后调用。"""
        self.tick(wall, mono)
        if (not self.armed or generation != self.generation
                or not self.route_ready or not self.matches(record)):
            return None
        ts = record["ts"]
        rhythm = self.mode == "rhythm"
        window = MAX_AGE if rhythm else MAX_SAMPLE_AGE
        if not (self.started_wall < ts <= wall and wall - ts <= window):
            return None
        sid = record["session"]
        if self.game_session is not None and self.game_session != sid:
            # 游戏重开不影响启用状态，只重置本局节奏。
            self._stop_state()
            self.game_session = sid
            self.bout_target = None
            self.last_wave = float("-inf")
            self.last_ts = self.last_mono = float("-inf")
            self.generation += 1
            return None
        self.game_session = sid
        event = record["event"]
        if self.mode == "rhythm":
            message = self._admit_rhythm((event, str(record["target"])), ts, mono, event)
            if message is not None:
                self.last_wave = mono
            return message
        if mono - self.last_wave < COOLDOWN:
            return None
        self.last_wave = mono
        self.count += 1
        self._queue_strength(event)
        return Message("wave", self.preset, self.channel, event=event)

