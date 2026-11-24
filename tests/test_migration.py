"""调律资料迁移测试：旧弯音通道 -> 显式比值新格式。

运行：python3 -I tests/test_migration.py

覆盖的关键契约：
* 同时起音的两个音不共用后续弯音，通道不足时改派或保留待处理；
* 合成器回报通道仍在释放时不能复用，待处理音在释放结束后补发；
* 超出旧弯音范围的和弦逐音标记，可表达部分频率完整保留；
* 新映射频率与旧资料真实目标频率一致，并由实际渲染信号核对；
* 非十二等分偏移以比值保留，无法表达的旧偏移被报告；
* hold_freq 与安全换气点切换在持续音上的差异；
* 净目录与构建锁约束。
"""

import math
import os
import shutil
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microtone.core import TuningSystem, parse_score, render_score, verify_render
from microtone.migration import (
    LegacyTuning,
    LegacyScore,
    LegacyEvent,
    parse_legacy_score,
    load_legacy_tuning,
    BendChannelAllocator,
    simulate_channel_events,
    migrate_tuning,
    compare_switch_strategies,
    FORMAT_VERSION,
    write_format_lock,
    assert_lock_current,
    ensure_clean_output_dir,
)
from microtone.migration.channels import IDLE, ACTIVE, RELEASING

LEGACY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "legacy")


def make_tuning(channels=4, bend=200.0, offsets=None, ref_note=60):
    return LegacyTuning(
        name="t", ref_freq=261.625565, ref_midi_note=ref_note,
        bend_range_cents=bend, channel_count=channels,
        offsets_cents=offsets or {})


def ev(t, dur, note, vel=1.0):
    return LegacyEvent(time=t, duration=dur, midi_note=note, velocity=vel)


def _read(name):
    with open(os.path.join(LEGACY_DIR, name), encoding="utf-8") as f:
        return f.read()


class TestLegacyFrequencyModel(unittest.TestCase):
    def test_target_freq_includes_cents_offset(self):
        t = make_tuning(offsets={61: 50.0})
        # 61 比 60 高一个半音再加 50 音分 => 150 音分
        expected = 261.625565 * 2 ** (150 / 1200)
        self.assertAlmostEqual(t.target_freq(61), expected, places=10)
        # 未列出的级偏移为 0
        self.assertAlmostEqual(t.target_freq(60), 261.625565, places=10)

    def test_representability_uses_bend_range(self):
        t = make_tuning(bend=100.0, offsets={62: -13.686, 76: 150.0})
        self.assertTrue(t.is_representable(62))
        self.assertFalse(t.is_representable(76))

    def test_legacy_roundtrip_dict(self):
        t = make_tuning(offsets={61: 50.0})
        t2 = LegacyTuning.from_dict(t.to_dict())
        self.assertEqual(t2.bend_range_cents, t.bend_range_cents)
        self.assertAlmostEqual(t2.target_freq(61), t.target_freq(61), places=12)

    def test_reject_unknown_legacy_version(self):
        with self.assertRaises(ValueError):
            LegacyTuning.from_dict(
                {"format_version": "nope", "name": "x",
                 "ref_freq": 261.0, "offsets_cents": {}})


class TestChannelAllocation(unittest.TestCase):
    def test_same_onset_two_notes_get_separate_channels_and_bends(self):
        t = make_tuning(channels=4, offsets={60: 0.0, 61: 50.0})
        score = LegacyScore(events=[ev(0.0, 1.0, 60), ev(0.0, 1.0, 61)])
        allocator, timeline = simulate_channel_events(t, score)
        on_events = [e for e in timeline if e.kind == "note_on"]
        self.assertEqual(len(on_events), 2)
        channels = {e.channel for e in on_events}
        # 起音相同也必须分到不同通道，不能共用一条后续弯音
        self.assertEqual(len(channels), 2)
        bends = {e.midi_note: e.bend_cents for e in on_events}
        self.assertEqual(bends, {60: 0.0, 61: 50.0})
        # 只有非零弯音（61）下发 bend，且弯音落在它自己的通道上，不串到 60
        bend_events = [e for e in timeline if e.kind == "bend"]
        self.assertEqual(len(bend_events), 1)
        self.assertEqual(bend_events[0].midi_note, 61)
        self.assertEqual(bend_events[0].channel,
                         next(e.channel for e in on_events if e.midi_note == 61))
        self.assertNotEqual(bend_events[0].channel,
                            next(e.channel for e in on_events if e.midi_note == 60))

    def test_releasing_channel_not_reused_until_release_done(self):
        # 两通道；第一个音在 t=1 收到 note-off，但释放到 1.12 才结束
        t = make_tuning(channels=2, offsets={60: 0.0, 61: 10.0, 62: 20.0})
        score = LegacyScore(events=[
            ev(0.0, 1.0, 60), ev(0.0, 1.0, 61),
            ev(1.05, 0.5, 62),  # 此刻两通道仍在释放（0.12s 释放）
        ])
        allocator, timeline = simulate_channel_events(
            t, score, release_seconds=0.12)
        # 第三个音先 pending
        held = [e for e in timeline if e.kind == "pending_held"]
        self.assertTrue(held)
        alloc62 = next(a for a in allocator.allocations
                       if a.event.midi_note == 62)
        self.assertTrue(alloc62.held_pending)
        # 释放完成事件早于补发
        releases = [e for e in timeline if e.kind == "release_done"]
        flush = next(e for e in timeline
                     if e.kind == "note_on" and e.midi_note == 62
                     and e.time >= 1.05)
        self.assertLessEqual(min(e.time for e in releases), flush.time)
        # 补发拿到的是已归还的空闲通道
        self.assertIn(flush.channel, (0, 1))

    def test_pending_note_kept_not_share_bend_then_flushed(self):
        # 单通道：第二个同时音必须 pending，不能共享通道弯音
        t = make_tuning(channels=1, offsets={60: 0.0, 61: 50.0})
        # 61 持续到 1.5，越过 60 的释放结束时刻 1.1，故能在 1.1 补发
        score = LegacyScore(events=[ev(0.0, 1.0, 60), ev(0.0, 1.5, 61)])
        allocator, timeline = simulate_channel_events(
            t, score, release_seconds=0.1)
        a60 = next(a for a in allocator.allocations
                   if a.event.midi_note == 60)
        a61 = next(a for a in allocator.allocations
                   if a.event.midi_note == 61)
        self.assertEqual(a60.channel, 0)
        # 61 起音时无空闲通道：曾进入 pending，绝不共享 60 的弯音
        self.assertTrue(a61.held_pending)
        self.assertIsNotNone(a61.flushed_at)
        # 时间线中先有 61 的 pending_held，后才有补发 note_on
        self.assertTrue(any(e.kind == "pending_held" and e.midi_note == 61
                            for e in timeline))
        # 61 在 1.1 补发（60 在 1.0 note-off + 0.1 释放）
        flush = next(e for e in timeline
                     if e.kind == "note_on" and e.midi_note == 61
                     and e.time >= 1.05)
        self.assertAlmostEqual(flush.time, 1.1, places=9)
        self.assertEqual(flush.channel, 0)
        self.assertAlmostEqual(flush.bend_cents, 50.0)
        self.assertAlmostEqual(a61.flushed_at, 1.1, places=9)

    def test_note_ending_while_pending_never_occupies_channel(self):
        t = make_tuning(channels=1, offsets={60: 0.0, 61: 50.0})
        # 61 的结束时间早于 60 释放完成，pending 期间就结束了
        score = LegacyScore(events=[ev(0.0, 2.0, 60), ev(0.0, 0.3, 61)])
        allocator, timeline = simulate_channel_events(
            t, score, release_seconds=0.1)
        a61 = next(a for a in allocator.allocations
                   if a.event.midi_note == 61)
        self.assertIsNone(a61.channel)
        # 没有 61 的 note_on 补发，但有撤销记录
        self.assertFalse(any(e.kind == "note_on" and e.midi_note == 61
                             and e.time > 0 for e in timeline))
        self.assertTrue(any(e.kind == "pending_cancel" and e.midi_note == 61
                            and e.channel == -1 for e in timeline))

    def test_unrepresentable_note_marked_never_sounds_on_legacy(self):
        t = make_tuning(channels=2, bend=100.0, offsets={76: 150.0})
        score = LegacyScore(events=[ev(0.0, 1.0, 76)])
        allocator, timeline = simulate_channel_events(t, score)
        alloc = allocator.allocations[0]
        self.assertFalse(alloc.representable)
        self.assertIsNone(alloc.channel)
        self.assertTrue(any(e.kind == "pending_held" and e.midi_note == 76
                            for e in timeline))
        self.assertFalse(any(e.kind == "note_on" for e in timeline))

    def test_shared_bend_history_one_releasing_redirects_or_holds(self):
        # 需求场景：两个音共用一条旧弯音通道（这里通道数=1 强制串行），
        # 第一音仍在释放时第三音到达，必须改派/待处理，不能共用后续弯音。
        t = make_tuning(channels=1,
                        offsets={60: 0.0, 61: 50.0, 62: -30.0})
        score = LegacyScore(events=[
            ev(0.0, 1.0, 60), ev(0.0, 1.0, 61),   # 61 先 pending
            ev(1.05, 0.5, 62),                    # 60 释放中（到 1.12）
        ])
        allocator, timeline = simulate_channel_events(
            t, score, release_seconds=0.12)
        by_note = {a.event.midi_note: a for a in allocator.allocations}
        # 60 独占通道；61、62 都经历过 pending
        self.assertEqual(by_note[60].channel, 0)
        self.assertTrue(by_note[61].held_pending)
        self.assertTrue(by_note[62].held_pending)
        # 任一在发音的后续 bend 只属于它自己的 note，不出现一条 bend 同时
        # 服务两个在发音（每个 note_on 的通道在该时刻唯一占用）
        active_window = {}
        for e in timeline:
            if e.kind == "note_on":
                active_window.setdefault(e.channel, []).append(e.midi_note)
        # 通道 0 上的每次起音都是不同音，但时间上互不重叠（释放后才复用）
        onsets = [e for e in timeline if e.kind == "note_on"]
        for earlier, later in zip(onsets, onsets[1:]):
            release_done = next((e for e in timeline
                                 if e.kind == "release_done"
                                 and e.time <= later.time
                                 and e.channel == earlier.channel), None)
            self.assertIsNotNone(
                release_done,
                "复用通道前必须先收到释放结束，禁止共用后续弯音")
        # 62 在 1.12 通道归还后补发，弯音是它自己的 -30c
        flush62 = next(e for e in timeline
                       if e.kind == "note_on" and e.midi_note == 62)
        self.assertAlmostEqual(flush62.time, 1.12, places=9)
        self.assertAlmostEqual(flush62.bend_cents, -30.0)


class TestMigrator(unittest.TestCase):
    def _quarter(self):
        legacy = load_legacy_tuning(os.path.join(
            LEGACY_DIR, "quarter_tone_bend.legacy.json"))
        score = parse_legacy_score(
            _read("quarter_tone_bend.legacy.score"))
        return legacy, score

    def test_new_tuning_preserves_non_12edo_meaning_as_ratios(self):
        legacy, score = self._quarter()
        manifest = migrate_tuning(legacy, score)
        tuning = TuningSystem.from_dict(manifest.tuning)
        # 61 级是 12-TET 不存在的四分音：比例对应 +150 音分而非整数半音
        n61 = next(n for n in manifest.notes if n.legacy_midi_note == 61)
        ratio = Fraction(n61.ratio_vs_ref)
        self.assertAlmostEqual(
            1200 * math.log2(float(ratio)), 150.0, places=6)
        # 新格式发声频率等于旧资料真实目标频率
        self.assertTrue(math.isclose(
            tuning.degree_to_freq(n61.new_degree),
            legacy.target_freq(61), rel_tol=1e-9))

    def test_frequency_contract_and_signal_verification(self):
        legacy, score = self._quarter()
        manifest = migrate_tuning(legacy, score, sample_rate=22050)
        self.assertTrue(manifest.frequency_contract_ok)
        self.assertTrue(manifest.verification)
        self.assertTrue(all(v["pass"] for v in manifest.verification))
        for v in manifest.verification:
            self.assertTrue(v["frequency_contract_ok"])
            self.assertLess(abs(v["error_vs_legacy_cents"]), 3.0)
            self.assertLess(abs(v["error_vs_new_cents"]), 3.0)

    def test_migrated_score_renders_through_new_path(self):
        legacy, score = self._quarter()
        manifest = migrate_tuning(legacy, score, sample_rate=22050)
        tuning = TuningSystem.from_dict(manifest.tuning)
        new_score = parse_score(manifest.migrated_score_text)
        result = render_score(new_score, tuning, sample_rate=22050)
        checks = verify_render(result)
        self.assertTrue(checks)
        self.assertTrue(all(c["pass"] for c in checks))
        # 迁移谱面的事件数与旧谱面一致（和弦展开后）
        self.assertEqual(len(result["mapping"]), len(score.events))

    def test_unrepresentable_chord_marks_specific_notes_keeps_rest(self):
        legacy = load_legacy_tuning(os.path.join(
            LEGACY_DIR, "just_narrow_bend.legacy.json"))
        score = parse_legacy_score(
            _read("narrow_bend_unrepresentable.legacy.score"))
        manifest = migrate_tuning(legacy, score)
        bad = {u.legacy_midi_note for u in manifest.unrepresentable}
        self.assertEqual(bad, {76})
        # 不可表达音在新映射里仍保留真实频率
        n76 = next(n for n in manifest.notes if n.legacy_midi_note == 76)
        self.assertAlmostEqual(n76.new_mapped_hz,
                               legacy.target_freq(76), places=9)
        self.assertFalse(n76.representable_on_legacy)
        # 和弦警告逐音点名，且可表达部分（60、67）频率被保留
        self.assertTrue(manifest.warnings)
        msg = manifest.warnings[0]
        self.assertIn("76", msg)
        self.assertIn("261.626", msg)   # 60 的频率
        self.assertIn("392.882", msg)   # 67 的频率

    def test_manifest_versions_stamped(self):
        legacy, score = self._quarter()
        manifest = migrate_tuning(legacy, score)
        self.assertEqual(manifest.format_version, FORMAT_VERSION)
        self.assertEqual(manifest.legacy_format_version, "legacy-bend/1")

    def test_degree_map_covers_all_events(self):
        legacy, score = self._quarter()
        from microtone.migration.migrator import build_new_tuning, convert_events
        new_tuning, dmap = build_new_tuning(legacy, score)
        new_score, notes = convert_events(legacy, score, dmap)
        self.assertEqual(len(notes), len(score.events))
        self.assertEqual(set(dmap),
                         {e.midi_note for e in score.events}
                         | {legacy.ref_midi_note}
                         | set(legacy.offsets_cents))


class TestSwitchStrategies(unittest.TestCase):
    def test_hold_freq_vs_hold_ratio_conflict_on_sustained_voice(self):
        legacy = load_legacy_tuning(os.path.join(
            LEGACY_DIR, "quarter_tone_bend.legacy.json"))
        score = parse_legacy_score(
            _read("quarter_tone_bend.legacy.score"))
        from microtone.migration.migrator import build_new_tuning
        new_tuning, dmap = build_new_tuning(legacy, score)
        # 构造一个与旧目标不同的“新版调律”：整体拉伸 5 音分
        shifted = TuningSystem(
            name="shifted",
            ref_freq=new_tuning.ref_freq * 2 ** (5 / 1200),
            ref_degree=new_tuning.ref_degree,
            period_steps=new_tuning.period_steps,
            period_ratio=new_tuning.period_ratio,
            scale=list(new_tuning.scale),
            labels=list(new_tuning.labels))
        cmp = compare_switch_strategies(
            legacy, score, new_tuning, shifted, dmap, switch_time=0.0)
        self.assertTrue(cmp.hold_ratio)
        for hf, hr, ss in zip(cmp.hold_freq, cmp.hold_ratio,
                              cmp.safe_switch):
            # hold_freq：持续音物理频率不动
            self.assertEqual(hf.delta_hz, 0.0)
            # hold_ratio：跟随新版调律，频率整体上移
            self.assertAlmostEqual(hr.delta_cents, 5.0, places=6)
            # 安全换气点：切换瞬间不拽动持续音
            self.assertEqual(ss.delta_hz, 0.0)
        # 安全点必须是没有在发音的时刻
        for sp in cmp.safe_points:
            self.assertFalse(any(
                e.time - 1e-12 <= sp.time < e.end_time - 1e-12
                for e in score.events))

    def test_safe_point_after_all_voices_released(self):
        legacy = make_tuning(offsets={60: 0.0})
        score = LegacyScore(events=[ev(0.0, 1.0, 60)])
        from microtone.migration.migrator import build_new_tuning
        new_tuning, dmap = build_new_tuning(legacy, score)
        cmp = compare_switch_strategies(
            legacy, score, new_tuning, new_tuning, dmap, switch_time=0.0)
        self.assertAlmostEqual(cmp.safe_points[0].time, 1.0, places=9)


class TestLockAndCleanDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="miglock_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_dir_required(self):
        target = os.path.join(self.tmp, "out")
        ensure_clean_output_dir(target)
        self.assertTrue(os.path.isdir(target))
        with open(os.path.join(target, "stale.json"), "w") as f:
            f.write("{}")
        with self.assertRaises(FileExistsError):
            ensure_clean_output_dir(target)

    def test_lock_write_and_assert(self):
        path = write_format_lock(self.tmp)
        self.assertTrue(os.path.exists(path))
        lock = assert_lock_current(self.tmp)
        self.assertEqual(lock["new_format_version"], FORMAT_VERSION)

    def test_tampered_lock_rejected(self):
        write_format_lock(self.tmp)
        import json
        lock_path = os.path.join(self.tmp, "tuning-format.lock.json")
        data = json.load(open(lock_path))
        data["new_format_version"] = "tampered/9"
        with open(lock_path, "w") as f:
            json.dump(data, f)
        with self.assertRaises(ValueError):
            assert_lock_current(self.tmp)


class TestLegacyExampleRunsToEnd(unittest.TestCase):
    def test_both_shipped_legacy_examples(self):
        pairs = [
            ("quarter_tone_bend.legacy.json",
             "quarter_tone_bend.legacy.score"),
            ("just_narrow_bend.legacy.json",
             "narrow_bend_unrepresentable.legacy.score"),
        ]
        for lt, ls in pairs:
            legacy = load_legacy_tuning(os.path.join(LEGACY_DIR, lt))
            score = parse_legacy_score(_read(ls))
            manifest = migrate_tuning(legacy, score, sample_rate=16000)
            self.assertTrue(manifest.verification, lt)
            self.assertTrue(all(v["pass"] for v in manifest.verification), lt)
            self.assertTrue(manifest.channel_timeline, lt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
