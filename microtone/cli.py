#!/usr/bin/env python3
"""命令行辅助工具（图形界面才是主入口，见运行说明）。

用法：
  python3 microtone/cli.py list
  python3 cli.py table <tuning> [--lo -7 --hi 14]
  python3 cli.py render <score文件> --tuning 12-TET --out output/x.wav
  python3 cli.py compare <score文件> --a 12-TET --b 19-TET --out output/ab.wav
  python3 cli.py verify <score文件> --tuning just
  python3 cli.py export-tunings <目录>
  python3 cli.py migrate <旧调律.json> <旧谱面.score> <净输出目录>
  python3 cli.py check-lock <迁移输出目录>
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
from microtone.migration import (
    load_legacy_tuning,
    parse_legacy_score,
    migrate_tuning,
    FORMAT_VERSION,
    write_format_lock,
    assert_lock_current,
    ensure_clean_output_dir,
    migration_header,
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


def cmd_migrate(args):
    """旧弯音资料 + 旧谱面 -> 净目录中的显式比值新格式（含锁与证据）。"""
    legacy = load_legacy_tuning(args.legacy_tuning)
    score = parse_legacy_score(open(args.legacy_score, encoding="utf-8").read())
    manifest = migrate_tuning(
        legacy, score, release_seconds=args.release,
        sample_rate=args.sample_rate)

    # 净目录约束：不允许在含手改/陈旧资料的目录里落迁移产物
    ensure_clean_output_dir(args.outdir)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_"
                        for c in legacy.name) or "migrated"
    tuning_path = os.path.join(args.outdir, f"{safe_name}.tuning.json")
    score_path = os.path.join(args.outdir, f"{safe_name}.score")
    manifest_path = os.path.join(args.outdir, f"{safe_name}.manifest.json")
    lock_path = write_format_lock(args.outdir)

    tuning_doc = {**migration_header(), **manifest.tuning}
    with open(tuning_path, "w", encoding="utf-8") as f:
        json.dump(tuning_doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(score_path, "w", encoding="utf-8") as f:
        f.write(manifest.migrated_score_text)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"迁移格式版本: {FORMAT_VERSION}")
    print(f"输出目录（净目录）: {args.outdir}")
    print("  锁文件:", lock_path)
    print("  新调律:", tuning_path)
    print("  新谱面:", score_path)
    print("  迁移证据清单:", manifest_path)
    print("\n逐音迁移（旧级 -> 新级数 / 旧目标频率 / 新映射频率 / 通道）:")
    for n in manifest.notes:
        flag = "不可表达" if not n.representable_on_legacy else (
            "pending" if n.held_pending else f"ch{n.channel}")
        print(f"  L{n.legacy_midi_note:<3} -> deg{n.new_degree:<3} "
              f"{n.legacy_target_hz:10.4f}Hz -> {n.new_mapped_hz:10.4f}Hz "
              f"比例 {n.ratio_vs_ref:<10} [{flag}] {n.note}")
    if manifest.unrepresentable:
        print("\n旧通道无法表达的具体音（新映射仍保留其真实频率）:")
        for u in manifest.unrepresentable:
            print(f"  L{u.legacy_midi_note} 偏移 {u.offset_cents:+.2f}c "
                  f"超出 ±{u.bend_range_cents:.0f}c，"
                  f"目标 {u.target_freq_hz:.4f}Hz")
    for w in manifest.warnings:
        print("  警告:", w)
    print("\n切换策略对比:")
    print(" ", manifest.strategy_comparison["explanation"])
    for r in manifest.strategy_comparison["hold_ratio"]:
        print(f"  hold_ratio  L{r['legacy_midi_note']} "
              f"{r['old_freq_hz']:.4f} -> {r['new_freq_hz']:.4f}Hz "
              f"({r['delta_cents']:+.4f}c)")
    print("\n逐音独听信号核对:")
    for v in manifest.verification:
        flag = "OK  " if v["pass"] else "FAIL"
        print(f"  [{flag}] L{v['legacy_midi_note']}->deg{v['new_degree']} "
              f"旧目标={v['legacy_target_hz']:.4f} "
              f"新映射={v['new_mapped_hz']:.4f} "
              f"实测={v['measured_hz']:.4f}Hz")
    print("\n频率契约:", "通过" if manifest.frequency_contract_ok else "失败")
    if not manifest.frequency_contract_ok:
        raise SystemExit(1)


def cmd_check_lock(args):
    """核对迁移目录的构建锁是否与当前代码契约一致。"""
    lock = assert_lock_current(args.dir)
    print("锁文件与当前代码一致:")
    for k, v in lock.items():
        print(f"  {k}: {v}")


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

    pm = sub.add_parser(
        "migrate",
        help="把旧弯音调律资料与旧谱面迁移为显式比值新格式（辅助入口）")
    pm.add_argument("legacy_tuning", help="旧调律 JSON（legacy-bend/1）")
    pm.add_argument("legacy_score", help="旧谱面（legacy-note/legacy-chord）")
    pm.add_argument("outdir", help="迁移输出目录，必须不存在或为空（净目录）")
    pm.add_argument("--release", type=float, default=0.12,
                    help="合成器释放尾音秒数（通道释放占用建模）")
    pm.add_argument("--sample-rate", type=int, default=22050)
    pm.set_defaults(func=cmd_migrate)

    pl = sub.add_parser("check-lock",
                        help="核对迁移目录的调律格式构建锁")
    pl.add_argument("dir")
    pl.set_defaults(func=cmd_check_lock)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
