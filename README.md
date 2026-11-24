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

浏览器打开 `http://localhost:8000`，界面分五区：

1. **调律编辑**：选择 7 个预置体系（12/19/24-TET、纯律、五度相生、中庸全音、
   Bohlen-Pierce），或直接编辑参考频率、周期比、每周期级数、各级比例
   （支持 `3/2`、`1.25`、`701.955c`、`2^(1/12)` 四种写法）与音名标签，
   保存后已发声音按所选策略续接并显示续接记录。
2. **琴键触发**：按当前调律真实映射生成琴键，键面直接标注频率与精确比例；
   点击发声，另有滑音按钮（对数频率轴连续滑动，不经半音量化）。
3. **谱面事件**：内置四个示例，可编辑后渲染、导出 WAV、做 A/B 调律对比。
4. **调律资料迁移**：粘贴旧弯音偏移资料（`legacy-bend/1`：参考频率、弯音范围、
   通道数、音分偏移）与旧谱面，一键迁移为显式比值新格式（`explicit-ratio/2`）。
   后端按真实频率与通道事件运算，展示逐音新旧频率契约、每音独听的信号实测核对、
   实际通道事件时间线（含释放占用与待处理补发）、无法用旧通道表达的具体音，以及
   “按原音程比保留持续音(hold_freq)”与“安全换气点切换”的对比；迁移后的调律自动
   注册到第 1 区、迁移谱面填入第 3 区，琴键与渲染都走新映射发声。异常由本界面提示。
5. **结果区**：每个事件的映射频率、精确比例、**从输出信号实测的主频**、
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
# 调律资料迁移（图形界面为主入口，CLI 仅辅助；输出目录必须是净目录）
python3 -I microtone/cli.py migrate \
        examples/legacy/quarter_tone_bend.legacy.json \
        examples/legacy/quarter_tone_bend.legacy.score /tmp/mig_out
python3 -I microtone/cli.py check-lock /tmp/mig_out    # 核对构建锁
python3 -I examples/demo_verify.py                     # 端到端演示
python3 -I examples/demo_migration.py                  # 迁移端到端演示
```

## 调律资料迁移（旧弯音 → 显式比值）

旧格式 `legacy-bend/1` 在十二平均律半音上叠加固定音分偏移，并受音源弯音通道
约束：每通道一条弯音、通道数固定、偏移超出弯音范围即无法发声。迁移到新格式
`explicit-ratio/2` 后，每个旧基底级成为一个独立级数，比例按旧资料的**真实目标
频率**反算为精确 `Fraction`，发声只走 `TuningSystem.degree_to_freq()`，旧弯音
仅用于迁移期的通道模拟，不参与新格式发声。迁移语义保证：

- **通道争用**：同时起音的两个音即使起音相同，也不共用后续弯音；空闲通道不足时
  改派或保留为待处理（pending），待通道释放结束的精确时刻补发，绝不把待处理音
  改写成共享弯音下的错误频率。
- **释放占用**：合成器回报通道进入释放（默认 0.12s 尾音）后，通道在释放结束前
  不可复用，时间线显式记录 `note_off / release_done / note_on(补发)`。
- **无法表达的和弦**：超出弯音范围的具体音逐音标记，可表达部分的真实频率完整
  保留；不可表达音在新映射中仍按真实频率发声。
- **非十二等分含义**：音分偏移以比值永久保留（如四分音 +150c 不是整数半音），
  无法用旧通道表达的偏移都会在清单中报告。
- **两种切换策略对比**：`hold_freq`（按原音程比保留持续音，物理频率不动）与
  “安全换气点切换”（推迟到无在发音的时刻整体切到新调律），逐音给出 ΔHz/Δ音分。
- **核对闭环**：每个迁移音做单音独听渲染并从 PCM 实测主频，同时核对旧目标频率与
  新映射频率；全部证据写入迁移清单，格式转换不允许伪报。
- **构建锁定与净目录**：迁移输出目录必须不存在或为空，并写入
  `tuning-format.lock.json`；运行时核对锁与代码常量，版本不符或被手改即拒绝。
  迁移产物头部注明新旧格式版本。

旧资料示例见 `examples/legacy/`（四分音弯音、窄弯音含不可表达音两套）。

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
python3 -I tests/test_core.py        # 核心 22 项
python3 -I tests/test_migration.py   # 迁移 22 项
```

核心 22 个测试覆盖：N 平均律比例、负级数与跨周期、纯律/平均律大三度 13.7 音分差、
Bohlen-Pierce 非八度周期、参考频率独立性、JSON 往返、复音逐音实测核对、
滑音端点与对数连续性、WAV 头、超范围静音、两种续接策略的真实冲突、
调律对比与四分音拍频。

迁移 22 个测试覆盖：旧音分偏移的真实频率、弯音范围可表达性、同起音不共用弯音、
释放通道不复用、待处理音在释放结束时刻补发、补发前结束的撤销、不可表达音标记、
非十二等分偏移以比值保留、新旧频率契约与信号实测、不可表达和弦逐音标记且保留
可表达部分、两种切换策略冲突、安全换气点、净目录与构建锁（含被篡改拒绝）、
两套旧样例运行到底。

## 目录结构

```
microtone/
  core/tuning.py     调律模型与预置体系（精确分数比例）
  core/score.py      谱面事件与 DSL 解析
  core/renderer.py   PCM 合成、滑音、VoiceManager 续接策略、WAV 导出
  core/analysis.py   DFT/Goertzel 主频估计、和弦比、拍频、渲染核对
  migration/         旧弯音资料 -> 显式比值新格式的迁移器
    legacy.py        旧格式模型（参考频率/弯音范围/通道数/音分偏移）与旧 DSL
    channels.py      时间驱动的弯音通道分配（释放占用、待处理补发）
    migrator.py      新格式构建、频率契约、策略对比、逐音独听核对
    lock.py          格式版本构建锁与净目录约束
  web/               零依赖 Web 图形入口（http.server + 原生 JS，含迁移面板）
  gui/tk_app.py      可选 tkinter 图形入口
  cli.py             命令行辅助（含 migrate / check-lock）
examples/
  legacy/            旧弯音调律 JSON 与旧谱面样例
  demo_migration.py  迁移端到端演示
tests/test_core.py       22 项核心测试
tests/test_migration.py  22 项迁移测试
output/              渲染产物（WAV）
```
