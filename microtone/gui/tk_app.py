"""微分音编曲 tkinter 桌面界面（可选图形入口）。

前提：系统已安装 python3-tk（Debian/Ubuntu: apt install python3-tk）。
无显示器或未装 tkinter 的环境请使用零依赖 Web 界面：
    python3 -I microtone/web/server.py

本界面与 Web 界面共用 microtone.core，发声频率同样全部来自
TuningSystem.degree_to_freq，无任何半音量化。
桌面端用 stdlib winsound/简单回调不可移植，因此“试听”采用：
实时 VoiceManager 生成 PCM -> 写临时 WAV -> 系统播放器播放（若可用），
并始终提供完整频率表、续接记录与信号分析结果。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from microtone.core import (
    builtin_systems,
    TuningSystem,
    parse_score,
    render_score,
    write_wav,
    VoiceManager,
    verify_render,
)

SR = 44100


class MicrotoneTk(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("微分音音乐编配（tkinter）")
        self.geometry("1080x780")
        self.systems = builtin_systems()
        self.current_name = "12-TET"
        self.manager = VoiceManager(
            self.systems[self.current_name], sample_rate=SR)
        self._build_widgets()
        self._refresh_tuning_form()
        self._build_keyboard()

    # ------------------------------------------------------------------ UI
    def _build_widgets(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        # Tab 1 调律编辑 + 琴键
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="调律与琴键")

        top = ttk.Frame(tab1)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="预置调律").pack(side="left")
        self.tuning_var = tk.StringVar(value=self.current_name)
        sel = ttk.Combobox(top, textvariable=self.tuning_var, width=28,
                           values=list(self.systems), state="readonly")
        sel.pack(side="left", padx=6)
        sel.bind("<<ComboboxSelected>>", lambda e: self._on_select_tuning())

        ttk.Label(top, text="续接策略").pack(side="left", padx=(16, 4))
        self.policy_var = tk.StringVar(value="hold_ratio")
        ttk.Combobox(top, textvariable=self.policy_var, width=12,
                     values=["hold_ratio", "hold_freq"],
                     state="readonly").pack(side="left")
        ttk.Button(top, text="应用策略",
                   command=self._on_apply_policy).pack(side="left", padx=6)

        form = ttk.LabelFrame(tab1, text="调律参数（参考频率/周期比/重复规则独立）")
        form.pack(fill="x", padx=8, pady=4)
        self.ref_var = tk.StringVar()
        self.refdeg_var = tk.StringVar()
        self.steps_var = tk.StringVar()
        self.period_var = tk.StringVar()
        for i, (label, var) in enumerate([
            ("参考频率Hz", self.ref_var),
            ("参考级", self.refdeg_var),
            ("每周期级数", self.steps_var),
            ("周期比", self.period_var),
        ]):
            ttk.Label(form, text=label).grid(row=0, column=i * 2, padx=4, pady=4)
            ttk.Entry(form, textvariable=var, width=12).grid(
                row=0, column=i * 2 + 1)

        scales = ttk.Frame(tab1)
        scales.pack(fill="both", expand=True, padx=8)
        ttk.Label(scales, text="音阶比例（如 3/2、400c、2^(1/12)）").grid(
            row=0, column=0, sticky="w")
        self.scale_text = scrolledtext.ScrolledText(scales, width=34, height=14)
        self.scale_text.grid(row=1, column=0, sticky="nsew", padx=4)
        ttk.Label(scales, text="音名标签（仅显示）").grid(row=0, column=1, sticky="w")
        self.label_text = scrolledtext.ScrolledText(scales, width=18, height=14)
        self.label_text.grid(row=1, column=1, sticky="nsew", padx=4)
        scales.columnconfigure(0, weight=2)
        scales.columnconfigure(1, weight=1)

        btns = ttk.Frame(tab1)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="保存并应用（声音按策略续接）",
                   command=self._on_save_tuning).pack(side="left")
        ttk.Button(btns, text="重新载入",
                   command=self._refresh_tuning_form).pack(side="left", padx=6)

        self.key_frame = ttk.Frame(tab1)
        self.key_frame.pack(fill="x", padx=8, pady=6)

        # Tab 2 谱面与分析
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="谱面与信号分析")
        self.score_text = scrolledtext.ScrolledText(tab2, height=12)
        self.score_text.pack(fill="x", padx=8, pady=6)
        self.score_text.insert("1.0",
            "bpm 120\ntuning 24-TET\npolicy hold_freq\n"
            "chord 0,1 3.0\nrest 0.5\ngliss 0 6 3.0 steps 24\n")
        bar = ttk.Frame(tab2)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="渲染并信号分析",
                   command=self._on_render).pack(side="left")
        ttk.Button(bar, text="导出 WAV 并试听",
                   command=self._on_export).pack(side="left", padx=6)
        self.result_text = scrolledtext.ScrolledText(tab2)
        self.result_text.pack(fill="both", expand=True, padx=8, pady=6)

        # Tab 3 续接记录
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text="调律切换续接记录")
        self.cont_text = scrolledtext.ScrolledText(tab3)
        self.cont_text.pack(fill="both", expand=True, padx=8, pady=6)

    def _build_keyboard(self):
        for w in self.key_frame.winfo_children():
            w.destroy()
        t = self.systems[self.current_name]
        for degree in range(-7, 13):
            freq = t.degree_to_freq(degree)
            btn = tk.Button(
                self.key_frame,
                text=f"{t.label_of(degree)}\ndeg{degree}\n{freq:.1f}Hz",
                width=8, height=4,
                command=lambda d=degree: self._on_key(d))
            btn.grid(row=0, column=degree + 7, padx=2, pady=4)

    # ------------------------------------------------------------- 调律逻辑
    def _refresh_tuning_form(self):
        t = self.systems[self.current_name]
        self.ref_var.set(str(t.ref_freq))
        self.refdeg_var.set(str(t.ref_degree))
        self.steps_var.set(str(t.period_steps))
        self.period_var.set(str(t.period_ratio))
        self.scale_text.delete("1.0", "end")
        self.scale_text.insert("1.0", "\n".join(str(r) for r in t.scale))
        self.label_text.delete("1.0", "end")
        self.label_text.insert("1.0", "\n".join(t.labels))

    def _on_select_tuning(self):
        name = self.tuning_var.get()
        report = self.manager.apply_tuning_change(self.systems[name])
        self.current_name = name
        self._refresh_tuning_form()
        self._build_keyboard()
        self._log_continuation(report)

    def _on_apply_policy(self):
        self.manager.set_policy(self.policy_var.get())
        self._log_continuation([{"note": f"策略切换为 {self.policy_var.get()}"}])

    def _on_save_tuning(self):
        try:
            t = TuningSystem(
                name=self.current_name,
                ref_freq=float(self.ref_var.get()),
                ref_degree=int(self.refdeg_var.get()),
                period_steps=int(self.steps_var.get()),
                period_ratio=self.period_var.get().strip(),
                scale=[line.strip() for line in
                       self.scale_text.get("1.0", "end").splitlines()
                       if line.strip()],
                labels=[line.strip() for line in
                        self.label_text.get("1.0", "end").splitlines()
                        if line.strip()],
            )
            report = self.manager.apply_tuning_change(t)
            self.systems[self.current_name] = t
            self._build_keyboard()
            self._log_continuation(report)
            messagebox.showinfo("成功", f"调律已更新：{t.name}")
        except Exception as exc:
            messagebox.showerror("调律错误", str(exc))

    def _on_key(self, degree: int):
        v = self.manager.note_on(degree)
        t = self.systems[self.current_name]
        if v is None:
            messagebox.showwarning(
                "超出范围",
                f"deg{degree} = {t.degree_to_freq(degree):.2f}Hz 已静音")
            return
        # 桌面试听：渲染该单音 1.2s WAV 后交给系统播放器
        score = parse_score(f"bpm 60\nnote {degree} 1.2\n")
        result = render_score(score, t, sample_rate=SR)
        self._play_pcm(result["pcm"], SR)

    # ------------------------------------------------------------- 谱面逻辑
    def _on_render(self):
        try:
            score = parse_score(self.score_text.get("1.0", "end"))
            t = self.systems[self.current_name]
            result = render_score(score, t, sample_rate=SR)
            checks = verify_render(result)
            lines = [f"调律 {t.name}｜周期 {t.period_steps} 级 / {t.period_ratio}"]
            lines.append("级数  标签    映射Hz      实测Hz      误差     核对")
            for m, c in zip(result["mapping"], checks):
                lines.append(
                    f"{m['degree']:>4}  {m.get('label',''):<6} "
                    f"{m['freq_hz']:>10.4f} {c['estimated_hz']:>10.4f} "
                    f"{c['error_cents']:+7.3f}c {'OK' if c['pass'] else 'FAIL'}")
            for e in result["out_of_range"]:
                lines.append(f"超范围静音: {e}")
            self.result_text.delete("1.0", "end")
            self.result_text.insert("1.0", "\n".join(lines))
        except Exception as exc:
            messagebox.showerror("渲染错误", str(exc))

    def _on_export(self):
        score = parse_score(self.score_text.get("1.0", "end"))
        t = self.systems[self.current_name]
        result = render_score(score, t, sample_rate=SR)
        path = os.path.join(os.getcwd(),
                            f"microtone_{self.current_name}.wav")
        write_wav(path, result["pcm"], SR)
        self._play_pcm(result["pcm"], SR)
        messagebox.showinfo("已导出", path)

    # ------------------------------------------------------------------ 工具
    def _log_continuation(self, report):
        self.cont_text.insert("end", "--- 调律变化 ---\n")
        for r in report:
            self.cont_text.insert("end", str(r) + "\n")
        self.cont_text.see("end")

    def _play_pcm(self, pcm, sr):
        if not shutil.which("aplay") and not shutil.which("paplay"):
            return  # 无系统播放器时只保留分析与导出能力
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        write_wav(path, pcm, sr)
        player = "aplay" if shutil.which("aplay") else "paplay"
        try:
            subprocess.Popen([player, path],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass


def main():
    try:
        app = MicrotoneTk()
    except tk.TclError as exc:
        print("无法启动 tkinter（缺少显示器或 python3-tk）：", exc)
        print("请改用零依赖 Web 图形入口：python3 -I microtone/web/server.py")
        raise SystemExit(1)
    app.mainloop()


if __name__ == "__main__":
    main()
