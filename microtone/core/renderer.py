"""音频渲染与实时声音管理（纯标准库，不依赖 numpy）。

关键不变量：
* 振荡器频率直接来自 TuningSystem.degree_to_freq，全程不经过半音/MIDI 量化；
* 滑音在对数频率轴上逐采样连续移动，微分音级真实可闻；
* 调律表切换时，每个已发声 Voice 按显式策略续接：
  - hold_freq  : 锁定切换瞬间的物理频率（“固定参考音”优先）；
  - hold_ratio : 按新调律重新计算频率（“保持音程比”优先）。
  二者在跨越微分音级时确实冲突，策略必须二选一并记录在渲染产物里。
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from fractions import Fraction

from .score import Score, NoteEvent, ChordEvent, GlissEvent
from .tuning import TuningSystem


@dataclass
class Voice:
    """一个正在发声的声音，由 GUI/实时播放器维护。"""

    degree: int
    freq: float                 # 当前实际振荡频率（Hz）
    velocity: float
    sample_rate: int
    harmonics: tuple = (1.0, 0.35, 0.12)   # 简单可验证的加法合成音色
    phase: float = 0.0
    gliss_to_degree: int | None = None
    gliss_samples_left: int = 0
    gliss_samples_total: int = 0
    gliss_freq_from: float = 0.0
    gliss_freq_to: float = 0.0
    locked_freq: float | None = None       # hold_freq 策略锁定值

    def samples(self, n: int) -> list[float]:
        out = []
        for _ in range(n):
            if self.gliss_samples_left > 0:
                p = 1.0 - self.gliss_samples_left / self.gliss_samples_total
                # 对数频率轴插值 => 音程比保持恒定的滑音
                self.freq = self.gliss_freq_from * (
                    (self.gliss_freq_to / self.gliss_freq_from) ** p
                )
                self.gliss_samples_left -= 1
            self.phase += 2.0 * math.pi * self.freq / self.sample_rate
            v = 0.0
            for h, amp in enumerate(self.harmonics, start=1):
                v += amp * math.sin(h * self.phase) / h
            out.append(v * self.velocity)
        return out

    def begin_gliss(self, target_degree: int, target_freq: float,
                    duration_seconds: float, sample_rate: int):
        self.gliss_to_degree = target_degree
        self.gliss_freq_from = self.freq
        self.gliss_freq_to = target_freq
        self.gliss_samples_total = max(1, int(duration_seconds * sample_rate))
        self.gliss_samples_left = self.gliss_samples_total
        self.locked_freq = None  # 滑音中的声音不允许再被 hold_freq 锁定


class VoiceManager:
    """管理实时按下的琴键，并在调律表变化时决定已发声音如何续接。"""

    def __init__(self, tuning: TuningSystem, sample_rate: int = 44100,
                 policy: str = "hold_ratio",
                 freq_range: tuple[float, float] = (20.0, 20000.0)):
        self.tuning = tuning
        self.sample_rate = sample_rate
        self.policy = policy
        self.freq_range = freq_range
        self.voices: dict[int, Voice] = {}
        self.out_of_range_events: list[dict] = []

    def set_policy(self, policy: str):
        if policy not in ("hold_freq", "hold_ratio"):
            raise ValueError("policy 只能是 hold_freq 或 hold_ratio")
        self.policy = policy

    def _check_range(self, degree: int, freq: float) -> bool:
        lo, hi = self.freq_range
        if not (lo <= freq <= hi):
            self.out_of_range_events.append(
                {"degree": degree, "freq_hz": freq, "action": "muted"}
            )
            return False
        return True

    def note_on(self, degree: int, velocity: float = 1.0) -> Voice | None:
        freq = self.tuning.degree_to_freq(degree)
        if not self._check_range(degree, freq):
            return None
        existing = self.voices.get(degree)
        if existing is not None:
            return existing
        v = Voice(degree=degree, freq=freq, velocity=velocity,
                  sample_rate=self.sample_rate)
        self.voices[degree] = v
        return v

    def note_off(self, degree: int):
        self.voices.pop(degree, None)

    def gliss_to(self, degree: int, target_degree: int,
                 duration_seconds: float) -> bool:
        v = self.voices.get(degree)
        if v is None:
            # 源音未按下时自动起一个声音，保证滑音可独立触发
            v = self.note_on(degree)
            if v is None:
                return False
        target_freq = self.tuning.degree_to_freq(target_degree)
        if not self._check_range(target_degree, target_freq):
            return False
        v.begin_gliss(target_degree, target_freq, duration_seconds,
                      self.sample_rate)
        return True

    def apply_tuning_change(self, new_tuning: TuningSystem) -> list[dict]:
        """调律表变化时，按策略续接所有已发声音，返回每个声音的续接记录。"""
        report = []
        old_tuning = self.tuning
        for degree, v in list(self.voices.items()):
            old_freq = v.freq
            if v.locked_freq is not None:
                new_freq = v.locked_freq
                kind = "hold_freq(locked)"
            elif self.policy == "hold_freq":
                # 固定参考音：物理频率不动，音程比随新表改变
                v.locked_freq = old_freq
                new_freq = old_freq
                kind = "hold_freq"
            else:
                # 保持音程比：用同一级数在新调律里重算，物理频率改变
                new_freq = new_tuning.degree_to_freq(degree)
                kind = "hold_ratio"
                if v.gliss_samples_left > 0:
                    v.gliss_freq_to = new_tuning.degree_to_freq(
                        v.gliss_to_degree)
            report.append(
                {
                    "degree": degree,
                    "policy": kind,
                    "old_freq_hz": old_freq,
                    "new_freq_hz": new_freq,
                    "freq_delta_hz": new_freq - old_freq,
                    "old_tuning": old_tuning.name,
                    "new_tuning": new_tuning.name,
                }
            )
            v.freq = new_freq
        self.tuning = new_tuning
        return report

    def render_block(self, n: int) -> list[float]:
        if not self.voices:
            return [0.0] * n
        mix = [0.0] * n
        for v in list(self.voices.values()):
            block = v.samples(n)
            for i, x in enumerate(block):
                mix[i] += x
        peak = max((abs(x) for x in mix), default=0.0)
        if peak > 1.0:
            mix = [x / peak for x in mix]
        return mix


# --------------------------------------------------------------------------
# 离线渲染：Score + TuningSystem -> WAV
# --------------------------------------------------------------------------

def _adsr_envelope(n: int, sr: int,
                   attack=0.008, decay=0.06, sustain=0.75, release=0.12):
    a = max(1, int(attack * sr))
    d = max(1, int(decay * sr))
    r = max(1, int(release * sr))
    s = max(0, n - a - d - r)
    env = []
    for i in range(n):
        if i < a:
            env.append(i / a)
        elif i < a + d:
            env.append(1.0 - (1.0 - sustain) * ((i - a) / d))
        elif i < a + d + s:
            env.append(sustain)
        else:
            k = i - (a + d + s)
            env.append(max(0.0, sustain * (1.0 - k / r)))
    return env


def _osc(freq_at_sample, n: int, sr: int, velocity: float,
         harmonics=(1.0, 0.35, 0.12)) -> list[float]:
    phase = 0.0
    out = [0.0] * n
    norm = sum(h / (i + 1) for i, h in enumerate(harmonics))
    for i in range(n):
        f = freq_at_sample(i)
        phase += 2.0 * math.pi * f / sr
        v = 0.0
        for h, amp in enumerate(harmonics, start=1):
            v += amp * math.sin(h * phase) / h
        out[i] = velocity * v / norm
    return out


def render_score(score: Score, tuning: TuningSystem,
                 sample_rate: int = 44100,
                 freq_range: tuple[float, float] = (20.0, 20000.0)
                 ) -> dict:
    """把整张谱面渲染成单声道浮点 PCM，并返回完整的音高映射元数据。"""
    total = int((score.length_seconds + 0.3) * sample_rate)
    mix = [0.0] * total
    mapping = []
    out_of_range = []

    def add_voice(start: float, dur: float, degree: int, vel: float,
                  freq_func=None):
        s0 = int(start * sample_rate)
        n = int(dur * sample_rate)
        const_freq = tuning.degree_to_freq(degree)
        lo, hi = freq_range
        if not (lo <= const_freq <= hi):
            out_of_range.append(
                {"time": start, "degree": degree,
                 "freq_hz": const_freq, "action": "muted"})
            return
        if freq_func is None:
            wave_data = _osc(lambda i, f=const_freq: f, n, sample_rate, vel)
        else:
            wave_data = _osc(freq_func, n, sample_rate, vel)
        env = _adsr_envelope(n, sample_rate)
        for i in range(n):
            idx = s0 + i
            if idx < total:
                mix[idx] += wave_data[i] * env[i]
        mapping.append(
            {"time": start, "duration": dur, "degree": degree,
             "label": tuning.label_of(degree),
             "freq_hz": const_freq,
             "ratio": str(tuning.degree_ratio(degree))})

    for ev in score.events:
        if isinstance(ev, NoteEvent):
            add_voice(ev.time, ev.duration, ev.degree, ev.velocity)
        elif isinstance(ev, ChordEvent):
            for d in ev.degrees:
                add_voice(ev.time, ev.duration, d, ev.velocity)
        elif isinstance(ev, GlissEvent):
            s0 = int(ev.time * sample_rate)
            n = int(ev.duration * sample_rate)
            f_from = tuning.degree_to_freq(ev.degree_from)
            f_to = tuning.degree_to_freq(ev.degree_to)
            lo, hi = freq_range
            for tag, f in (("from", f_from), ("to", f_to)):
                if not (lo <= f <= hi):
                    out_of_range.append(
                        {"time": ev.time, "degree": ev.degree_from,
                         "freq_hz": f, "action": f"gliss_{tag}_muted"})
            # 扫频占用前 70% 的时长，之后保持目标频率，
            # 使滑音末端存在可做稳态主频分析的段落（release 仅 12%）
            sweep_n = max(2, int(n * 0.70))

            def sweep(i, n=n, sweep_n=sweep_n, f_from=f_from, f_to=f_to):
                if i >= sweep_n:
                    return f_to
                p = i / (sweep_n - 1)
                return f_from * ((f_to / f_from) ** p)
            add_voice(ev.time, ev.duration, ev.degree_from, ev.velocity,
                      freq_func=sweep)
            mapping[-1]["gliss_to_degree"] = ev.degree_to
            mapping[-1]["gliss_to_freq_hz"] = f_to
            mapping[-1]["gliss_steps"] = ev.steps

    peak = max((abs(x) for x in mix), default=0.0)
    if peak > 0.99:
        mix = [0.99 * x / peak for x in mix]
    return {
        "pcm": mix,
        "sample_rate": sample_rate,
        "mapping": mapping,
        "out_of_range": out_of_range,
        "tuning": tuning.to_dict(),
        "policy": score.policy,
    }


def write_wav(path: str, pcm: list[float], sample_rate: int = 44100):
    """16-bit PCM 单声道 WAV 导出。"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for x in pcm:
            v = int(max(-1.0, min(1.0, x)) * 32767)
            frames += struct.pack("<h", v)
        wf.writeframes(bytes(frames))
    return path


def write_wav_stereo_compare(path: str, pcm_a: list[float],
                             pcm_b: list[float], sample_rate: int = 44100):
    """两种调律的同一谱面：左声道 A、右声道 B，可直接 A/B 对比。"""
    n = max(len(pcm_a), len(pcm_b))
    a = pcm_a + [0.0] * (n - len(pcm_a))
    b = pcm_b + [0.0] * (n - len(pcm_b))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for x, y in zip(a, b):
            frames += struct.pack(
                "<hh",
                int(max(-1.0, min(1.0, x)) * 32767),
                int(max(-1.0, min(1.0, y)) * 32767),
            )
        wf.writeframes(bytes(frames))
    return path
