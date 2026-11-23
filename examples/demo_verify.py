#!/usr/bin/env python3
"""端到端演示：不同周期/近邻音阶样例 -> 渲染 WAV -> 信号分析核对。

运行：python3 -I examples/demo_verify.py
不依赖任何第三方库，产物写入 output/。
"""

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from microtone.core import (
    builtin_systems,
    parse_score,
    render_score,
    write_wav,
    write_wav_stereo_compare,
    verify_render,
    chord_ratios,
    compare_tuning_mappings,
)

OUT = os.path.join(ROOT, "output")
SR = 44100


def line(s=""):
    print(s)


def demo_tuning_frequencies():
    line("=" * 72)
    line("1) 不同周期体系的真实频率（参考频率、周期比、重复规则独立）")
    line("=" * 72)
    for name in ("12-TET", "24-TET", "just", "bohlen-pierce"):
        t = builtin_systems()[name]
        line(f"\n[{t.name}] 周期 {t.period_steps} 级 / 比例 {t.period_ratio}, "
             f"参考 {t.ref_freq:.3f}Hz")
        for d in range(0, min(t.period_steps, 8)):
            r = t.degree_ratio(d)
            line(f"  deg{d:>2} {t.label_of(d):>7} "
                 f"{t.degree_to_freq(d):>11.4f}Hz  比例 {str(r):>10} "
                 f"({1200*math.log2(float(r)):+.2f}c)")


def demo_render_and_verify(score_file: str, tuning_name: str, tag: str):
    line("\n" + "=" * 72)
    line(f"2) 渲染并信号核对：{score_file} @ {tuning_name}")
    line("=" * 72)
    score = parse_score(open(score_file, encoding="utf-8").read())
    t = builtin_systems()[tuning_name]
    result = render_score(score, t, sample_rate=SR)
    wav_path = os.path.join(OUT, f"demo_{tag}.wav")
    write_wav(wav_path, result["pcm"], SR)
    line(f"WAV: {wav_path}")
    for c in verify_render(result):
        flag = "OK  " if c["pass"] else "FAIL"
        poly = "复音" if c["polyphonic"] else "单音"
        line(f"  [{flag}] {poly} deg{c['degree']:>3} "
             f"期望={c['expected_hz']:>10.4f}Hz "
             f"实测={c['estimated_hz']:>10.4f}Hz "
             f"误差={c['error_cents']:+.4f}c")
    # 和弦比例与拍频
    by_time = {}
    for m in result["mapping"]:
        by_time.setdefault(m["time"], []).append(m["freq_hz"])
    for tt, freqs in sorted(by_time.items()):
        if len(freqs) >= 2:
            info = chord_ratios(freqs)
            line(f"  和弦 t={tt:.2f}s:")
            for v in info["voices"]:
                line(f"    {v['freq_hz']:>10.4f}Hz  比例 "
                     f"{v['approx_simple_ratio']:>7} "
                     f"({v['cents_vs_lowest']:+.2f}c)")
            for p in info["pair_beats"]:
                line(f"    拍频 {p['pair'][0]:.2f}/"
                     f"{p['pair'][1]:.2f} = {p['beat_hz']:.3f}Hz")
    for e in result["out_of_range"]:
        line(f"  超范围静音: {e}")
    return result


def demo_compare():
    line("\n" + "=" * 72)
    line("3) 同一谱面不同调律 A/B 对比（12-TET vs 19-TET，立体声 WAV）")
    line("=" * 72)
    score = parse_score(
        "bpm 96\nchord 0,4,7 2.5\nrest 0.5\nnote 0 1.0\nnote 4 1.5\n")
    ra = render_score(score, builtin_systems()["12-TET"], sample_rate=SR)
    rb = render_score(score, builtin_systems()["19-TET"], sample_rate=SR)
    path = os.path.join(OUT, "demo_compare_12_19_stereo.wav")
    write_wav_stereo_compare(path, ra["pcm"], rb["pcm"], SR)
    line(f"立体声 WAV（左 12-TET / 右 19-TET）: {path}")
    for row in compare_tuning_mappings(ra["mapping"], rb["mapping"]):
        line(f"  t={row['time']:5.2f}s deg{row['degree']:>2} "
             f"A={row['freq_a_hz']:9.4f} B={row['freq_b_hz']:9.4f} "
             f"Δ={row['delta_hz']:+9.4f}Hz ({row['delta_cents']:+8.3f}c)")


def demo_policy_conflict():
    line("\n" + "=" * 72)
    line("4) 滑音跨微分音级时调律切换：hold_freq 与 hold_ratio 的冲突")
    line("=" * 72)
    from microtone.core import VoiceManager
    for policy in ("hold_freq", "hold_ratio"):
        vm = VoiceManager(builtin_systems()["just"], sample_rate=SR,
                          policy=policy)
        v = vm.note_on(2)
        before = v.freq
        report = vm.apply_tuning_change(builtin_systems()["12-TET"])
        r = report[0]
        line(f"  policy={policy:<11} 纯律E {before:.4f}Hz -> "
             f"{r['new_freq_hz']:.4f}Hz (Δ {r['freq_delta_hz']:+.4f}Hz)")


def main():
    os.makedirs(OUT, exist_ok=True)
    demo_tuning_frequencies()
    demo_render_and_verify(
        os.path.join(ROOT, "examples", "quarter_tone_demo.score"),
        "24-TET", "quarter_tone")
    demo_render_and_verify(
        os.path.join(ROOT, "examples", "bohlen_pierce_demo.score"),
        "bohlen-pierce", "bohlen_pierce")
    demo_compare()
    demo_policy_conflict()
    line("\n全部演示完成。WAV 位于 output/，可用任意播放器试听。")


if __name__ == "__main__":
    main()
