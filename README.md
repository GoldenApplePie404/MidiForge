# MIDI 调试工具

一个 Windows 平台的双向 MIDI 监控、信号绑定与虚拟端口调试工具。

- 实时收发与解析 MIDI 消息，支持 note / CC / pitch bend / aftertouch / sysex 等
- 自动检测 loopMIDI / Virtual MIDI 虚拟端口
- 键盘 ↔ MIDI 信号绑定，JSON 持久化
- 霓虹暗色主题，16 通道矩阵 + 消息日志 + 发送测试面板
- 事件总线驱动，支持 Python 插件和 headless SDK

---

## 快速开始

### 方式一：一键启动（推荐）

双击 `start.bat`，首次运行会自动创建虚拟环境并安装依赖。

### 方式二：手动启动

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 安装依赖
.venv\Scripts\pip install -r requirements.txt

# 3. 运行
.venv\Scripts\python app.py
```

### 测试

```bash
.venv\Scripts\python -m pytest tests/ -v
```

当前状态：**81 passed, 0 skipped**（含真实虚拟端口回环测试）。

### 依赖

| 包 | 版本 | 用途 |
|---|---|---|
| mido | ≥1.3.2 | MIDI 消息封装与解析 |
| python-rtmidi | ≥1.5.8 | 跨平台 MIDI I/O |
| PyQt6 | ≥6.7.0 | GUI 框架 |
| keyboard | ≥0.13.5 | 全局键盘监听 |
| pygame | ≥2.5 | 打击垫插件音频播放 |
| pytest / pytest-qt | — | 开发依赖，测试框架 |

---

## 使用指南

### 连接 MIDI 设备

1. 打开工具，顶部"输入设备"下拉选你的键盘或虚拟端口输入
2. "输出设备"下拉选需要接收消息的端口（如 loopMIDI 输出端口）
3. 右侧状态栏会高亮显示自动检测到的虚拟端口名称

> Windows 上 rtmidi 会给端口名追加索引号（如 `CYD-MIDI 0` / `CYD-MIDI 1`），工具会自动识别同 base 名的输入输出对作为虚拟端口。

### 实时监控

- **通道矩阵**：16 通道按 MIDI 1–16 对应，收到消息的通道会青色脉冲高亮
- **消息日志**：显示时间戳、来源、通道、消息类型、数值、原始 hex 字节、中文描述
- 点击"清空"按钮重置日志

### 发送测试

面板底部的发送测试区可快速发一条 MIDI 消息到输出端口：
- 类型下拉：`note_on` / `note_off` / `control_change` / `pitchwheel` / `program_change` / `poly_aftertouch` / `channel_aftertouch`
- 通道：0–15（日志中显示为 1–16）
- 参数根据类型自动切换（note + velocity / control + value / pitch 等）
- 点击青色"发送"按钮

### 信号绑定

绑定面板管理 MIDI / 键盘 → 自定义信号的映射关系：

| 列 | 说明 |
|---|---|
| 信号名 | 自定义标签（如 `kick` / `solo_on`） |
| 源类型 | `midi` 或 `keyboard` |
| 源描述 | MIDI：事件类型 + 通道 + 参数过滤；键盘：按键名 |
| 虚拟输出 | 可选，匹配后自动转发到虚拟端口 |

- **新增 / 删除**：行级操作
- **导入 / 导出 JSON**：方便跨设备同步

配置文件保存在 `config/bindings.json`（首次运行自动创建，损坏文件自动备份）。

### PadSentry 打击垫插件

2×4 可视化打击垫，监听 MIDI CC 102–109（对应多数 61 键键盘打击垫），踩 pad 时格子发光 + 播放鼓组采样。

**音频特性：**

- 每 pad 独占 4 条 pygame channel，round-robin 轮换播放，**连打 / 同时踩永不丢音**
- 支持 FL Studio Edison 导出的 **Ogg Vorbis 嵌 WAV 格式**（format tag 0x674F），自动解包转 PCM
- 用户原始采样安全：解包前自动备份到 `samples_backup/` 目录，gitignore 排除不上传

**自定义采样：**

把 wav 丢进 `plugins/pad_sentry/samples/`，改 `plugins/pad_sentry/config.json` 的 `wav` 字段指向新文件名即可。

---

## SDK —— Headless 模式

不启动 GUI，直接用 Python 订阅 MIDI 事件：

```python
import api

app = api.create_app()

@api.on("midi.message")
def on_msg(parsed):
    print(f"[{parsed.type}] ch={parsed.channel} values={parsed.values} hex={parsed.raw_hex}")

app.engine.open_input("CYD-MIDI 0")
app.start()
# ... 持续监听
app.stop()
```

运行示例：

```bash
python examples/sdk_demo.py "CYD-MIDI 0"
```

### 事件总线 Topic

| Topic | 载荷 | 触发时机 |
|---|---|---|
| `midi.message` | `ParsedMessage` | 收到并解析后的每条 MIDI |
| `midi.signal` | `{"signals": Set[str], "source": ...}` | Matcher 匹配到绑定时 |
| `midi.sent` | `ParsedMessage` | 主动发送一条消息后 |
| `key.pressed` | `str` — 归一化按键名 | 键盘监听回调 |
| `binding.changed` | `BindingConfig` | 绑定配置修改后 |

### AppContext 聚合对象

`api.create_app()` 返回的 `AppContext` 包含：

- `engine: MidiEngine` — MIDI 端口引擎
- `bus: EventBus` — 事件总线
- `config: BindingConfig` — 绑定配置
- `matcher: Matcher` — 匹配器
- `send_midi(type, channel, **kw)` — 快捷发送方法

---

## 插件开发

在 `plugins/` 目录下新建目录和 `plugin.py`，继承 `core.plugin.Plugin`：

```python
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget
from core.plugin import Plugin
from ui import style_qss as QSS


class NoteSentry(Plugin):
    name = "note_sentry"

    def on_activate(self, app):
        self.app = app
        app.on("midi.message", self.on_message)

    def on_message(self, parsed):
        if hasattr(self, "label"):
            self.label.setText(f"最后消息: {parsed.type} ch{parsed.channel + 1}")

    def create_panel(self):
        box = QWidget()
        v = QVBoxLayout(box)
        self.label = QLabel("等待 MIDI 消息…")
        v.addWidget(self.label)
        return box

    def on_deactivate(self):
        pass
```

插件宿主会自动发现、隔离加载失败的插件、在 UI 插件 Tab 中展示。

可参考 `examples/example_plugin/` 中的完整示例。

---

## 项目结构

```
midi/
├── app.py                        # GUI 入口
├── api.py                        # SDK：AppContext + 事件总线装饰器
├── start.bat           # Windows 一键启动
├── requirements.txt              # 运行依赖
├── requirements-dev.txt          # 开发依赖（含 pytest + pytest-qt）
│
├── midi/                         # MIDI 底层
│   ├── parser.py                 # ParsedMessage + mido → 统一字典解析
│   ├── engine.py                 # 端口枚举 / 收发引擎
│   ├── virtual_port.py           # 虚拟端口检测
│   └── keyboard_input.py         # 键盘名归一化 + 全局监听
│
├── core/                         # 业务逻辑
│   ├── events.py                 # EventBus（线程安全）
│   ├── bindings.py               # Binding / BindingSource / BindingConfig
│   ├── matcher.py                # MIDI + 键盘 → 信号匹配
│   ├── plugin.py                 # Plugin 抽象类
│   └── plugin_loader.py          # 插件自动发现与宿主
│
├── ui/                           # PyQt6 界面组件
│   ├── main_window.py            # 主窗口组装
│   ├── port_panel.py             # 端口选择 + 虚拟端口状态
│   ├── channel_matrix.py         # 16 通道脉冲矩阵
│   ├── log_view.py               # 实时日志表格
│   ├── send_panel.py             # 发送测试面板
│   ├── bindings_view.py          # 绑定编辑器
│   ├── plugin_tabs.py            # 插件面板 Tab 容器
│   └── style_qss.py              # 霓虹暗色主题
│
├── examples/
│   ├── sdk_demo.py               # Headless SDK 用法
│   └── example_plugin/           # 插件模板
│
└── tests/                        # pytest 测试覆盖所有模块
```

### 架构概览

```
 ┌─────────┐  端口枚举  ┌────────────┐  mido 回调   ┌──────────┐
 │ Port UI │──────────▶│ MidiEngine │──────────────▶│  parser  │
 └─────────┘           └──────┬─────┘               └────┬─────┘
                               │ send_message()            │ ParsedMessage
                               ▼                           ▼
                          ┌──────────┐              ┌────────────┐
                          │  mido     │              │  EventBus  │
                          └──────────┘              └──────┬─────┘
                                                           │ publish topics
                                                   ┌───────┴──────┐
                                                   ▼              ▼
                                              ┌──────────┐  ┌──────────┐
                                              │ Matcher  │  │ Plugin   │
                                              │ +Binding │  │ Host     │
                                              └────┬─────┘  └────┬─────┘
                                                   │ signal       │ create_panel()
                                                   ▼              ▼
                                              ┌────────────────────┐
                                              │     UI / 插件面板    │
                                              └────────────────────┘
```

数据流：**MIDI / 键盘 → EventBus → Matcher / Plugin → UI 更新**。Engine 只负责 I/O 和原始消息，业务逻辑和 UI 通过 EventBus 解耦。

---

## 配置

### 绑定文件

`config/bindings.json`，格式：

```json
{
  "version": 1,
  "bindings": [
    {
      "signal": "kick",
      "sources": [
        { "type": "midi", "channel": 9, "event": "note_on", "note": 36 }
      ],
      "virtual_midi": {
        "output_port": "CYD-MIDI 1",
        "channel": 0
      }
    }
  ]
}
```

### 虚拟端口检测策略

按优先级尝试：
1. 端口名包含 `loopMIDI` / `Virtual` 关键字（标准 loopMIDI / VirtualMIDISynth）
2. 同 base 名的输入/输出端口对（通用兼容，适配用户自定义的 CYD-MIDI、MIDI-PORT 等）

Windows rtmidi 自动附加的索引号后缀会被自动剔除再比较。

---

## 常见问题

### 端口列表为空

- 确认 MIDI 设备已连接并安装驱动
- 安装 [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) 创建虚拟端口做回环测试
- 点击 UI 右上"刷新设备"按钮

### `keyboard` 全局钩子启动失败

- 键盘监听降级为"窗口内聚焦捕获"，关闭后仍会收到 GUI 内按键
- 需要系统级监听时，以**管理员**权限运行

### 程序一闪而过

使用 `start.bat`，它自带 `pause` 并在首次运行时自动安装依赖。手动运行时观察控制台错误输出。

---

## 测试

```bash
# 全部
python -m pytest tests/ -v

# 只跑引擎 + 回环
python -m pytest tests/test_engine.py -v

# 虚拟端口相关
python -m pytest tests/test_virtual_port.py -v
```

测试覆盖：parser、bindings、matcher、engine（含 fake 端口路径）、keyboard_input（含全局钩子降级）、virtual_port、events、plugin_loader、UI 组件（用 QTest）、SDK AppContext。

回环测试需要真实虚拟端口（loopMIDI）存在，否则自动跳过。

---

## 开发路线图

- [x] 绑定触发主线接通（Matcher → 虚拟 MIDI 转发 / 键盘模拟 / 自定义回调）
- [x] 插件 Tab 自动渲染 `create_panel()` 面板
- [x] 打击垫插件 PadSentry：2×4 可视化 + pygame 音频 + Ogg-in-WAV 解包
- [x] 绑定导入 JSON 后不生效 bug（原地更新配置对象）
- [x] 绑定列表 checkbox 批量选择 + 批量删除
- [ ] 日志过滤 UI（类型下拉、通道过滤）
- [ ] MIDI 文件导入播放 / 录制导出
- [ ] 信号示波器（力度曲线 / CC 曲线）
- [ ] 命令行模式（`python app.py --port CYD-MIDI --log`）
- [ ] 自动绑定学习（监听最近一条 MIDI 生成绑定）

---

## Changelog

### v0.2 — 2026-09-17

**新增**

- **PadSentry 打击垫插件**（`plugins/pad_sentry/`）：2×4 可视化网格，监听 CC 102–109，pygame.mixer 音频播放
- **每 pad 4 channel round-robin 池**：彻底解决快速连打 / 多 pad 同时触发丢音
- **Ogg Vorbis 嵌 WAV 格式自动解包**：FL Studio Edison 导出的 0x674F format tag 文件，从 data chunk 抽出 OGG 流再转 PCM
- 绑定列表 **checkbox 多选 + 批量删除**
- 键盘绑定 key_out 分支（`ctrl+c` / `f1` 等全局按键模拟）
- `requirements-dev.txt` 开发依赖文件

**修复**

- **绑定 JSON 导入后不触发**：`_import()` 替换了 `self._config` 对象，但 `matcher._config` / `app.config` 仍持旧引用 → 改为原地更新 bindings 列表
- **`requirements.txt` 格式损坏**：`keyboard>=0.13.5pygame>=2.5` 粘在一行 → 分两行
- pygame mixer channel 数不足（原 8 条全局共享）→ 32 条（8 pad × 4 独占）
- `_ensure_samples` 自动合成采样会覆盖用户原始文件 → 改为 skip_existing + 手动 ffmpeg 解包
- 路径名含空格 / `#`（如 `Snare Hit HQ Rock #8.wav`）ffmpeg 转码参数报错

**基础设施**

- `.gitignore` 更新：排除 `_diag*.py`、`.pytest_cache/`、`docs/superpowers/`、`samples_backup/`、`*.log`、IDE 配置目录
- `启动MIDI调试工具.bat` → `start.bat` 重命名
- 78 pytest 测试全部通过

