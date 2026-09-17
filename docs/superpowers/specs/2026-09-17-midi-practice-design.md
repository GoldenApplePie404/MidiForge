# MIDI Practice — 设计规格

**日期**: 2026-09-17  
**项目**: MidiForge v0.4 插件  
**设备**: KL Essential 61 mk3（61 键钢琴 + 8 打击垫 CC 102-109）  
**依赖**: MidiForge API v0.3+（state / audio / clock / midi_file / bindings / log）

---

## 1. 目标

为 MidiForge 打造一个**综合 MIDI 设备练习插件**，支持：

- **钢琴**（61 键 note_on/note_off）+ **打击垫**（CC 102-109）**同轨混练**
- 三种反馈：**准确性判定** / **实时辅导** / **练习后图表分析**
- 三种练习源：**内置题库** / **用户录制** / **MIDI 文件导入**
- 两种练习模式：**视奏**（显示目标）/ **闭卷**（盲弹，事后看分）
- 三档练习范围：**钢琴 only** / **Pad only** / **同轨混练**

---

## 2. 架构

```
plugins/midi_practice/
  plugin.py          ← Plugin 入口 + 主 UI 框架
  engine.py          ← Judge 判定引擎 + 数据模型（Exercise/NoteTarget/HitResult）
  exercises.py       ← 练习源：BuiltInScales / UserRecording / MidiFileImport
  analytics.py       ← 成绩分析 + 薄弱项识别 + 图表数据
  config.json        ← 用户偏好（默认 BPM、容差、上次练习等）
```

**核心思想**：判定引擎只认统一的 `Exercise` 数据结构，不管练习从哪来。

---

## 3. 数据模型

### 3.1 Exercise（所有练习类型的统一抽象）

```python
@dataclass
class Exercise:
    name: str
    tempo: float = 120.0
    mode: str = "sight_read"       # "sight_read" | "blind"
    scope: str = "both"            # "pad" | "piano" | "both"
    notes: List[NoteTarget]
```

### 3.2 NoteTarget（单条练习目标）

```python
@dataclass
class NoteTarget:
    time: float                    # 从练习开始的秒数
    channel: int = 0
    note: Optional[int] = None     # piano note（60 = C4）
    cc: Optional[int] = None       # pad CC（102 = pad1）
    duration: float = 0.5
    velocity: int = 100            # 期望力度
```

### 3.3 HitResult（单条判定结果）

```python
@dataclass
class HitResult:
    expected: NoteTarget
    actual_time: float             # 用户实际按的时间（秒）
    delta_ms: float                # 节奏偏差（毫秒）
    pitch_ok: bool                 # 音高/CC号对不对
    velocity: int                  # 用户实际力度
    rating: str                    # "perfect" | "good" | "ok" | "miss"
```

### 3.4 SessionResult（一次练习结束的汇总）

```python
@dataclass
class SessionResult:
    exercise: Exercise
    results: List[HitResult]
    duration: float                # 总时长（秒）
    bpm_used: float
    stats: dict                    # {perfect: N, good: N, ok: N, miss: N, score: 0-100}
```

---

## 4. 判定引擎（Judge）

### 4.1 核心参数

| 参数 | 默认值 | 用户可调 |
|---|---|---|
| `tolerance_ms` | 100ms | 是（80-200ms） |
| `perfect_threshold_ms` | 30ms | 否（硬编码） |
| `velocity_tolerance` | ±20 | 否（判定力度时用） |
| `miss_timeout_ms` | tolerance + 200 | 否（超时自动 miss） |

### 4.2 判定流程

```
用户 MIDI 输入
    ↓
note_on (velocity > 0) 或 CC (value > 0)？
    ↓ 否
    忽略

    ↓ 是
Judge.on_midi_in(parsed)

1. 找所有未判定目标中，时间最接近的 NoteTarget
2. 算时间差 delta_ms
3. 音高/CC 是否匹配？
   - piano: parsed.values["note"] == target.note
   - pad:   parsed.values["control"] == target.cc
4. 给 rating:
   - pitch wrong → "miss"
   - |delta| < 30 → "perfect"
   - |delta| < 100 → "good"
   - |delta| < 200 → "ok"
   - else → "miss"
5. 标记目标已判定，返回 HitResult
6. 定时检查：还有未判定目标 + 当前时间 > target.time + miss_timeout
   → 强制标 miss
```

### 4.3 多余输入 & 漏按处理

- **多余输入**（Exercise 里没有这个音但用户按了）→ 返回 `HitResult(rating="miss", pitch_ok=False)`
- **漏按**（到了 miss_timeout 还没按）→ Judge 内部定时器把目标强制标 miss

---

## 5. 练习源

### 5.1 BuiltInScales（内置题库）

`exercises.py` 里硬编码若干预设练习：

| 名称 | 范围 | 内容 |
|---|---|---|
| C 大调音阶 ascending | piano | C4→B4→C5，8 个音 |
| A 小调五声 blues | piano | A4/C4/D4/E4/G4，循环 |
| I-IV-V 和弦进行 | piano | Cmaj → Fmaj → Gmaj |
| Pad CC 识别 | pad | 8 个 pad 随机顺序 |
| 八分音符节奏 | both | 钢琴 + pad 同轨 |

每个都是 `Exercise(notes=[NoteTarget(...), ...])` 直接构造。

### 5.2 UserRecording（用户录制）

```
用户点"录制" → clock.beat 开始计时
    ↓
subscribe("midi.all") 收集每条消息 + 记录相对时间戳
    ↓
达到指定时长（默认 4 小节）→ 停止
    ↓
把 ParsedMessage 列表转 Exercise（提取 note/cc + time）
    ↓
保存到 app.data_dir("midi_practice") / recordings/*.json
```

### 5.3 MidiFileImport（MIDI 文件导入）

```
用户选 .mid 文件
    ↓
app.midi_file.load(path) → List[ParsedMessage]（带 tick_time）
    ↓
过滤只留 note_on/cc（忽略 program_change/pitch_bend/sysex）
    ↓
转 Exercise（note_on → NoteTarget.note, cc → NoteTarget.cc）
```

---

## 6. UI 设计

### 6.1 主面板布局

```
┌────────────────────────────────────────────────────────────┐
│  MIDI Practice                                              │
│                                                              │
│  ┌─── 练习选择 ───────────────────────────────────────────┐ │
│  │ [内置 ▼]  C大调音阶  [🎙 录制] [📂 导入MIDI]            │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌─── 设置 ──────────┐  ┌─── 范围 ──────┐  ┌─── 节奏 ────┐ │
│  │ [● 视奏 ○ 闭卷]   │  │ [●钢琴 ○Pad  │  │ BPM: 120 ▼  │ │
│  │                   │  │ [○混练]       │  │ 容差: 100ms │ │
│  └───────────────────┘  └───────────────┘  └─────────────┘ │
│                                                              │
│  [▶ 开始]  [■ 停止]              进度: ████░░░░  4/8       │
│                                                              │
│  ══════════════════════════════════════════════════════════ │
│                                                              │
│  ╭── 钢琴键盘区 ────────────────────────────────────────╮  │
│  │  视奏模式: 目标音符在对应键上高亮                       │  │
│  │  C4 █████  ← perfect                                  │  │
│  │  D4     █████  ← good                                 │  │
│  │  E4         █████  ← miss（标红）                      │  │
│  ╰──────────────────────────────────────────────────────╯  │
│                                                              │
│  ╭── 打击垫区 ───────────────────────────────────────────╮  │
│  │  ┌────┬────┬────┬────┐                                │  │
│  │  │ P1 │ P2 │ P3 │ P4 │                                │  │
│  │  ├────┼────┼────┼────┤                                │  │
│  │  │ P5 │ P6 │ P7 │ P8 │                                │  │
│  │  └────┴────┴────┴────┘                                │  │
│  │  视奏模式: 目标 pad 发光；判定后显示 rating            │  │
│  ╰──────────────────────────────────────────────────────╯  │
│                                                              │
│  ╭── 实时反馈 ───────────────────────────────────────────╮  │
│  │  -32ms  pitch OK    PERFECT   [绿色]                   │  │
│  │  +85ms  pitch OK    GOOD      [白色]                   │  │
│  │  +12ms  pitch WRONG MISS      [红色]                   │  │
│  │  +300ms （超时漏按）MISS      [灰色]                   │  │
│  ╰──────────────────────────────────────────────────────╯  │
│                                                              │
│  实时得分: Perfect 3  Good 2  Miss 1  →  83/100              │
└────────────────────────────────────────────────────────────┘
```

### 6.2 练习结束分析面板（弹窗 / QTabWidget）

```
┌──────────────────────────────────────────────────────┐
│ 练习完成！  88/100  准确率 89%                          │
│                                                        │
│  [力度曲线]    目标（虚线）vs 你的力度（实线）           │
│  110 ─ ─ ─ ─ ─ ─ ─ ─ ─                               │
│      ╱╲       ╱╲╱╲                                     │
│  80 ─   ╲ ╱╲╱     ╲ ╱ ╱                               │
│                                                        │
│  [节奏偏差]    每个音的误差 ms                          │
│  +100 │        ┃                                      │
│    0  │ ┃ ┃ ┃    ┃ ┃ ┃                                │
│  -100 │           ┃                                    │
│        1  2  3  4  5  6  7  8                          │
│                                                        │
│  [薄弱项]                                              │
│    - G4 力度偏弱 -18 velocity                          │
│    - Pad5 平均提前 60ms                                │
│                                                        │
│  [建议]                                                │
│    - 试试容差调到 150ms                                 │
│    - 单独练 Pad CC 识别                                │
│                                                        │
│  [🔄 重练]  [📂 保存成绩]                              │
└──────────────────────────────────────────────────────┘
```

---

## 7. 和 MidiForge API 的集成点

| API | 用途 |
|---|---|
| `subscribe("midi.note_on")` | 接收钢琴输入做判定 |
| `subscribe("midi.cc")` | 接收 pad 输入做判定 |
| `subscribe("clock.beat")` | Judge 定时检查漏按 + 练习进度 |
| `state.pressed_notes()` | 视奏模式：高亮当前按下的键 |
| `midi_file.load()` | MIDI 文件导入 |
| `midi_file.save()` | 用户录制导出为 .mid |
| `audio.play()` | 目标音提示音（可选开/关） |
| `data_dir("midi_practice")` | 存储 recordings + 成绩 |
| `log.info()` | 插件日志 |

---

## 8. 运行状态机

```
                    ┌─────────┐
                    │  IDLE   │ ← 初始 / 练习结束
                    └────┬────┘
                         │ 选练习 + 点"开始"
                         ↓
                    ┌─────────┐
            ┌──────→│PLAYING  │ ← Judge.on_midi_in() 持续判定
            │       └────┬────┘
            │            │ 所有目标判定完 或 手动停止
            │            ↓
            │       ┌──────────┐
            │       │ ANALYZING│ → 生成 SessionResult + 显示分析面板
            │       └────┬─────┘
            │            │ 用户点"重练"
            └────────────┘
```

---

## 9. 非功能需求

| 项 | 要求 |
|---|---|
| 判定延迟 | 用户按键 → 判定结果显示 < 50ms |
| 并发 | 练习器运行不影响 PadSentry（两者独立） |
| 录制时长 | 无硬性上限，建议 4-16 小节 |
| 成绩存储 | JSON 存 `data_dir("midi_practice")/scores/` |
| 错误处理 | MIDI 输入异常不崩 Judge；导入 MIDI 失败给清晰提示 |

---

## 10. 实现优先级

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **P0 核心** | Judge 判定引擎 + 数据模型 + BuiltInScales + 主 UI（简化版） | API v0.3 |
| **P1 录制** | UserRecording + app.midi_file.load/save | midi_file API |
| **P2 导入** | MidiFileImport + NoteTarget 生成逻辑 | midi_file API |
| **P3 分析** | Analytics + 图表（QPainter 画）+ 薄弱项 | 完整 Judge |
| **P4 打磨** | 视奏模式飘下动画 / 闭卷模式 / 建议系统 | 完整主链路 |

---

## 11. 不在范围内

- OSC 无线控制（需要新依赖）
- 多练习器并行（一个插件一个练习器）
- 排行榜 / 云同步（纯本地）
- DAW 联动 / VST 桥
