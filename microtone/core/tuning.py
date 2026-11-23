"""微分音调律核心数据模型。

设计原则（与具体界面、播放器无关）：

* 参考频率 ref_freq、周期比 period_ratio、音阶重复规则 period_steps 独立保存；
  不假定体系以八度(2:1)或十二音为基础。
* 音阶只存每一级相对参考级的比例（精确 Fraction），音名只是标签。
* 频率由比例直接计算，任何环节都不量化回十二平均律半音。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterable, Sequence


def _to_fraction(value) -> Fraction:
    """把 int/float/str/Fraction 统一转成精确 Fraction。

    支持的字符串形式："3/2"、"1.5"、"400.0c"(音分)、"400c"、
    以及幂表达式 "2^(1/5)"（按浮点求值后约束分母，用于无理比例）。
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value).limit_denominator(10_000_000)
    text = str(value).strip()
    if text.endswith(("c", "C")):
        cents = float(text[:-1])
        return Fraction(2) ** (cents / 1200.0)
    if "^" in text:
        base_text, exp_text = text.split("^", 1)
        base = float(_to_fraction(base_text.strip("() ")))
        exp = float(_to_fraction(exp_text.strip("() ")))
        return Fraction(base ** exp).limit_denominator(10_000_000)
    if "/" in text:
        num, den = text.split("/", 1)
        return Fraction(int(num), int(den))
    if "." in text or "e" in text or "E" in text:
        return Fraction(float(text)).limit_denominator(10_000_000)
    return Fraction(int(text))


def ratio_to_cents(ratio: Fraction) -> float:
    """比例 -> 音分值（仅用于显示，不参与发声计算）。"""
    return 1200.0 * math.log2(float(ratio))


def cents_str(ratio: Fraction) -> str:
    return f"{ratio_to_cents(ratio):+.2f}c"


@dataclass
class TuningSystem:
    """一个完整调律体系。

    Attributes
    ----------
    name:
        体系名称，仅为标签。
    ref_freq:
        参考级的真实频率（Hz），例如 261.63。
    ref_degree:
        参考频率对应音阶中的第几级（scale 的下标）。
    period_steps:
        音阶每重复一次跨越多少级（例如七声音阶写 7，不假定为 12）。
    period_ratio:
        重复周期的频率比（Fraction）。八度体系是 2/1，Bohlen-Pierce 是 3/1。
    scale:
        一个周期内各级相对周期起点的比例（Fraction 序列，长度 >= period_steps）。
        第 k 级的周期内比例即 scale[k]。
    labels:
        各级音名标签，仅用于显示；播放完全不依赖它。
    """

    name: str
    ref_freq: float
    ref_degree: int
    period_steps: int
    period_ratio: Fraction
    scale: list[Fraction]
    labels: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.period_ratio = _to_fraction(self.period_ratio)
        self.scale = [_to_fraction(r) for r in self.scale]
        if self.period_steps <= 0:
            raise ValueError("period_steps 必须为正整数")
        if len(self.scale) < self.period_steps:
            raise ValueError("scale 长度不能小于 period_steps")
        if not (0 <= self.ref_degree < self.period_steps):
            raise ValueError("ref_degree 必须落在一个周期的级数范围内")
        if not self.labels:
            self.labels = [f"deg{i}" for i in range(self.period_steps)]
        elif len(self.labels) < self.period_steps:
            self.labels = list(self.labels) + [
                f"deg{i}" for i in range(len(self.labels), self.period_steps)
            ]

    # ---- 频率映射（核心，不许量化到半音） --------------------------------

    def degree_ratio(self, degree: int) -> Fraction:
        """任意级数（可为负、可跨任意多个周期）相对参考级的精确比例。"""
        period_index, within = divmod(degree - self.ref_degree, self.period_steps)
        ref_within = self.ref_degree
        target_index = ref_within + within
        # target_index 一定落在 [0, period_steps) 内
        return (self.period_ratio ** period_index) * (
            self.scale[target_index] / self.scale[ref_within]
        )

    def degree_to_freq(self, degree: int) -> float:
        """级数 -> 真实频率（Hz）。"""
        return self.ref_freq * float(self.degree_ratio(degree))

    def label_of(self, degree: int) -> str:
        period_index, within = divmod(degree, self.period_steps)
        base = self.labels[within]
        if period_index == 0:
            return base
        sign = "+" if period_index > 0 else ""
        return f"{base}{sign}{period_index}"

    def freq_table(self, lo_degree: int, hi_degree: int) -> list[dict]:
        rows = []
        for d in range(lo_degree, hi_degree + 1):
            ratio = self.degree_ratio(d)
            rows.append(
                {
                    "degree": d,
                    "label": self.label_of(d),
                    "freq_hz": self.degree_to_freq(d),
                    "ratio": str(ratio),
                    "ratio_float": float(ratio),
                    "cents_vs_ref": ratio_to_cents(ratio),
                }
            )
        return rows

    # ---- 两体系间的真实音程比较 ------------------------------------------

    def interval_between(self, d1: int, d2: int) -> Fraction:
        """同一体系内两个级数的精确频率比（高/低）。"""
        r = self.degree_ratio(d2) / self.degree_ratio(d1)
        return r if r >= 1 else 1 / r

    # ---- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ref_freq": self.ref_freq,
            "ref_degree": self.ref_degree,
            "period_steps": self.period_steps,
            "period_ratio": str(self.period_ratio),
            "scale": [str(r) for r in self.scale[: self.period_steps]],
            "labels": list(self.labels[: self.period_steps]),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> "TuningSystem":
        return cls(
            name=data["name"],
            ref_freq=float(data["ref_freq"]),
            ref_degree=int(data["ref_degree"]),
            period_steps=int(data["period_steps"]),
            period_ratio=_to_fraction(data["period_ratio"]),
            scale=[_to_fraction(r) for r in data["scale"]],
            labels=list(data.get("labels", [])),
        )

    @classmethod
    def from_json(cls, text: str) -> "TuningSystem":
        return cls.from_dict(json.loads(text))


def equal_temperament(
    steps_per_octave: int,
    ref_freq: float = 261.625565,
    ref_degree: int = 0,
    name: str | None = None,
) -> TuningSystem:
    """N 平均律（N 等分八度）工厂，用于和 12 平均律做近邻对比。"""
    n = int(steps_per_octave)
    scale = [Fraction(2) ** (k / n) for k in range(n)]
    labels = [f"{n}ET-{k}" for k in range(n)]
    return TuningSystem(
        name=name or f"{n}-TET 八度平均律",
        ref_freq=ref_freq,
        ref_degree=ref_degree,
        period_steps=n,
        period_ratio=Fraction(2, 1),
        scale=scale,
        labels=labels,
    )


def just_scale(ref_freq: float = 261.625565) -> TuningSystem:
    """C 大调纯律（五度相生式大三和弦比例），周期为八度、七级。"""
    # 相对 C 的比例：1, 9/8, 5/4, 4/3, 3/2, 5/3, 15/8
    scale = [
        Fraction(1),
        Fraction(9, 8),
        Fraction(5, 4),
        Fraction(4, 3),
        Fraction(3, 2),
        Fraction(5, 3),
        Fraction(15, 8),
    ]
    return TuningSystem(
        name="C 大调纯律 (Just Intonation)",
        ref_freq=ref_freq,
        ref_degree=0,
        period_steps=7,
        period_ratio=Fraction(2, 1),
        scale=scale,
        labels=["C", "D", "E", "F", "G", "A", "B"],
    )


def pythagorean_scale(ref_freq: float = 261.625565) -> TuningSystem:
    """毕达哥拉斯（五度相生）七声音阶。"""
    # 连续纯五度 3/2 折回一个八度
    fifths = [0, 2, 4, -1, 1, 3, 5]  # C D E F G A B 在五度圈上的位置
    scale = []
    for k in fifths:
        r = (Fraction(3, 2) ** k)
        # 折回 [1,2) 八度
        while r < 1:
            r *= 2
        while r >= 2:
            r /= 2
        scale.append(r)
    return TuningSystem(
        name="毕达哥拉斯五度相生律",
        ref_freq=ref_freq,
        ref_degree=0,
        period_steps=7,
        period_ratio=Fraction(2, 1),
        scale=scale,
        labels=["C", "D", "E", "F", "G", "A", "B"],
    )


def bohlen_pierce(ref_freq: float = 261.625565) -> TuningSystem:
    """Bohlen-Pierce 音阶：周期比 3/1（非八度！），每周期 13 级。"""
    n = 13
    scale = [Fraction(3) ** (k / n) for k in range(n)]
    return TuningSystem(
        name="Bohlen-Pierce (3:1 周期, 13 级)",
        ref_freq=ref_freq,
        ref_degree=0,
        period_steps=n,
        period_ratio=Fraction(3, 1),
        scale=scale,
        labels=[chr(ord("A") + i) for i in range(n)],
    )


def quarter_tone_scale(ref_freq: float = 261.625565) -> TuningSystem:
    """24 平均律（四分之一音），每级 50 音分。"""
    return equal_temperament(24, ref_freq, name="24-TET 四分音音阶")


def meantone_scale(ref_freq: float = 261.625565) -> TuningSystem:
    """四一调律（quarter-comma meantone）七声音阶。

    每个五度收窄 1/4 普通音差，大三度为精确 5/4。
    """
    # 以 5/4 的等比四次根定义五度：5^(1/4)
    fifth = Fraction(5) ** 0.25  # 约 1.49535
    fifths = [0, 2, 4, -1, 1, 3, 5]
    scale = []
    for k in fifths:
        r = fifth ** k
        while r < 1:
            r *= 2
        while r >= 2:
            r /= 2
        scale.append(_to_fraction(r))
    return TuningSystem(
        name="四一中庸全音律 (1/4-comma meantone)",
        ref_freq=ref_freq,
        ref_degree=0,
        period_steps=7,
        period_ratio=Fraction(2, 1),
        scale=scale,
        labels=["C", "D", "E", "F", "G", "A", "B"],
    )


def builtin_systems(ref_freq: float = 261.625565) -> dict[str, TuningSystem]:
    return {
        "12-TET": equal_temperament(12, ref_freq, name="12-TET 十二平均律"),
        "24-TET": quarter_tone_scale(ref_freq),
        "19-TET": equal_temperament(19, ref_freq, name="19-TET 十九平均律"),
        "just": just_scale(ref_freq),
        "pythagorean": pythagorean_scale(ref_freq),
        "meantone": meantone_scale(ref_freq),
        "bohlen-pierce": bohlen_pierce(ref_freq),
    }
