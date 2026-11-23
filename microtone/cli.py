#!/usr/bin/env python3
"""命令行辅助工具（图形界面才是主入口，见运行说明）。

用法：
  python3 microtone/cli.py list
  python3 cli.py table <tuning> [--lo -7 --hi 14]
  python3 cli.py render <score文件> --tuning 12-TET --out output/x.wav
  python3 cli.py compare <score文件> --a 12-TET --b 19-TET --out output/ab.wav
  python3 cli.py verify <score文件> --tuning just
  python3 cli.py export-tunings <目录>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from microtone.core import (
    builtin_systems,
    parse_score,
    render_score,
    write_wav,
    write_wav_stereo_compare,
    verify_render,
    compare_tuning_mappings,
    chord_ratios,
    TuningSystem,
)


def _load_tuning(name_or_path: str):
    systems = builtin_systems()
    if name_or_path in systems:
        return systems[name_or_path]
    if os.path.exists(name_or_path):
        with open(name_or_path, "r", encoding="utf-8") as f:
            return TuningSystem.from_json(f.read())
    raise SystemExit(f"找不到调律: {name_or_path}（内置: {', '.join(systems)})")


def cmd_list(_args):
    for key, t in builtin_systems().items():
        print(f"{key:14s} {t.name:32s} "
              f"周期={t.period_steps}级/{t.period_ratio} 参考={t.ref_freq:.2f}Hz")


def cmd_table(args):
    t = _load_tuning(args.tuning)
    print(f"# {t.name}  参考级 {t.ref_degree}={t.ref_freq:.4f}Hz  "
          f"周期比 {t.period_ratio} / {t.period_steps} 级")
    for row in t.freq_table(args.lo, args.hi):
        print(f"deg {row['degree']:>3} {row['label']:>6}  "
              f"{row['freq_hz']:>12.4f} Hz  比例 {row['ratio']:>10}  "
              f"{row['cents_vs_ref']:>9.3f}c")


def cmd_render(args):
    score = parse_score(open(args.score, encoding="utf-8").read())
    tuning = _load_tuning(args.tuning)
    result = render_score(score, tuning)
    write_wav(args.out, result["pcm"], result["sample_rate"])
    print(f"已导出 {args.out}（{len(result['pcm'])/result['sample_rate']:.2f}s）")
    for m in result["mapping"]:
        extra = ""
        if "gliss_to_freq_hz" in m:
            extra = f" -> {m['gliss_to_freq_hz']:.3f}Hz"
        print(f"  t={m['time']:6.3f}s deg={m['degree']:>3} "
              f"{m.get('label',''):>5} {m['freq_hz']:10.4f}Hz "
              f"比例={m['ratio']}{extra}")
    if result["out_of_range"]:
        print("超出音源有效范围（已静音）:")
        for e in result["out_of_range"]:
            print(" ", e)


def cmd_compare(args):
    score = parse_score(open(args.score, encoding="utf-8").read())
    ta = _load_tuning(args.a)
    tb = _load_tuning(args.b)
    ra = render_score(score, ta)
    rb = render_score(score, tb)
    write_wav_stereo_compare(args.out, ra["pcm"], rb["pcm"],
                             ra["sample_rate"])
    print(f"立体声 A/B 已导出 {args.out}（左={ta.name} 右={tb.name}）")
    for row in compare_tuning_mappings(ra["mapping"], rb["mapping"]):
        print(f"  t={row['time']:6.3f}s deg={row['degree']:>3} "
              f"A={row['freq_a_hz']:9.4f}Hz B={row['freq_b_hz']:9.4f}Hz "
              f"Δ={row['delta_hz']:+8.4f}Hz ({row['delta_cents']:+7.3f}c)")


def cmd_verify(args):
    score = parse_score(open(args.score, encoding="utf-8").read())
    tuning = _load_tuning(args.tuning)
    result = render_score(score, tuning)
    checks = verify_render(result)
    ok = True
    print(f"# 信号分析核对：{tuning.name}")
    for c in checks:
        flag = "OK " if c["pass"] else "FAIL"
        ok = ok and c["pass"]
        print(f"  [{flag}] deg={c['degree']:>3} {c.get('label',''):>5} "
              f"期望={c['expected_hz']:9.4f}Hz 实测={c['estimated_hz']:9.4f}Hz "
              f"误差={c['error_cents']:+.4f}c")
    # 和弦核对
    by_time = {}
    for m in result["mapping"]:
        by_time.setdefault(m["time"], []).append(m["freq_hz"])
    for t, freqs in sorted(by_time.items()):
        if len(freqs) >= 2:
            info = chord_ratios(freqs)
            print(f"  和弦 t={t:.2f}s:")
            for v in info["voices"]:
                print(f"    {v['freq_hz']:9.4f}Hz 比例="
                      f"{v['approx_simple_ratio']:>7} "
                      f"({v['cents_vs_lowest']:+.2f}c)")
            for p in info["pair_beats"]:
                print(f"    拍频 {p['pair'][0]:.2f}/{p['pair'][1]:.2f} "
                      f"= {p['beat_hz']:.3f}Hz")
    raise SystemExit(0 if ok else 1)


def cmd_export(args):
    os.makedirs(args.dir, exist_ok=True)
    for key, t in builtin_systems().items():
        path = os.path.join(args.dir, f"{key}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(t.to_json())
        print("写出", path)


def main():
    p = argparse.ArgumentParser(description="微分音调律 CLI（辅助，主入口为图形界面）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    pt = sub.add_parser("table")
    pt.add_argument("tuning")
    pt.add_argument("--lo", type=int, default=-7)
    pt.add_argument("--hi", type=int, default=14)
    pt.set_defaults(func=cmd_table)

    pr = sub.add_parser("render")
    pr.add_argument("score")
    pr.add_argument("--tuning", required=True)
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_render)

    pc = sub.add_parser("compare")
    pc.add_argument("score")
    pc.add_argument("--a", required=True)
    pc.add_argument("--b", required=True)
    pc.add_argument("--out", required=True)
    pc.set_defaults(func=cmd_compare)

    pv = sub.add_parser("verify")
    pv.add_argument("score")
    pv.add_argument("--tuning", required=True)
    pv.set_defaults(func=cmd_verify)

    pe = sub.add_parser("export-tunings")
    pe.add_argument("dir")
    pe.set_defaults(func=cmd_export)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
