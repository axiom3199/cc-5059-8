"""微分音核心测试：全部可离线运行，python3 -I tests/test_core.py。"""

import math
import os
import sys
import unittest
import wave
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microtone.core import (
    TuningSystem,
    builtin_systems,
    equal_temperament,
    just_scale,
    bohlen_pierce,
    parse_score,
    render_score,
    write_wav,
    VoiceManager,
    verify_render,
    chord_ratios,
    estimate_fundamental,
    compare_tuning_mappings,
    ratio_to_cents,
)


class TestTuning(unittest.TestCase):
    def test_12tet_semitone_ratio(self):
        t = equal_temperament(12)
        # 相邻半音比例 = 2^(1/12)
        self.assertAlmostEqual(
            t.degree_to_freq(1) / t.degree_to_freq(0),
            2 ** (1 / 12), places=12)
        # 十二级后精确八度
        self.assertAlmostEqual(t.degree_to_freq(12) / t.degree_to_freq(0),
                               2.0, places=12)

    def test_negative_degrees_and_periods(self):
        t = just_scale()
        # 负级数：低八度 C
        self.assertAlmostEqual(t.degree_to_freq(-7),
                               t.ref_freq / 2, places=10)
        # 跨两个周期
        self.assertAlmostEqual(t.degree_to_freq(14),
                               t.ref_freq * 4, places=10)
        # 精确分数比例
        self.assertEqual(t.degree_ratio(4), Fraction(3, 2))

    def test_just_vs_12tet_major_third_audible_gap(self):
        just = just_scale()
        tet = equal_temperament(12)
        just_third = ratio_to_cents(just.degree_ratio(2))
        tet_third = ratio_to_cents(tet.degree_ratio(4))
        # 纯律大三度 386.31c vs 平均律 400c，差约 13.7c，真实可闻
        self.assertAlmostEqual(just_third, 386.3137, places=2)
        self.assertAlmostEqual(tet_third, 400.0, places=2)
        self.assertAlmostEqual(tet_third - just_third, 13.686, places=1)

    def test_bohlen_pierce_non_octave(self):
        bp = bohlen_pierce()
        # 周期比是 3/1 而非 2/1
        self.assertEqual(bp.period_ratio, Fraction(3, 1))
        self.assertEqual(bp.period_steps, 13)
        self.assertAlmostEqual(bp.degree_to_freq(13) / bp.degree_to_freq(0),
                               3.0, places=10)
        # 十三级内不应该出现精确八度
        for k in range(1, 13):
            self.assertNotAlmostEqual(
                bp.degree_to_freq(k) / bp.degree_to_freq(0), 2.0, places=2)

    def test_ref_freq_independent(self):
        a = just_scale(261.63)
        b = just_scale(440.0)
        ratio = b.degree_to_freq(4) / a.degree_to_freq(4)
        self.assertAlmostEqual(ratio, 440.0 / 261.63, places=10)

    def test_json_roundtrip(self):
        t = builtin_systems()["meantone"]
        t2 = TuningSystem.from_json(t.to_json())
        self.assertEqual(t2.period_ratio, t.period_ratio)
        self.assertEqual(t2.period_steps, t.period_steps)
        self.assertAlmostEqual(t2.degree_to_freq(3),
                               t.degree_to_freq(3), places=12)

    def test_power_expression_ratio(self):
        # "2^(1/5)" 形式的无理比例，用于自定义 N 平均律
        t = TuningSystem(
            name="custom-5", ref_freq=261.63, ref_degree=0,
            period_steps=5, period_ratio="2/1",
            scale=["1", "2^(1/5)", "2^(2/5)", "2^(3/5)", "2^(4/5)"],
            labels=["a", "b", "c", "d", "e"])
        self.assertAlmostEqual(t.degree_to_freq(5) / t.degree_to_freq(0),
                               2.0, places=10)
        self.assertAlmostEqual(
            t.degree_to_freq(1) / t.degree_to_freq(0),
            2 ** 0.2, places=8)


class TestScoreAndRender(unittest.TestCase):
    def test_dsl_parse_timing(self):
        s = parse_score("bpm 120\nnote 0 1.0\nnote 2 2.0\nrest 1.0\n")
        self.assertAlmostEqual(s.beat_seconds, 0.5)
        self.assertAlmostEqual(s.events[0].time, 0.0)
        self.assertAlmostEqual(s.events[1].time, 0.5)
        self.assertAlmostEqual(s.length_seconds, 2.0)

    def test_render_matches_tuning_freq(self):
        s = parse_score("bpm 120\nnote 0 1.0\nnote 4 1.0\n")
        result = render_score(s, just_scale(), sample_rate=22050)
        checks = verify_render(result, tolerance_cents=3.0)
        self.assertEqual(len(checks), 2)
        for c in checks:
            self.assertTrue(c["pass"], c)

    def test_polyphonic_chord_each_voice_verified(self):
        s = parse_score("bpm 120\nchord 0,2,4 2.0\n")
        result = render_score(s, just_scale(), sample_rate=22050)
        checks = verify_render(result, tolerance_cents=4.0)
        self.assertEqual(len(checks), 3)
        self.assertTrue(all(c["polyphonic"] for c in checks))
        for c in checks:
            self.assertTrue(c["pass"], c)

    def test_chord_exact_ratios(self):
        t = just_scale()
        freqs = [t.degree_to_freq(d) for d in (0, 2, 4)]
        info = chord_ratios(freqs)
        ratios = [v["approx_simple_ratio"] for v in info["voices"]]
        self.assertEqual(ratios, ["1", "5/4", "3/2"])

    def test_glissando_endpoint_and_continuity(self):
        s = parse_score("bpm 120\ngliss 0 6 2.0 steps 24\n")
        t = quarter = builtin_systems()["24-TET"]
        result = render_score(s, t, sample_rate=22050)
        m = result["mapping"][0]
        # 端点核对
        checks = verify_render(result, tolerance_cents=5.0)
        self.assertTrue(checks and checks[0]["pass"], checks)
        # 连续性：扫频中段频率应落在两端点对数插值上。
        # 扫频段频率时变，必须用短窗（约 20ms）换取时间分辨率，
        # 长窗会因谱峰拖尾产生固定正向偏差。
        sr = result["sample_rate"]
        pcm = result["pcm"]
        dur = m["duration"]
        p = 0.5
        mid_time = m["time"] + dur * 0.70 * p
        s0 = int(mid_time * sr)
        mid_seg = pcm[s0:s0 + int(0.05 * sr)]
        expected_mid = m["freq_hz"] * (
            (m["gliss_to_freq_hz"] / m["freq_hz"]) ** p)
        est = estimate_fundamental(
            mid_seg, sr,
            f_min=expected_mid * 0.85, f_max=expected_mid * 1.15)
        err = abs(1200 * math.log2(est["freq_hz"] / expected_mid))
        self.assertLess(err, 15.0)

    def test_wav_export_header(self):
        out = os.path.join(os.path.dirname(__file__), "_tmp.wav")
        s = parse_score("bpm 120\nnote 0 0.5\n")
        result = render_score(s, just_scale(), sample_rate=16000)
        write_wav(out, result["pcm"], 16000)
        with wave.open(out, "rb") as wf:
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
        os.remove(out)

    def test_out_of_range_muted(self):
        s = parse_score("bpm 120\nnote 60 1.0\n")
        result = render_score(s, equal_temperament(12),
                              freq_range=(20.0, 5000.0))
        self.assertTrue(result["out_of_range"])
        self.assertEqual(result["out_of_range"][0]["action"], "muted")
        self.assertTrue(all(abs(x) < 1e-9 for x in result["pcm"][:1000]))


class TestTuningChangePolicy(unittest.TestCase):
    def test_hold_freq_keeps_physical_frequency(self):
        just = just_scale()
        tet = equal_temperament(12)
        vm = VoiceManager(just, sample_rate=22050, policy="hold_freq")
        v = vm.note_on(2)  # 纯律 E
        f_before = v.freq
        report = vm.apply_tuning_change(tet)
        self.assertEqual(report[0]["policy"], "hold_freq")
        self.assertAlmostEqual(v.freq, f_before, places=12)
        self.assertAlmostEqual(v.locked_freq, f_before, places=12)

    def test_hold_ratio_retracks_new_tuning(self):
        just = just_scale()
        tet = equal_temperament(12)
        vm = VoiceManager(just, sample_rate=22050, policy="hold_ratio")
        v = vm.note_on(2)
        report = vm.apply_tuning_change(tet)
        # 同一“级数”概念在两个体系里不同：纯律 deg2=E；
        # hold_ratio 下声音移动到 12-TET 的 deg2（两个半音）
        self.assertAlmostEqual(v.freq, tet.degree_to_freq(2), places=12)
        self.assertNotAlmostEqual(v.freq,
                                  builtin_systems()["just"].degree_to_freq(2),
                                  places=2)
        self.assertEqual(report[0]["policy"], "hold_ratio")

    def test_policies_conflict_is_real(self):
        """两种策略在微分音级上必须给出不同的结果（冲突真实存在）。"""
        just = just_scale()
        tet = equal_temperament(12)
        vm1 = VoiceManager(just, policy="hold_freq")
        vm2 = VoiceManager(just, policy="hold_ratio")
        f1 = vm1.note_on(2).freq
        f2 = vm2.note_on(2).freq
        self.assertAlmostEqual(f1, f2, places=12)
        vm1.apply_tuning_change(tet)
        vm2.apply_tuning_change(tet)
        self.assertNotAlmostEqual(vm1.voices[2].freq,
                                  vm2.voices[2].freq, places=3)

    def test_gliss_voice_not_locked(self):
        t = builtin_systems()["24-TET"]
        vm = VoiceManager(t, policy="hold_freq")
        vm.note_on(0)
        self.assertTrue(vm.gliss_to(0, 6, 0.5))
        vm.voices[0].gliss_samples_left = 0
        vm.voices[0].freq = vm.voices[0].gliss_freq_to
        other = equal_temperament(12)
        report = vm.apply_tuning_change(other)
        # 滑音声音按 hold_ratio 重算而不是被旧频率锁死
        self.assertNotIn("locked", report[0]["policy"])

    def test_gliss_auto_creates_voice(self):
        t = builtin_systems()["24-TET"]
        vm = VoiceManager(t)
        # 没有按住源音时滑音也应自动起音并成功
        self.assertTrue(vm.gliss_to(3, 9, 0.5))
        self.assertIn(3, vm.voices)
        v = vm.voices[3]
        self.assertEqual(v.gliss_to_degree, 9)
        self.assertAlmostEqual(v.gliss_freq_to,
                               t.degree_to_freq(9), places=10)


class TestComparison(unittest.TestCase):
    def test_same_score_two_tunings_mapping_differs(self):
        # 两个同构的 N 平均律：级数语义一致，差异可精确预期
        text = "bpm 120\nchord 0,4,7 2.0\n"
        score = parse_score(text)
        ra = render_score(score, equal_temperament(12), sample_rate=22050)
        rb = render_score(score, equal_temperament(19), sample_rate=22050)
        rows = compare_tuning_mappings(ra["mapping"], rb["mapping"])
        deltas = {r["degree"]: r["delta_cents"] for r in rows}
        # 根音（参考级）恒等；第 4 级：12-TET=400c，19-TET=4*1200/19≈252.6c
        self.assertAlmostEqual(deltas[0], 0.0, places=6)
        self.assertAlmostEqual(deltas[4], 4 * 1200 / 19 - 400, places=1)
        # 全部事件都应有非零差异（除根音）
        self.assertTrue(all(
            abs(deltas[d]) > 1 for d in deltas if d != 0))

    def test_just_vs_tet_same_label_third(self):
        # 同一参考频率下，“大三度”这个实际音程在两体系中的频率不同
        just = just_scale()
        tet = equal_temperament(12)
        f_just = just.degree_to_freq(2)   # 5/4
        f_tet = tet.degree_to_freq(4)     # 2^(4/12)
        gap_cents = 1200 * math.log2(f_tet / f_just)
        self.assertAlmostEqual(gap_cents, 13.686, places=1)

    def test_quarter_tone_beats(self):
        # 24-TET 相邻两级相差 50 音分，在 ~262Hz 处拍频约 7.7Hz
        t = builtin_systems()["24-TET"]
        f0, f1 = t.degree_to_freq(0), t.degree_to_freq(1)
        self.assertAlmostEqual(1200 * math.log2(f1 / f0), 50.0, places=6)
        info = chord_ratios([f0, f1])
        self.assertAlmostEqual(info["pair_beats"][0]["beat_hz"],
                               f1 - f0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
