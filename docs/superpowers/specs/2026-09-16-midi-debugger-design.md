# MIDI 调试工具 设计文档

- 日期：2026-09-16
- 状态：已批准（用户确认方案 A；2026-09-16 增补"API 与插件平台"设计并获批准）
- 技术栈：Python 3.11 + mido / python-rtmidi + PyQt6

## 1. 目标

制作一个可以被 MIDI 设备接入的 Python 桌面程序，用于调试 MIDI 通道，并作为可二次开发的平台：

- **双向调试**：实时监控 MIDI 输入（通道、类型、数值、原始字节），并可向设备/软件发送测试消息
- **绑定信号**：把 MIDI 事件和电脑键盘按键绑定为自定义命名信号，支持别名显示与配置导入导出（供游戏对接）
- **虚拟环境**：Python venv 开发；通过 loopMIDI 创建虚拟 MIDI 端口，让游戏或其他软件接入；键盘可模拟成 MIDI 消息发入虚拟端口
- **API 与插件平台**：既是可 `import` 的库（SDK 模式，开发者自建音游/练习程序），也是带插件宿主的调试工具（插件可内嵌面板、可纯逻辑无界面），类似游戏 mod
- **界面风格**：QSS 实现的霓虹风格，统一主题色 + 少量星空渐变，统一尺寸控件

## 2. 架构与组件

```
midi-debugger/
├── app.py               # 入口：创建 QApplication，组装主窗口
├── api.py               # SDK 统一入口：create_app() → AppContext，重导出公开符号
├── requirements.txt     # deps: mido, python-rtmidi, PyQt6, keyboard, pytest
├── midi/
│   ├── engine.py        # 端口枚举/打开/关闭，收发回调（mido + python-rtmidi）
│   ├── virtual_port.py  # loopMIDI 虚拟端口管理（检测/引导安装/状态）
│   ├── keyboard_input.py# 键盘监听：焦点内捕获 + 可选全局钩子
│   └── parser.py        # MIDI 消息解析：原始字节 hex、类型、通道、数值、易读描述
├── core/
│   ├── bindings.py      # 绑定模型 + JSON 导入导出
│   ├── matcher.py       # 绑定匹配引擎：MIDI 事件/键盘按键 → 信号
│   ├── events.py        # EventBus：线程安全事件总线（订阅/发布/退订）
│   ├── plugin.py        # Plugin 接口：元数据 + on_activate/on_deactivate/create_panel
│   └── plugin_loader.py # 扫描 plugins/ 目录加载插件（失败隔离）
├── ui/
│   ├── main_window.py   # 主窗口与布局
│   ├── channel_matrix.py# 16 通道实时活动矩阵
│   ├── log_view.py      # 消息日志（含别名显示、过滤）
│   ├── send_panel.py    # 测试消息发送
│   ├── bindings_view.py # 绑定编辑器（添加/删除/导入导出）
│   ├── port_panel.py    # 设备与虚拟端口选择
│   └── plugin_host.py   # "插件"Tab：挂载插件面板，隔离异常
└── examples/
    ├── example_plugin/  # 示例插件：订阅事件 + 练习面板示例
    └── sdk_demo.py      # SDK 用法演示（无 GUI 纯逻辑）
```

分层原则：

- `midi/`：只管设备与协议，不涉及业务；可独立测试
- `core/`：绑定模型、匹配逻辑、事件总线、插件协议与加载，不依赖 GUI；可独立测试
- `ui/`：纯展示与交互，调用 `core/` 与 `midi/` 的接口
- `api.py`（SDK 门面）：把 `core/` + `midi/` 聚合为 `AppContext`，对外提供统一接口；`app.py` 与第三方程序都经它使用能力

## 3. 数据流

**输入流**：MIDI 设备 → 输入端口回调 → `parser` 解析 → `matcher` 查绑定 → 分发（通道矩阵点亮 + 日志显示别名/原始数据 + **EventBus 发布 `midi.message` / `midi.signal`**，插件与 SDK 订阅者同步收到）

**输出流（测试发送）**：发送面板选通道/类型/值 → 输出端口 → 目标设备（同时发布 `midi.sent`）

**键盘模拟流**：键盘按键 → `matcher` 匹配绑定 → 若绑定含 `virtual_midi` 字段，转成相应 MIDI 消息（可指定通道/音符/CC）写进虚拟端口 → 游戏从虚拟端口读到

**游戏对接流**：loopMIDI 虚拟端口作为程序的输出目标；游戏把该端口作为 MIDI 输入打开 → 绑定的信号（或键盘模拟的消息）直接进入游戏

## 4. 界面布局

```
┌──────────────────────────────────────────┐
│ 端口栏: [输入设备▼][输出设备▼][虚拟端口●]          │
├──────────────────────────────────────────┤
│ [主界面]  [插件]                                │    ← Tab 页
├───────────┬──────────────────────────────┤
│ 通道矩阵   │ 消息日志（时间/设备/通道/类型/值/别名)  │
│ (16通道    │ [过滤: 类型▾ 通道▾ 关键词▾] [清空]    │
│  实时点亮) │                              │
├───────────┴──────────────────────────────┤
│ 发送面板: [通道▾][类型▾][通道值][音符值] [发送]     │
├──────────────────────────────────────────┤
│ 绑定面板: 信号名 → 源(设备/通道/事件/值/按键) [±]    │
│           [导出 JSON][导入 JSON]                  │
└──────────────────────────────────────────┘
```

- **主界面**：上述调试界面
- **插件**：每个已加载插件一张子 Tab，`create_panel()` 返回的 QWidget 挂载于此；纯逻辑插件不显示 Tab

样式用 QSS 实现霓虹风格，主题色统一，有少量星空渐变点缀，控件尺寸统一。

## 5. 绑定模型与导出格式

绑定模型：**一个信号可以绑定多个源**。每个源类型为 `midi` 或 `keyboard`。

```json
{
  "version": 1,
  "bindings": [
    {"signal": "drum_hit",
     "sources": [{"type": "midi", "channel": 9, "event": "note_on", "note": 36},
                 {"type": "keyboard", "key": "space"}]},
    {"signal": "arrow_left", "sources": [{"type": "midi", "event": "cc", "cc": 1}]}
  ]
}
```

规则：

- MIDI 源各字段（channel/note/cc 等）支持**通配**（不填 = 任意）
- 键盘模拟的 MIDI 消息参数（通道/音符/CC 等）存在绑定项附带的 `virtual_midi` 字段，例如：

```json
{"signal": "arrow_left",
 "sources": [{"type": "keyboard", "key": "left"}],
 "virtual_midi": {"type": "note_on", "channel": 0, "note": 60, "velocity": 100}}
```

- 导出的 JSON 供游戏直接读取

## 6. 错误处理

- 设备拔出/打开失败 → 日志提示 + 设备列表自动刷新
- loopMIDI 未安装 → 弹窗提示并提供下载链接指引
- 绑定 JSON 损坏 → 提示错误并保留 `.bak` 备份，不覆盖原文件
- 发送失败（端口不可写）→ 界面提示
- **插件异常隔离**：插件加载/激活/运行/构造面板抛异常 → 记录到日志、标记禁用，宿主不崩溃

## 7. 测试

- **单元测试**（pytest）：`parser` 各类型消息解析（raw bytes → 结构化）；`bindings` JSON 往返；`matcher` 精确/通配匹配；`EventBus` 订阅/退订/发布；`plugin_loader` 发现与失败隔离
- **集成测试**：用 loopMIDI 端口回环（程序写 → 程序读）验证双向收发；SDK 事件链路（回环 → `midi.message` 事件被订阅者收到）
- **示例插件测试**：激活、事件触发、面板构造 smoke
- **手动测试**：键盘模拟 → 虚拟端口 → 游戏接收；运行示例插件验证面板渲染与事件流

## 8. API 与插件平台

**SDK 模式（框架用法）：**

```python
import api
app = api.create_app()          # AppContext: engine/bus/config/matcher/virtual_port

@api.on("midi.signal")          # 装饰器订阅
def on_signal(signal, source):
    print("信号:", signal, source)

app.start()                     # 无 GUI 运行（引擎 + 事件总线）
```

- `AppContext`：`engine`（收发）、`bus`（EventBus）、`config`（BindingConfig 读写）、`matcher`、`virtual_port`
- 开发者可自建任意界面（PyQt/Tkinter/CLI）；repo 根即导入根（`import api`），后续可打包为 `midi_debugger` 发行包（本期不做）

**插件模式（宿主内嵌，类似 mod）：**

```python
# plugins/my_game/plugin.py
from core.plugin import Plugin

class MyGame(Plugin):
    name = "my_game"
    def on_activate(self, app):            # app: AppContext
        app.bus.subscribe("midi.message", self._on_note)
    def create_panel(self):                # 可选，返回 QWidget
        return MyGamePanel()
    def _on_note(self, parsed):
        app.engine.send_message("note_on", channel=0, note=60)
```

- 插件 = `plugins/` 下子目录中的 `plugin.py`，同进程加载
- 生命周期：`on_activate(app)` / `on_deactivate()`；可选 `create_panel()` 挂进"插件"Tab
- 纯逻辑插件（无面板）完全支持

**事件主题：** `midi.message`（ParsedMessage）、`midi.signal`（信号触发）、`midi.sent`（发送回显）、`binding.changed`

**线程模型：** rtmidi 回调线程入队 → 程序主线程统一分发（UI 与 EventBus 同一循环，无需锁）

## 9. 开发环境

- Python 3.11.9（已确认可用，venv 隔离）
- 依赖：mido、python-rtmidi、PyQt6、keyboard、pytest
- 虚拟 MIDI 端口依赖 loopMIDI（Windows），程序负责检测与引导安装