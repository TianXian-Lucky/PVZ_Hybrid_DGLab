"""Offline reader for the shipped, unmodified DG-LAB waveform table."""
import math
import re
from pathlib import Path

def load_presets(root: Path):
    text = (root / 'data' / 'coyote.ts').read_text('utf-8')
    presets = {}
    for label, raw in re.findall(r"cn: '([^']+)',\s*en: '[^']*',\s*\},\s*raw:\s*\[([^\]]*)\]", text):
        frames = re.findall(r"'([0-9a-fA-F]{16})'", raw)
        if not frames or any(max(bytes.fromhex(f)[4:]) > 100 for f in frames):
            raise ValueError("损坏的预设：" + label)
        presets[label] = frames
    if '呼吸' not in presets or len(presets) != 24:
        raise ValueError("缺少官方预设数据")
    return presets

def frame_at(frames, index, gain, samples=4):
    # Intensity bytes only. Never increase frequency, phone channel level or app limit.
    raw = bytes.fromhex(frames[index % len(frames)])
    gains = gain if isinstance(gain,(list,tuple)) else [gain]*4
    amps = [min(100, round(x * (1 + max(-100,min(100,gains[j]))/100))) if j < samples else 0 for j, x in enumerate(raw[4:])]
    return (raw[:4] + bytes(amps)).hex().upper()

def preview(frames):
    amps = [v for f in frames for v in bytes.fromhex(f)[4:]]
    step = max(1, math.ceil(len(amps)/256))
    return amps[::step]
