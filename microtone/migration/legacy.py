"""旧版调律资料格式：MIDI 式弯音通道 + 十二音基底上的音分偏移。

旧格式的历史局限（本次迁移要解决的问题）：

* 调律只在十二平均律半音上叠加固定音分偏移，隐含“八度=2:1、每八度 12 个
  基底级”的假设，无法直接表达 Bohlen-Pierce 这类非八度、非十二等分体系；
* 音源通道数固定且共用一条弯音，同一通道上两个微分音无法分别施加弯音；
* 偏移一旦超出通道弯音范围就无法发出，但旧资料没有显式标注。

本模块只负责旧资料的读取与“真实目标频率”计算，不做任何发声决策。
频率计算一律按物理定义::

    f = ref_freq * 2 ** ((midi_note - ref_midi_note) / 12 + offset_cents / 1200)

其中 ref_midi_note 是参考频率对应的旧基底级数，偏移按音分精确参与计算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 旧资料格式版本（迁移输入侧）
FORMAT_VERSION_LEGACY = "legacy-bend/1"


@dataclass
class LegacyTuning:
    """一份旧弯音偏移调律资料。

    Attributes
    ----------
    name:
        资料名称（标签）。
    ref_freq:
        参考级真实频率 Hz。
    ref_midi_note:
        参考频率对应的旧基底级数（MIDI 音符号语义，可为任意整数）。
    bend_range_cents:
        每条通道弯音相对该通道当前基底级可表达的最大音分幅度，
        旧硬件常见 200（±200 音分）。
    channel_count:
        音源可用弯音通道数。
    offsets_cents:
        旧基底级 -> 相对十二平均律位置的音分偏移。未列出的级偏移为 0。
        key 以 ref_midi_note 为基准的级名（整数），与 midi 音号一一对应。
    period_ratio_note:
        资料中记录的周期说明；旧格式隐含 2/1，这里仅保留原文用于审计。
    """

    name: str
    ref_freq: float
    ref_midi_note: int = 69
    bend_range_cents: float = 200.0
    channel_count: int = 4
    offsets_cents: dict[int, float] = field(default_factory=dict)
    period_ratio_note: str = "2/1 (旧格式隐含八度)"

    def target_freq(self, midi_note: int) -> float:
        """旧资料中某个基底级发声时的真实目标频率（含音分偏移）。"""
        offset = self.offsets_cents.get(int(midi_note), 0.0)
        semis = (int(midi_note) - self.ref_midi_note) + offset / 100.0
        return self.ref_freq * (2.0 ** (semis / 12.0))

    def offset_of(self, midi_note: int) -> float:
        return self.offsets_cents.get(int(midi_note), 0.0)

    def is_representable(self, midi_note: int) -> bool:
        """该级偏移能否落在单条通道弯音范围内。

        弯音以该通道当前基底级为零点，因此可表达性只取决于偏移绝对值；
        跨八度的部分由选择基底级本身承担，不需要弯音覆盖。
        """
        return abs(self.offset_of(midi_note)) <= self.bend_range_cents + 1e-9

    def to_dict(self) -> dict:
        return {
            "format_version": FORMAT_VERSION_LEGACY,
            "name": self.name,
            "ref_freq": self.ref_freq,
            "ref_midi_note": self.ref_midi_note,
            "bend_range_cents": self.bend_range_cents,
            "channel_count": self.channel_count,
            "offsets_cents": {str(k): v for k, v in
                              sorted(self.offsets_cents.items())},
            "period_ratio_note": self.period_ratio_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LegacyTuning":
        version = data.get("format_version")
        if version is not None and version != FORMAT_VERSION_LEGACY:
            raise ValueError(
                f"不是受支持的旧格式版本: {version!r}，"
                f"期望 {FORMAT_VERSION_LEGACY!r}")
        offsets = data.get("offsets_cents", {})
        return cls(
            name=data["name"],
            ref_freq=float(data["ref_freq"]),
            ref_midi_note=int(data.get("ref_midi_note", 69)),
            bend_range_cents=float(data.get("bend_range_cents", 200.0)),
            channel_count=int(data.get("channel_count", 4)),
            offsets_cents={int(k): float(v) for k, v in offsets.items()},
            period_ratio_note=str(data.get(
                "period_ratio_note", "2/1 (旧格式隐含八度)")),
        )


@dataclass
class LegacyEvent:
    """旧谱面事件：时间（秒）+ 旧基底级 + 时长。"""

    time: float
    duration: float
    midi_note: int
    velocity: float = 1.0

    @property
    def end_time(self) -> float:
        return self.time + self.duration


@dataclass
class LegacyScore:
    bpm: float = 120.0
    events: list[LegacyEvent] = field(default_factory=list)

    @property
    def beat_seconds(self) -> float:
        return 60.0 / self.bpm

    @property
    def length_seconds(self) -> float:
        return max((e.end_time for e in self.events), default=0.0)


def parse_legacy_score(text: str) -> LegacyScore:
    """解析旧谱面 DSL。

    每行一个事件，# 为注释::

        bpm 100
        legacy-note 69 1.0 vel 0.8     # 旧基底级 69，1 拍
        legacy-chord 69,70 2.0         # 同时发声（可能争用弯音通道）
        legacy-rest 0.5
    """
    score = LegacyScore()
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

        def _vel(tokens: list[str]) -> float:
            if "vel" in tokens:
                return max(0.0, min(1.0,
                                    float(tokens[tokens.index("vel") + 1])))
            return 1.0

        if kind == "legacy-note":
            note = int(parts[1])
            beats = float(parts[2])
            score.events.append(LegacyEvent(
                cursor_beats * score.beat_seconds,
                beats * score.beat_seconds, note, _vel(parts)))
            cursor_beats += beats
        elif kind == "legacy-chord":
            notes = [int(x) for x in parts[1].split(",") if x != ""]
            beats = float(parts[2])
            vel = _vel(parts)
            t0 = cursor_beats * score.beat_seconds
            for note in notes:
                score.events.append(LegacyEvent(
                    t0, beats * score.beat_seconds, note, vel))
            cursor_beats += beats
        elif kind == "legacy-rest":
            beats = float(parts[1])
            cursor_beats += beats
        else:
            raise ValueError(f"无法识别的旧谱面指令: {kind} ({raw})")
    return score


def load_legacy_tuning(path: str) -> LegacyTuning:
    with open(path, "r", encoding="utf-8") as f:
        return LegacyTuning.from_dict(json.load(f))
