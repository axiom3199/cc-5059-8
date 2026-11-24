"""旧音源弯音通道分配与事件级模拟（时间驱动）。

旧音源每条通道只有一条弯音（pitch bend），通道上所有在发音符共享它。
微分音迁移必须在这个真实约束下决定每个旧事件落到哪条通道，并把
“通道仍在释放、不能立刻复用”的情况显式建模出来。

关键规则（对应迁移需求，禁止违反）：

1. 两个**同时在发**的音即使起音时刻相同，也不能长期共用一条弯音通道，
   因为它们各自的目标弯音不同；拿不到空闲通道时后到的音改派或保留为
   待处理（pending），绝不共享在发音的后续弯音。
2. 通道进入 release（释放尾音）后不会立刻空闲：合成器回报该通道仍在释放，
   释放结束前不能在同通道上起新音，否则旧尾音会被新弯音拽走。
3. 待处理音在**通道真正空闲的那个时刻**立即补发，补发时刻由释放结束事件
   驱动而不是由下一个外部事件驱动；待处理音保留原始目标频率，不会被改写成
   共享弯音下的错误频率。
"""

from __future__ import annotations

from dataclasses import dataclass

from .legacy import LegacyEvent, LegacyTuning

# 通道生命周期状态
IDLE = "idle"            # 完全空闲，可立即分配
ACTIVE = "active"        # 有在发音，弯音归该音独有
RELEASING = "releasing"  # 收到 note-off，合成器仍在播放释放尾音


@dataclass
class ChannelEvent:
    """一次真实下发到旧音源的通道事件（执行证据用）。"""

    time: float
    kind: str                 # note_on / bend / note_off / release_done /
    #                           pending_held / pending_cancel
    channel: int
    midi_note: int | None = None
    bend_cents: float = 0.0   # 相对该通道基底级的弯音（音分）
    target_freq_hz: float = 0.0
    detail: str = ""


@dataclass
class Allocation:
    """单个旧谱面事件的通道分配结论（pending 补发时会原地更新）。"""

    event: LegacyEvent
    channel: int | None               # None 表示尚未拿到通道
    bend_cents: float
    target_freq_hz: float
    representable: bool
    held_pending: bool = False
    flushed_at: float | None = None
    note: str = ""


@dataclass
class _Channel:
    index: int
    state: str = IDLE
    note: int | None = None
    bend_cents: float = 0.0
    free_at: float = 0.0      # RELEASING 通道真正空闲的时刻


class BendChannelAllocator:
    """按时间推进的弯音通道分配器（含释放占用与待处理队列）。

    Parameters
    ----------
    tuning:
        旧调律资料，提供通道数、弯音范围与目标频率。
    release_seconds:
        合成器释放尾音时长。note-off 之后通道在该时长内保持 RELEASING，
        不能复用；这正是“合成器回报其中一音仍在释放”的建模参数。
    """

    def __init__(self, tuning: LegacyTuning, release_seconds: float = 0.12):
        self.tuning = tuning
        self.release_seconds = float(release_seconds)
        self.channels = [_Channel(i) for i in range(tuning.channel_count)]
        self.timeline: list[ChannelEvent] = []
        self.allocations: list[Allocation] = []
        self._pending: list[Allocation] = []
        self._now = 0.0

    # ---- 内部工具 --------------------------------------------------------

    def _first_idle(self) -> "_Channel | None":
        for ch in self.channels:
            if ch.state == IDLE:
                return ch
        return None

    def _release_due_before(self, now: float):
        """归还 free_at <= now 的释放通道，并在每个空闲时刻补发待处理音。"""
        due = sorted(c.free_at for c in self.channels
                     if c.state == RELEASING and c.free_at <= now + 1e-12)
        for free_at in due:
            for ch in self.channels:
                if (ch.state == RELEASING
                        and abs(ch.free_at - free_at) <= 1e-12):
                    self.timeline.append(ChannelEvent(
                        time=free_at, kind="release_done", channel=ch.index,
                        midi_note=ch.note, detail="释放尾音结束，通道归还"))
                    ch.state = IDLE
                    ch.note = None
                    ch.bend_cents = 0.0
            # 通道一归还就立即补发等待中的音（精确到释放结束时刻）
            self._drain_pending(free_at)

    def _drain_pending(self, now: float):
        while self._pending:
            ch = self._first_idle()
            if ch is None:
                return
            alloc = self._pending.pop(0)
            # 待处理音若在补发前就已结束，则撤销，不再占用通道
            if now >= alloc.event.end_time - 1e-12:
                self.timeline.append(ChannelEvent(
                    time=min(now, alloc.event.end_time),
                    kind="pending_cancel", channel=-1,
                    midi_note=alloc.event.midi_note,
                    detail="待处理音在补发前已结束，撤销"))
                alloc.note = "待处理期间已结束，未占用通道"
                continue
            self._activate(ch, alloc, now=now, via_pending=True)

    def _activate(self, ch: "_Channel", alloc: Allocation, now: float,
                  via_pending: bool):
        ch.state = ACTIVE
        ch.note = alloc.event.midi_note
        ch.bend_cents = alloc.bend_cents
        alloc.channel = ch.index
        alloc.held_pending = via_pending
        alloc.flushed_at = now if via_pending else None
        alloc.note = ("无空闲通道，先保留为待处理后补发"
                      if via_pending else "独占弯音通道")
        self.timeline.append(ChannelEvent(
            time=now, kind="note_on", channel=ch.index,
            midi_note=alloc.event.midi_note, bend_cents=alloc.bend_cents,
            target_freq_hz=alloc.target_freq_hz,
            detail=("待处理音补发" if via_pending else "起音分配")))
        if alloc.bend_cents:
            self.timeline.append(ChannelEvent(
                time=now, kind="bend", channel=ch.index,
                midi_note=alloc.event.midi_note, bend_cents=alloc.bend_cents,
                target_freq_hz=alloc.target_freq_hz,
                detail="该通道独有的后续弯音"))

    # ---- 对外主流程 ------------------------------------------------------

    def advance(self, now: float):
        """把时间推进到 now：先处理到期释放与补发，再登记当前时刻。"""
        self._release_due_before(now)
        self._now = now

    def handle_event(self, ev: LegacyEvent) -> Allocation:
        """处理一个已按时间到达的旧 note 事件（调用前先 advance 到其时刻）。"""
        note = ev.midi_note
        bend = self.tuning.offset_of(note)
        target = self.tuning.target_freq(note)
        representable = self.tuning.is_representable(note)
        alloc = Allocation(event=ev, channel=None, bend_cents=bend,
                           target_freq_hz=target,
                           representable=representable)

        # 不可表达的偏移：明确标记，不允许静默按错误弯音发声
        if not representable:
            alloc.held_pending = True
            alloc.note = "旧通道无法表达（已标记并保留真实频率）"
            self.timeline.append(ChannelEvent(
                time=ev.time, kind="pending_held", channel=-1,
                midi_note=note, bend_cents=bend, target_freq_hz=target,
                detail=(f"偏移 {bend:+.2f}c 超出弯音范围 "
                        f"±{self.tuning.bend_range_cents:.0f}c，"
                        "旧通道无法表达，标记并保住真实频率")))
            self.allocations.append(alloc)
            return alloc

        ch = self._first_idle()
        if ch is None:
            # 全部通道在发或仍在释放：保留待处理，绝不共用在发音的后续弯音
            alloc.held_pending = True
            alloc.note = "等待空闲通道"
            self._pending.append(alloc)
            self.timeline.append(ChannelEvent(
                time=ev.time, kind="pending_held", channel=-1,
                midi_note=note, bend_cents=bend, target_freq_hz=target,
                detail="无空闲通道（含仍在释放的通道），音保留为待处理"))
            self.allocations.append(alloc)
            return alloc

        self._activate(ch, alloc, now=ev.time, via_pending=False)
        self.allocations.append(alloc)
        return alloc

    def release_event(self, ev: LegacyEvent, alloc: Allocation):
        """对应某个 note 事件的 note-off。

        仍在 pending（未补发）的事件从待处理队列移除；已在通道上的音让通道
        进入释放占用，释放结束前不可复用。
        """
        now = ev.end_time
        if alloc in self._pending:
            self._pending.remove(alloc)
            self.timeline.append(ChannelEvent(
                time=now, kind="pending_cancel", channel=-1,
                midi_note=ev.midi_note,
                detail="待处理音在补发前结束，撤销"))
            alloc.note = "待处理期间已结束，未占用通道"
            return
        if alloc.channel is None:
            # 不可表达等从未拿到通道的事件，没有通道可释放
            return
        ch = self.channels[alloc.channel]
        if ch.state != ACTIVE:
            return
        ch.state = RELEASING
        ch.free_at = now + self.release_seconds
        self.timeline.append(ChannelEvent(
            time=now, kind="note_off", channel=ch.index,
            midi_note=ev.midi_note, bend_cents=ch.bend_cents,
            target_freq_hz=self.tuning.target_freq(ev.midi_note),
            detail=("合成器回报该通道进入释放，"
                    f"{self.release_seconds:.3f}s 内不可复用")))

    def finalize(self) -> list[ChannelEvent]:
        """推进到所有释放结束、待处理音全部补发或撤销为止。"""
        guard = 0
        while self._pending or any(c.state == RELEASING for c in self.channels):
            guard += 1
            if guard > 10000:
                raise RuntimeError("通道释放模拟未能收敛")
            next_free = min(c.free_at for c in self.channels
                            if c.state == RELEASING)
            self.advance(next_free)
        return self.timeline


def simulate_channel_events(tuning: LegacyTuning, score,
                            release_seconds: float = 0.12
                            ) -> tuple[BendChannelAllocator, list[ChannelEvent]]:
    """对整张旧谱面跑一遍时间驱动的通道模拟。

    note-on 与 note-off 放进同一条时间线交错处理（同一时刻先释放后起音），
    待处理音在通道释放完成的精确时刻补发。起音相同的两个音若空闲通道不足，
    第二个会改派或 pending，不会共享第一条通道的后续弯音。
    """
    allocator = BendChannelAllocator(tuning, release_seconds)
    ordered = sorted(score.events, key=lambda e: (e.time, e.midi_note))

    queue: list[tuple[float, int, str, LegacyEvent]] = []
    for ev in ordered:
        queue.append((ev.time, 0, "on", ev))
        queue.append((ev.end_time, 1, "off", ev))
    queue.sort(key=lambda q: (q[0], q[1]))

    alloc_of: dict[int, Allocation] = {}
    for t, _, kind, ev in queue:
        allocator.advance(t)
        if kind == "on":
            alloc_of[id(ev)] = allocator.handle_event(ev)
        else:
            allocator.release_event(ev, alloc_of[id(ev)])
    allocator.finalize()
    # 稳定排序保证严格有序；同一时刻起音类事件排在释放类之前
    kind_order = {"note_on": 0, "bend": 1, "pending_held": 2,
                  "note_off": 3, "pending_cancel": 3, "release_done": 4}
    allocator.timeline.sort(key=lambda e: (e.time, kind_order.get(e.kind, 9)))
    return allocator, allocator.timeline
