# 微分音音乐编配试听工具

输入**比例调律或非十二等分音阶**，以及由**音阶级数**描述的旋律与和声，
计算每个声音的真实频率并渲染可听结果。纯 Python 标准库实现，无第三方依赖。

## 核心设计约束（逐条对应需求）

1. **参考频率、周期比、音阶重复规则独立保存**
   `TuningSystem` 把 `ref_freq`（参考频率）、`period_ratio`（周期比，如 2/1 或 3/1）、
   `period_steps`（每周期级数）分开存储，不假定体系以八度或十二音为基础。
   Bohlen-Pierce 预置体系即为 3:1 周期、13 级，完全不含八度。
2. **音名只是标签，播放不量化回半音**
   频率一律由 `TuningSystem.degree_to_freq()` 按精确 `Fraction` 比例计算；
   前端 JS 中没有任何固定频率表（可用 `grep` 核查），只播放后端下发的真实 Hz。
3. **滑音跨微分音级的冲突由显式策略决定**
   - `hold_freq`：固定物理频率，调律变化后已发声音不动，音程比随之改变；
   - `hold_ratio`：保持相对参考级的音程比，按新调律重算，物理频率移动。
   切换调律时每个已发声音的续接结果都记录并显示在界面上。
4. **同一谱面可在不同调律下比较**：CLI 立体声 A/B WAV，Web 界面逐事件 ΔHz/Δ音分表。
5. **和弦频率比、拍频、超范围处理均可核对**：
   持续和声输出各音简单整数比与两两拍频；超出 `(20Hz, 20kHz)` 的音被静音并列出。
6. **简单可验证音色 + 信号分析闭环**：
   基波 + 两个谐波的加法合成；对渲染出的 PCM 做加窗 DFT（抛物线插值）
   与 Goertzel 精细搜索，从**实际输出**反估主频，与映射频率逐音核对。

## 运行说明（图形入口为主，命令仅辅助）

### 入口 1：零依赖 Web 图形界面（推荐，主入口）

不需要 root、不需要安装任何包，只用 Python 标准库：

```bash
python3 -I microtone/web/server.py          # 默认 http://localhost:8000
python3 -I microtone/web/server.py 8124     # 自定义端口
```

浏览器打开 `http://localhost:8000`，界面分四区：

1. **调律编辑**：选择 7 个预置体系（12/19/24-TET、纯律、五度相生、中庸全音、
   Bohlen-Pierce），或直接编辑参考频率、周期比、每周期级数、各级比例
   （支持 `3/2`、`1.25`、`701.955c`、`2^(1/12)` 四种写法）与音名标签，
   保存后已发声音按所选策略续接并显示续接记录。
2. **琴键触发**：按当前调律真实映射生成琴键，键面直接标注频率与精确比例；
   点击发声，另有滑音按钮（对数频率轴连续滑动，不经半音量化）。
3. **谱面事件**：内置四个示例，可编辑后渲染、导出 WAV、做 A/B 调律对比。
4. **结果区**：每个事件的映射频率、精确比例、**从输出信号实测的主频**、
   误差音分与核对结论；和弦区给出整数比与拍频；超范围事件单独警告。

### 入口 2：tkinter 桌面图形界面（可选）

需要系统自带 `python3-tk`（Debian/Ubuntu：`sudo apt install python3-tk`）：

```bash
python3 -I microtone/gui/tk_app.py
```

无显示环境或缺少 tkinter 时会提示改用 Web 入口。

### 命令行（辅助）

```bash
python3 -I microtone/cli.py list                       # 列出预置调律
python3 -I microtone/cli.py table just --lo -7 --hi 14 # 真实频率表
python3 -I microtone/cli.py render examples/just_chorale.score \
        --tuning just --out output/just.wav            # 渲染 WAV
python3 -I microtone/cli.py compare examples/compare_common.score \
        --a 12-TET --b 19-TET --out output/ab.wav      # 立体声 A/B
python3 -I microtone/cli.py verify examples/quarter_tone_demo.score \
        --tuning 24-TET                                 # 信号分析核对
python3 -I microtone/cli.py export-tunings examples/tunings  # 导出可调 JSON
python3 -I examples/demo_verify.py                     # 端到端演示
```

## 谱面 DSL

每行一条事件，按拍号自动换算时间：

```
bpm 120
tuning 24-TET          # 默认调律名（渲染时仍可替换）
policy hold_freq       # 调律切换的续接策略
note 0 1.0 vel 0.8     # 级数 0，1 拍
chord 0,1 3.0          # 同时发声（这里 50 音分差产生约 7.7Hz 拍频）
gliss 0 6 3.0 steps 24 # 级数 0 滑到 6，内部 24 个微音级
rest 0.5               # 休止
```

## 验证

```bash
python3 -I tests/test_core.py
```

22 个测试覆盖：N 平均律比例、负级数与跨周期、纯律/平均律大三度 13.7 音分差、
Bohlen-Pierce 非八度周期、参考频率独立性、JSON 往返、复音逐音实测核对、
滑音端点与对数连续性、WAV 头、超范围静音、两种续接策略的真实冲突、
调律对比与四分音拍频。

## 目录结构

```
microtone/
  core/tuning.py     调律模型与预置体系（精确分数比例）
  core/score.py      谱面事件与 DSL 解析
  core/renderer.py   PCM 合成、滑音、VoiceManager 续接策略、WAV 导出
  core/analysis.py   DFT/Goertzel 主频估计、和弦比、拍频、渲染核对
  web/               零依赖 Web 图形入口（http.server + 原生 JS）
  gui/tk_app.py      可选 tkinter 图形入口
  cli.py             命令行辅助
examples/            谱面样例、调律 JSON、端到端演示
tests/test_core.py   22 项测试
output/              渲染产物（WAV）
```
