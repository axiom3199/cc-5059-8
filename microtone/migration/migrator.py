"""迁移器：把旧弯音资料与旧事件转换为显式比值新格式，并给出执行证据。

迁移产物不是“改个音名标签”，而是可直接发声的新 TuningSystem：
每个旧基底级在新格式里占一个独立级数，级数比例按旧资料的真实目标频率
反算为精确 Fraction，发声走 TuningSystem.degree_to_freq 的新映射路径。

同时输出三类可核对证据：

* 通道时间线（ChannelEvent）：旧音源实际会收到的 note_on/bend/note_off/
  release/pending 事件；
* 逐音迁移记录（MigratedNote）：旧级 -> 新级数 -> 旧目标频率 -> 新映射频率；
* 无法表达清单（UnrepresentableNote）：超出旧弯音范围的具体音，
  可表达部分的频率仍然完整保留。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from fractions import Fraction

from ..core.tuning import TuningSystem, _to_fraction
from ..core.score import Score, NoteEvent
from ..core.renderer import render_score
from ..core.analysis import verify_render
from .legacy import LegacyTuning, LegacyScore, LegacyEvent
from .channels import simulate_channel_events, Allocation

# 新调律资料格式版本（迁移输出侧）。与 lock.FORMAT_LOCK 一起构成构建锁定。
FORMAT_VERSION = "explicit-ratio/2"
_FREQ_REL_TOL = 1e-9


def _ratio_from_cents(cents: float) -> Fraction:
    """音分偏移 -> 精确 Fraction 比例（与调律编辑器同口径）。"""
    return _to_fraction(Fraction(2) ** (cents / 1200.0))


@dataclass
class MigratedNote:
    legacy_midi_note: int
    new_degree: int
    label: str
    offset_cents: float
    legacy_target_hz: float
    new_mapped_hz: float
    ratio_vs_ref: str
    representable_on_legacy: bool
    channel: int | None
    held_pending: bool
    note: str = ""


@dataclass
class UnrepresentableNote:
    legacy_midi_note: int
    offset_cents: float
    bend_range_cents: float
    target_freq_hz: float
    reason: str


@dataclass
class SafePoint:
    time: float
    reason: str


@dataclass
class StrategyVoiceResult:
    legacy_midi_note: int
    new_degree: int
    old_freq_hz: float
    new_freq_hz: float
    delta_hz: float
    delta_cents: float


@dataclass
class StrategyComparison:
    hold_freq: list[StrategyVoiceResult]
    hold_ratio: list[StrategyVoiceResult]
    safe_switch: list[StrategyVoiceResult]
    safe_points: list[SafePoint]
    explanation: str


@dataclass
class MigrationManifest:
    format_version: str
    legacy_format_version: str
    source_name: str
    tuning: dict
    migrated_score_text: str
    notes: list[MigratedNote]
    unrepresentable: list[UnrepresentableNote]
    channel_timeline: list[dict]
    allocations: list[dict]
    strategy_comparison: dict
    verification: list[dict]
    frequency_contract_ok: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "legacy_format_version": self.legacy_format_version,
            "source_name": self.source_name,
            "tuning": self.tuning,
            "migrated_score_text": self.migrated_score_text,
            "notes": [asdict(n) for n in self.notes],
            "unrepresentable": [asdict(n) for n in self.unrepresentable],
            "channel_timeline": self.channel_timeline,
            "allocations": self.allocations,
            "strategy_comparison": self.strategy_comparison,
            "verification": self.verification,
            "frequency_contract_ok": self.frequency_contract_ok,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------
# 新格式调律构建
# --------------------------------------------------------------------------

def build_new_tuning(legacy: LegacyTuning,
                     score: LegacyScore | None = None) -> tuple[TuningSystem, dict[int, int]]:
    """从旧资料构建显式比值 TuningSystem，并返回 旧级->新级数 映射。

    新调律保留旧资料的参考频率与八度周期（2:1），但每个旧基底级都成为
    独立级数，比例由真实目标频率反算；非十二等分的音分偏移因此以“比值”
    而非“半音+弯音”的形式永久保留，含义不被十二平均律吞掉。
    """
    used = set(legacy.offsets_cents.keys())
    if score is not None:
        used.update(e.midi_note for e in score.events)
    used.add(legacy.ref_midi_note)
    ordered = sorted(used)

    ref_freq = legacy.ref_freq
    # 一个周期覆盖所有出现过的级；周期比仍为 2:1（旧资料隐含八度）。
    period_steps = len(ordered)
    index_of = {note: i for i, note in enumerate(ordered)}
    ref_degree = index_of[legacy.ref_midi_note]

    # scale[k] = 第 k 级相对最低级的比例
    base_freq = legacy.target_freq(ordered[0])
    scale: list[Fraction] = []
    for note in ordered:
        f = legacy.target_freq(note)
        scale.append(_to_fraction(Fraction(f / base_freq).limit_denominator(
            10_000_000)))

    labels = [f"L{note}" for note in ordered]
    tuning = TuningSystem(
        name=f"{legacy.name}（迁移：显式比值）",
        ref_freq=ref_freq,
        ref_degree=ref_degree,
        period_steps=period_steps,
        period_ratio=Fraction(2, 1),
        scale=scale,
        labels=labels,
    )
    return tuning, index_of


def convert_events(legacy: LegacyTuning, score: LegacyScore,
                   degree_map: dict[int, int]) -> tuple[Score, list[NoteEvent]]:
    """旧事件 -> 新级数谱面事件（时间、时长、力度原样保留）。"""
    new = Score(bpm=score.bpm, default_tuning="",
                policy="hold_ratio", events=[])
    for ev in score.events:
        if ev.midi_note not in degree_map:
            raise KeyError(
                f"旧级 {ev.midi_note} 未进入新调律映射，迁移不完整")
        new.events.append(NoteEvent(
            time=ev.time, duration=ev.duration,
            degree=degree_map[ev.midi_note], velocity=ev.velocity))
    return new, [e for e in new.events if isinstance(e, NoteEvent)]


def migrated_score_text(score: LegacyScore, degree_map: dict[int, int],
                        tuning_name: str) -> str:
    """生成与新调律配套、可直接渲染的谱面 DSL 文本。"""
    lines = [
        f"# 由旧弯音谱面迁移生成（格式 {FORMAT_VERSION}），勿手改频率",
        f"bpm {score.bpm:g}",
        f"tuning {tuning_name}",
        "policy hold_ratio",
    ]
    # 同时刻事件合并回 chord，其余按 note 输出；时间用拍重排
    by_time: dict[float, list[LegacyEvent]] = {}
    for ev in score.events:
        by_time.setdefault(ev.time, []).append(ev)
    beat = score.beat_seconds
    cursor = 0.0
    for t in sorted(by_time):
        gap_beats = (t - cursor) / beat
        if gap_beats > 1e-9:
            lines.append(f"rest {gap_beats:g}")
            cursor += gap_beats * beat
        group = sorted(by_time[t], key=lambda e: e.midi_note)
        degrees = [str(degree_map[e.midi_note]) for e in group]
        dur_beats = group[0].duration / beat
        if len(group) == 1:
            lines.append(f"note {degrees[0]} {dur_beats:g}")
        else:
            lines.append(f"chord {','.join(degrees)} {dur_beats:g}")
        cursor = t + group[0].duration
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 切换策略对比
# --------------------------------------------------------------------------

def _active_at(score: LegacyScore, time: float) -> list[LegacyEvent]:
    return [e for e in score.events
            if e.time - 1e-12 <= time < e.end_time - 1e-12]


def compare_switch_strategies(legacy: LegacyTuning, score: LegacyScore,
                              old_tuning: TuningSystem,
                              new_tuning: TuningSystem,
                              degree_map: dict[int, int],
                              switch_time: float | None = None
                              ) -> StrategyComparison:
    """在调律切换点比较两种续接策略与“安全换气点切换”。

    hold_freq
        持续音锁定旧物理频率，按原音程比保留持续音；调律变了但已发声音不挪。
    hold_ratio
        持续音按新调律同级数重算，物理频率移动到新映射。
    safe_switch
        切换点处仍在发的音保持旧频率直到各自结束；在没有任何在发音的
        安全换气点才整体切到新调律，因此切换瞬间不存在被拽动的持续音。
    """
    if switch_time is None:
        switch_time = min((e.time for e in score.events), default=0.0)

    active = _active_at(score, switch_time)
    hold_freq_rows: list[StrategyVoiceResult] = []
    hold_ratio_rows: list[StrategyVoiceResult] = []
    safe_rows: list[StrategyVoiceResult] = []
    for ev in active:
        degree = degree_map[ev.midi_note]
        old_freq = old_tuning.degree_to_freq(degree)
        new_freq = new_tuning.degree_to_freq(degree)
        common = dict(
            legacy_midi_note=ev.midi_note, new_degree=degree,
            old_freq_hz=old_freq, new_freq_hz=new_freq,
            delta_hz=new_freq - old_freq,
            delta_cents=1200.0 * math.log2(new_freq / old_freq))
        # hold_freq：物理频率不动
        hold_freq_rows.append(StrategyVoiceResult(
            ev.midi_note, degree, old_freq, old_freq, 0.0, 0.0))
        # hold_ratio：跟随新调律
        hold_ratio_rows.append(StrategyVoiceResult(**common))
        # 安全换气点：持续音维持旧频率到结束，故切换对该音无频率改动
        safe_rows.append(StrategyVoiceResult(
            ev.midi_note, degree, old_freq, old_freq, 0.0, 0.0))

    # 找安全换气点：任一在发音结束、且该时刻没有在发音的最早时间点
    end_times = sorted({e.end_time for e in score.events})
    safe_points: list[SafePoint] = []
    chosen = None
    for t in end_times:
        if t < switch_time - 1e-12:
            continue
        if not _active_at(score, t):
            sp = SafePoint(time=t, reason="该时刻无在发音，可整体切到新调律")
            safe_points.append(sp)
            if chosen is None:
                chosen = sp
    if chosen is None:
        chosen = SafePoint(time=score.length_seconds,
                           reason="谱面结束后切换（全程有持续音）")
        safe_points.append(chosen)

    explanation = (
        f"切换请求 t={switch_time:.3f}s 处有 {len(active)} 个在发音。"
        "hold_freq 保留其物理频率（原音程比随新表改变）；"
        "hold_ratio 立即按新映射重算（物理频率移动见 delta）；"
        f"安全换气点策略把实际切换推迟到 t={chosen.time:.3f}s，"
        "持续音不受影响，之后的新事件才走新版调律。")
    return StrategyComparison(
        hold_freq=hold_freq_rows, hold_ratio=hold_ratio_rows,
        safe_switch=safe_rows, safe_points=safe_points,
        explanation=explanation)


# --------------------------------------------------------------------------
# 逐音独听核对（真实运算，不靠手改数据）
# --------------------------------------------------------------------------

def verify_migration(legacy: LegacyTuning, new_tuning: TuningSystem,
                     degree_map: dict[int, int],
                     sample_rate: int = 22050) -> list[dict]:
    """对每个迁移后的级数做单音独听渲染，再从 PCM 实测主频核对。

    实测主频必须同时逼近：(a) 新映射频率；(b) 旧资料目标频率。
    二者一致是迁移契约成立的证据，任一不符都会标记 fail。
    """
    checks = []
    for note, degree in sorted(degree_map.items()):
        legacy_target = legacy.target_freq(note)
        solo = Score(events=[NoteEvent(time=0.0, duration=0.8, degree=degree)])
        render = render_score(solo, new_tuning, sample_rate=sample_rate)
        measured = verify_render(render, tolerance_cents=3.0)
        estimated = measured[0]["estimated_hz"] if measured else float("nan")
        new_freq = new_tuning.degree_to_freq(degree)
        err_legacy = 1200.0 * math.log2(estimated / legacy_target)
        err_new = 1200.0 * math.log2(estimated / new_freq)
        contract = math.isclose(new_freq, legacy_target,
                                rel_tol=_FREQ_REL_TOL)
        checks.append({
            "legacy_midi_note": note,
            "new_degree": degree,
            "legacy_target_hz": legacy_target,
            "new_mapped_hz": new_freq,
            "measured_hz": estimated,
            "error_vs_legacy_cents": err_legacy,
            "error_vs_new_cents": err_new,
            "frequency_contract_ok": contract,
            "pass": bool(measured
                         and abs(err_legacy) <= 3.0
                         and abs(err_new) <= 3.0
                         and contract),
        })
    return checks


# --------------------------------------------------------------------------
# 迁移主入口
# --------------------------------------------------------------------------

def migrate_tuning(legacy: LegacyTuning, score: LegacyScore,
                   release_seconds: float = 0.12,
                   sample_rate: int = 22050,
                   switch_time: float | None = None) -> MigrationManifest:
    """执行完整迁移并返回带全部证据的清单（不在此处写文件）。"""
    new_tuning, degree_map = build_new_tuning(legacy, score)

    # 旧音源通道模拟（含释放占用与待处理）
    allocator, timeline = simulate_channel_events(
        legacy, score, release_seconds=release_seconds)
    alloc_by_note: dict[int, Allocation] = {}
    for alloc in allocator.allocations:
        alloc_by_note.setdefault(alloc.event.midi_note, alloc)

    # 逐音迁移记录
    notes: list[MigratedNote] = []
    unrepresentable: list[UnrepresentableNote] = []
    for note in sorted(degree_map):
        degree = degree_map[note]
        new_freq = new_tuning.degree_to_freq(degree)
        legacy_freq = legacy.target_freq(note)
        alloc = alloc_by_note.get(note)
        representable = legacy.is_representable(note)
        if not representable:
            unrepresentable.append(UnrepresentableNote(
                legacy_midi_note=note,
                offset_cents=legacy.offset_of(note),
                bend_range_cents=legacy.bend_range_cents,
                target_freq_hz=legacy_freq,
                reason=(f"音分偏移 {legacy.offset_of(note):+.2f} 超出旧通道 "
                        f"±{legacy.bend_range_cents:.0f}c 弯音范围；"
                        "新映射仍保留该真实频率")))
        notes.append(MigratedNote(
            legacy_midi_note=note,
            new_degree=degree,
            label=new_tuning.labels[degree],
            offset_cents=legacy.offset_of(note),
            legacy_target_hz=legacy_freq,
            new_mapped_hz=new_freq,
            ratio_vs_ref=str(new_tuning.degree_ratio(degree)),
            representable_on_legacy=representable,
            channel=None if alloc is None else alloc.channel,
            held_pending=False if alloc is None else alloc.held_pending,
            note=("旧谱面未使用该级（仅调律表迁移，无通道事件）"
                  if alloc is None else alloc.note)))

    # 无法用旧通道表达的和弦：逐音标记，可表达部分频率照保
    warnings = []
    by_time: dict[float, list[LegacyEvent]] = {}
    for ev in score.events:
        by_time.setdefault(ev.time, []).append(ev)
    for t, group in sorted(by_time.items()):
        if len(group) < 2:
            continue
        bad = [e.midi_note for e in group
               if not legacy.is_representable(e.midi_note)]
        if bad:
            kept = [f"{e.midi_note}@{legacy.target_freq(e.midi_note):.3f}Hz"
                    for e in group if legacy.is_representable(e.midi_note)]
            warnings.append(
                f"t={t:.3f}s 和弦中旧通道无法表达的音: {bad}；"
                f"可表达部分频率已保留: {kept}")

    new_score, _ = convert_events(legacy, score, degree_map)
    score_text = migrated_score_text(
        score, degree_map, f"migrated::{legacy.name}")

    comparison = compare_switch_strategies(
        legacy, score, new_tuning, new_tuning, degree_map,
        switch_time=switch_time)
    comp_dict = {
        "explanation": comparison.explanation,
        "safe_points": [asdict(p) for p in comparison.safe_points],
        "hold_freq": [asdict(r) for r in comparison.hold_freq],
        "hold_ratio": [asdict(r) for r in comparison.hold_ratio],
        "safe_switch": [asdict(r) for r in comparison.safe_switch],
    }

    verification = verify_migration(
        legacy, new_tuning, degree_map, sample_rate=sample_rate)
    contract_ok = (all(v["frequency_contract_ok"] for v in verification)
                   and all(v["pass"] for v in verification))

    return MigrationManifest(
        format_version=FORMAT_VERSION,
        legacy_format_version=legacy.to_dict()["format_version"],
        source_name=legacy.name,
        tuning=new_tuning.to_dict(),
        migrated_score_text=score_text,
        notes=notes,
        unrepresentable=unrepresentable,
        channel_timeline=[asdict(e) for e in timeline],
        allocations=[asdict(a) for a in allocator.allocations],
        strategy_comparison=comp_dict,
        verification=verification,
        frequency_contract_ok=contract_ok,
        warnings=warnings)


def verify_legacy_example(legacy_path: str, score_text: str,
                          release_seconds: float = 0.12) -> MigrationManifest:
    """供示例/测试使用：从旧资料文件与旧谱面文本跑完整迁移并核对。"""
    from .legacy import load_legacy_tuning
    legacy = load_legacy_tuning(legacy_path)
    score = parse_legacy_score(score_text)
    return migrate_tuning(legacy, score, release_seconds=release_seconds)
