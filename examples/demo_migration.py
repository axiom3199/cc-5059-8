#!/usr/bin/env python3
"""端到端迁移演示：旧弯音资料 -> 显式比值新格式 -> 真实频率/事件证据核对。

运行：python3 -I examples/demo_migration.py
纯标准库，不写仓库目录；迁移产物演示落在系统临时目录的净目录中。
"""

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from microtone.migration import (
    load_legacy_tuning,
    parse_legacy_score,
    migrate_tuning,
    FORMAT_VERSION,
    write_format_lock,
    ensure_clean_output_dir,
    assert_lock_current,
)
from microtone.core import TuningSystem, parse_score, render_score, verify_render

LEGACY = os.path.join(ROOT, "examples", "legacy")
PAIRS = [
    ("quarter_tone_bend.legacy.json",
     "quarter_tone_bend.legacy.score"),
    ("just_narrow_bend.legacy.json",
     "narrow_bend_unrepresentable.legacy.score"),
]


def line(s=""):
    print(s)


def main():
    out_root = tempfile.mkdtemp(prefix="microtone_migration_")
    line("迁移产物净目录根: " + out_root)
    try:
        for idx, (lt, ls) in enumerate(PAIRS):
            legacy = load_legacy_tuning(os.path.join(LEGACY, lt))
            score = parse_legacy_score(
                open(os.path.join(LEGACY, ls), encoding="utf-8").read())
            manifest = migrate_tuning(legacy, score, sample_rate=22050)

            out_dir = os.path.join(out_root, f"case{idx}")
            ensure_clean_output_dir(out_dir)
            write_format_lock(out_dir)
            tuning_path = os.path.join(out_dir, "tuning.json")
            score_path = os.path.join(out_dir, "migrated.score")
            manifest_path = os.path.join(out_dir, "manifest.json")
            with open(tuning_path, "w", encoding="utf-8") as f:
                json.dump({**{"format_version": FORMAT_VERSION},
                           **manifest.tuning}, f, ensure_ascii=False, indent=2)
            with open(score_path, "w", encoding="utf-8") as f:
                f.write(manifest.migrated_score_text)
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
            assert_lock_current(out_dir)

            line("=" * 72)
            line(f"案例 {lt}  格式 {manifest.format_version} "
                 f"<- {manifest.legacy_format_version}")
            line("=" * 72)
            line("逐音迁移（旧目标频率 == 新映射频率，比例保留非十二等分含义）:")
            for n in manifest.notes:
                if not n.representable_on_legacy:
                    tag = "不可表达"
                elif n.held_pending:
                    tag = "pending"
                elif n.channel is None:
                    tag = "谱面未用"
                else:
                    tag = f"ch{n.channel}"
                line(f"  L{n.legacy_midi_note:<3}->deg{n.new_degree:<3} "
                     f"{n.legacy_target_hz:10.4f}Hz == "
                     f"{n.new_mapped_hz:10.4f}Hz 比例 {n.ratio_vs_ref:<10} "
                     f"[{tag}]")
            for u in manifest.unrepresentable:
                line(f"  报告无法表达的旧偏移: L{u.legacy_midi_note} "
                     f"{u.offset_cents:+.2f}c 超 ±{u.bend_range_cents:.0f}c，"
                     f"真实频率 {u.target_freq_hz:.4f}Hz 已保留")
            for w in manifest.warnings:
                line("  和弦标记: " + w)

            line("实际通道事件（前 12 条，含释放占用/补发）:")
            for e in manifest.channel_timeline[:12]:
                target = (f"{e['target_freq_hz']:.2f}Hz"
                          if e["target_freq_hz"] else "")
                line(f"  t={e['time']:6.3f} {e['kind']:<14} "
                     f"ch={e['channel']:>2} note={e['midi_note']} "
                     f"bend={e['bend_cents']:+7.2f}c {target} {e['detail']}")

            line("每音独听信号核对（实测主频 vs 旧目标/新映射）:")
            for v in manifest.verification:
                flag = "OK  " if v["pass"] else "FAIL"
                line(f"  [{flag}] L{v['legacy_midi_note']} "
                     f"实测={v['measured_hz']:9.4f}Hz "
                     f"对旧={v['error_vs_legacy_cents']:+.3f}c "
                     f"对新={v['error_vs_new_cents']:+.3f}c")

            line("切换策略对比:")
            line("  " + manifest.strategy_comparison["explanation"])

            # 迁移谱面再走一次标准新路径渲染，闭环验证
            tuning = TuningSystem.from_dict(manifest.tuning)
            new_score = parse_score(manifest.migrated_score_text)
            render = render_score(new_score, tuning, sample_rate=22050)
            checks = verify_render(render)
            line(f"新路径渲染: {len(render['mapping'])} 事件, "
                 f"核对全通过={all(c['pass'] for c in checks)}, "
                 f"频率契约={manifest.frequency_contract_ok}")
            assert manifest.frequency_contract_ok
            assert all(c["pass"] for c in checks)
        line("\n全部迁移案例运行到底，证据位于: " + out_root)
        line("（演示结束，临时目录可手动删除）")
    finally:
        # 默认保留产物供检查；如需自动清理设环境变量 CLEAN=1
        if os.environ.get("CLEAN") == "1":
            shutil.rmtree(out_root, ignore_errors=True)


if __name__ == "__main__":
    main()
