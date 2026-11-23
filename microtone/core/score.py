"""谱面事件模型与微型 DSL。

谱面只描述“音阶级数 + 时间”，不绑定具体调律；
同一谱面交给不同 TuningSystem 渲染即可做调律对比。

DSF 示例（每行一个事件，# 开头为注释）::

    bpm 120
    tuning 12-TET            # 仅记录默认调律名，渲染时仍可替换
    policy hold_ratio        # 已发声声音在调律切换后的续接策略
    note 0 1.0               # 级数 0，1 拍
    note 2 0.5 vel 0.8
    chord 0,4,7 2.0          # 同时发声的三个级数
    gliss 0 3 2.0 steps 8    # 从级数 0 滑到级数 3，2 拍，内部 8 个微音级
    rest 0.5

续接策略（policy）语义见 renderer 模块：
* hold_freq    : 已发声音保持原频率（固定参考音）
* hold_ratio   : 已发声音按新调律重新计算，保持它相对参考级的音程比
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NoteEvent:
    time: float          # 起始时间，秒
    duration: float      # 时长，秒
    degree: int          # 音阶级数（任意整数，可负、可跨周期）
    velocity: float = 1.0


@dataclass
class GlissEvent:
    time: float
    duration: float
    degree_from: int
    degree_to: int
    steps: int = 12      # 滑音内部离散微音级数量
    velocity: float = 1.0


@dataclass
class ChordEvent:
    time: float
    duration: float
    degrees: list[int]
    velocity: float = 1.0


@dataclass
class RestEvent:
    time: float
    duration: float


@dataclass
class Score:
    bpm: float = 120.0
    default_tuning: str = "12-TET"
    policy: str = "hold_ratio"
    events: list = field(default_factory=list)

    @property
    def beat_seconds(self) -> float:
        return 60.0 / self.bpm

    @property
    def length_seconds(self) -> float:
        end = 0.0
        for ev in self.events:
            end = max(end, ev.time + ev.duration)
        return end

    def notes(self) -> list:
        """展开为统一的 (time, duration, degree, velocity) 发声列表，
        和弦展开成多个同时发声的音。滑音保留为 GlissEvent 单独处理。"""
        out = []
        for ev in self.events:
            if isinstance(ev, NoteEvent):
                out.append((ev.time, ev.duration, ev.degree, ev.velocity))
            elif isinstance(ev, ChordEvent):
                for d in ev.degrees:
                    out.append((ev.time, ev.duration, d, ev.velocity))
        return out


def parse_score(text: str) -> Score:
    score = Score()
    cursor_beats = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        kind = parts[0]
        if kind == "bpm":
            score.bpm = float(parts[1])
            continue
        if kind == "tuning":
            score.default_tuning = parts[1]
            continue
        if kind == "policy":
            score.policy = parts[1]
            continue

        if kind == "note":
            degree = int(parts[1])
            beats = float(parts[2])
            vel = _parse_vel(parts)
            score.events.append(
                NoteEvent(cursor_beats * score.beat_seconds,
                          beats * score.beat_seconds, degree, vel)
            )
            cursor_beats += beats
        elif kind == "rest":
            beats = float(parts[1])
            score.events.append(
                RestEvent(cursor_beats * score.beat_seconds,
                          beats * score.beat_seconds)
            )
            cursor_beats += beats
        elif kind == "chord":
            degrees = [int(x) for x in parts[1].split(",") if x != ""]
            beats = float(parts[2])
            vel = _parse_vel(parts)
            score.events.append(
                ChordEvent(cursor_beats * score.beat_seconds,
                           beats * score.beat_seconds, degrees, vel)
            )
            cursor_beats += beats
        elif kind == "gliss":
            d_from = int(parts[1])
            d_to = int(parts[2])
            beats = float(parts[3])
            steps = 12
            if "steps" in parts:
                steps = int(parts[parts.index("steps") + 1])
            vel = _parse_vel(parts)
            score.events.append(
                GlissEvent(cursor_beats * score.beat_seconds,
                           beats * score.beat_seconds, d_from, d_to,
                           steps, vel)
            )
            cursor_beats += beats
        else:
            raise ValueError(f"无法识别的谱面指令: {kind} ({raw})")
    return score


def _parse_vel(parts: list[str]) -> float:
    if "vel" in parts:
        return max(0.0, min(1.0, float(parts[parts.index("vel") + 1])))
    return 1.0
