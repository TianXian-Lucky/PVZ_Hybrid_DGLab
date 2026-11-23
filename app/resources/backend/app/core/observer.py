"""读取游戏端写入的 JSONL 事件流。

只读、只追新增。历史记录一律跳过；轮转/重建时重新锚定到末尾，
避免把归档前的旧伤害重新播一遍。

作者：TianXianbaobao
本插件为开源项目，请勿倒卖。禁止商用。
原项目地址以发布页为准；如为付费购买所得，请申请退款并举报。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

MAX_LINE = 4096          # 单行上限，超过即视为损坏
MAX_READ = 65536         # 单次最多读取
MAX_BACKLOG = 131072     # 积压过多时跳到最近一段
MAX_AGE_S = 6            # 记录允许的最大时延
FUTURE_SKEW_S = 2        # 允许的未来时钟偏差
QUIET_S = 2.0            # 同株卡片抑制间隔
BURST_CARDS = 4          # 每秒最多出的卡片数
SESSION_SLOTS = 32       # 同时跟踪的游戏会话数
TARGET_SLOTS = 1024      # 同时跟踪的目标数

EVENTS = ("plant_damage", "zombie_damage")
EVENT_LABELS = {"plant_damage": "植物受伤", "zombie_damage": "僵尸受伤"}
SESSION_RE = re.compile(r"[a-zA-Z0-9_-]+")


class Tail:
    """跟随追加/轮转，轮询之间不持有文件句柄。"""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.ident = None
        self.anchor = b""
        self.partial = b""
        self.discard = False
        self.state = "missing"
        self.initial = True
        self.read()

    def _split(self, data: bytes) -> list[bytes]:
        lines = []
        pieces = data.split(b"\n")
        for i, piece in enumerate(pieces):
            complete = i < len(pieces) - 1
            if not self.discard:
                if len(self.partial) + len(piece) > MAX_LINE:
                    self.discard = True
                    self.partial = b""
                else:
                    self.partial += piece
            if complete:
                if self.partial and not self.discard:
                    lines.append(self.partial)
                self.partial = b""
                self.discard = False
        return lines

    def _seek(self, handle, position: int):
        self.partial = b""
        self.offset = position
        handle.seek(max(0, position - 32))
        self.anchor = handle.read(min(32, position))
        # 锚点不是行首说明我们落在了半行上，丢弃这段残片。
        self.discard = bool(self.anchor and not self.anchor.endswith(b"\n"))

    def read(self) -> list[bytes]:
        try:
            with self.path.open("rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self.state = "unreadable"
                    return []
                self.state = "present"
                ident = (info.st_dev, info.st_ino)
                changed = ident != self.ident or info.st_size < self.offset
                if not changed and self.anchor:
                    handle.seek(self.offset - len(self.anchor))
                    changed = handle.read(len(self.anchor)) != self.anchor
                if self.initial or changed:
                    self._seek(handle, info.st_size if self.initial else 0)
                self.ident = ident
                self.initial = False
                if info.st_size - self.offset > MAX_BACKLOG:
                    self._seek(handle, info.st_size - MAX_READ)
                handle.seek(self.offset)
                data = handle.read(MAX_READ)
                self.offset += len(data)
                self.anchor = (self.anchor + data)[-32:]
                return self._split(data) if data else []
        except FileNotFoundError:
            self.state = "missing"
            self.initial = False    # 之后新建的文件属于新输入
            self.ident = None
        except OSError:
            self.state = "unreadable"
        return []


def parse(line: bytes) -> dict | None:
    if not isinstance(line, bytes) or len(line) > MAX_LINE:
        return None
    try:
        value = json.loads(line.decode("utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _text(value, limit: int) -> str | None:
    if type(value) is not str or not 1 <= len(value) <= limit:
        return None
    return value if value.isprintable() else None


def _number(value) -> bool:
    return type(value) in (int, float)


class Policy:
    """校验、去重、计数，并决定要不要出卡片。

    只读展示层：绝不发送波形或强度指令，那是 gate 的职责。
    """

    def __init__(self):
        self.master = True          # 总开关：控制是否出卡片
        self.quiet = QUIET_S
        self.sessions: dict[str, tuple[int, float]] = {}
        self.targets: dict[tuple, float] = {}
        self.burst: list[float] = []
        self.accepted = 0
        self.eligible = 0
        self.displayed = 0
        self.received = dict.fromkeys(EVENTS, 0)
        self.hit = dict.fromkeys(EVENTS, 0)
        self.total_life = 0.0
        self.total_armor = 0.0
        self.total_shield = 0.0

    def observe(self, record, now, mono, gate):
        """校验一条记录，返回 (卡片或 None, 是否命中筛选)。

        「命中」定义：通过校验、序号前进、且满足当前勾选。
        被筛选掉的记录仍然计入 received 与各自总量，但不参与波形。
        卡片还需要总开关打开 —— 关掉总开关时只统计不出卡。
        """
        matches = gate.matches(record)
        before = self.accepted
        card = self.card(record, now, mono, matches and self.master)
        accepted_now = self.accepted > before
        eligible = matches and accepted_now
        if eligible:
            self.eligible += 1
            self.hit[record["event"]] += 1
        return (card if accepted_now else None), eligible

    def configure(self, key, value):
        if key == "show_damage" and type(value) is bool:
            self.master = value
        elif key == "quiet_s" and _number(value) and 0 <= value <= 15:
            self.quiet = float(value)

    def card(self, record, now, mono, allow_display=True):
        """通过校验则返回一张卡片，否则 None。通过即代表记录被接受。"""
        if not isinstance(record, dict) or record.get("schema") != 1:
            return None
        event = record.get("event")
        if type(event) is not str or event not in self.received:
            return None
        ts, seq = record.get("ts"), record.get("seq")
        parts = tuple(record.get(k, 0) for k in ("life_lost", "armor_lost", "shield_absorbed"))
        session = _text(record.get("session"), 96)
        if not (session and SESSION_RE.fullmatch(session)):
            return None
        if (not _number(ts) or not now - MAX_AGE_S <= ts <= now + FUTURE_SKEW_S
                or type(seq) is not int or not 0 <= seq < (1 << 63)
                or not all(_number(x) and 0 <= x <= 1e15 for x in parts)
                or not any(parts)):
            return None
        life, armor, shield = parts
        target = record.get("target")
        if type(target) is int and 0 <= target < (1 << 64):
            target = str(target)
        target = _text(target, 128)
        kind = _text(record.get("target_type"), 128)
        if not (target and kind):
            return None

        # 序号水位：同一会话内必须严格递增，挡住重放与乱序。
        previous = self.sessions.get(session)
        if previous and seq <= previous[0]:
            return None
        if previous is None and len(self.sessions) >= SESSION_SLOTS:
            self.sessions = {k: v for k, v in self.sessions.items() if mono - v[1] < 30}
            if len(self.sessions) >= SESSION_SLOTS:
                return None
        self.sessions[session] = (seq, mono)

        self.accepted += 1
        self.received[event] += 1
        self.total_life += life
        self.total_armor += armor
        self.total_shield += shield
        if not allow_display:
            return None

        # 展示侧抑制：同株静默 + 每秒卡片上限。只影响卡片，不影响计数。
        key = (session, event, target)
        prior = self.targets.get(key)
        if prior is None and len(self.targets) >= TARGET_SLOTS:
            self.targets = {k: v for k, v in self.targets.items() if mono - v < 15}
            if len(self.targets) >= TARGET_SLOTS:
                return None
        self.targets[key] = mono
        if prior is not None and mono - prior < self.quiet:
            return None
        self.burst = [t for t in self.burst if mono - t < 1.0]
        if len(self.burst) >= BURST_CARDS:
            return None
        self.burst.append(mono)
        self.displayed += 1

        uid = hashlib.sha256(f"{session}:{seq}".encode()).hexdigest()[:24]
        details = [f"{kind} #{target}"]
        if life:
            details.append(f"生命 -{life:g} HP")
        if armor:
            details.append(f"护甲 -{armor:g}")
        if shield:
            details.append(f"格盾吸收 {shield:g}")
        return {"op": "event", "label": EVENT_LABELS[event],
                "name": " · ".join(details),
                "duration": 1.5, "event_id": f"pvzd-{uid}"}
