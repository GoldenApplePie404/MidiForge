# MIDI 调试工具 设计文档

- 日期：2026-09-16
- 状态：已批准（用户确认方案 A）
- 技术栈：Python 3.11 + mido / python-rtmidi + PyQt6

## 1. 目标

制作一个可以被 MIDI 设备接入的 Python 桌面程序，用于调试 MIDI 通道：

- **双向调试**：实时监控 MIDI 输入（通道、类型、数值、原始字节），并可向设备/软件发送测试消息
- **绑定信号**：把 MIDI 事件和电脑键盘按键绑定为自定义命名信号，支持别名显示与配置导入导出（供游戏对接）
- **虚拟环境**：Python venv 开发；通过 loopMIDI 创建虚拟 MIDI 端口，让游戏或其他软件接入；键盘可模拟成 MIDI 消息发入虚拟端口
- **界面风格**：QSS 实现的霓虹风格，统一主题色 + 少量星空渐变，统一尺寸控件

## 2. 架构与组件

```
midi-debugger/
├── app.py               # 入口：创建 QApplication，组装主窗口
├── requirements.txt     # deps: mido, python-rtmidi, PyQt6, keyboard, pytest
├── midi/
│   ├── engine.py        # 端口枚举/打开/关闭，收发回调（mido + python-rtmidi）
│   ├── virtual_port.py  # loopMIDI 虚拟端口管理（检测/引导安装/状态）
│   ├── keyboard_input.py# 键盘监听：焦点内捕获 + 可选全局钩子
│   └── parser.py        # MIDI 消息解析：原始字节 hex、类型、通道、数值、易读描述
├── core/
│   ├── bindings.py      # 绑定模型 + JSON 导入导出
│   └── matcher.py       # 绑定匹配引擎：MIDI 事件/键盘按键 → 信号
└── ui/
    ├── main_window.py   # 主窗口与布局
    ├── channel_matrix.py# 16 通道实时活动矩阵
    ├── log_view.py      # 消息日志（含别名显示、过滤）
    ├── send_panel.py    # 测试消息发送
    ├── bindings_view.py # 绑定编辑器（添加/删除/导入导出）
    └── port_panel.py    # 设备与虚拟端口选择
```

分层原则：

- `midi/`：只管设备与协议，不涉及业务；可独立测试
- `core/`：绑定模型与匹配逻辑，不依赖 GUI；可独立测试
- `ui/`：纯展示与交互，调用 `core/` 与 `midi/` 的接口

## 3. 数据流

**输入流**：MIDI 设备 → 输入端口回调 → `parser` 解析 → `matcher` 查绑定 → UI（通道矩阵点亮 + 日志显示别名/原始数据）

**输出流（测试发送）**：发送面板选通道/类型/值 → 输出端口 → 目标设备

**键盘模拟流**：键盘按键 → `matcher` 匹配绑定 → 若绑定含 `virtual_midi` 字段，转成相应 MIDI 消息（可指定通道/音符/CC）写进虚拟端口 → 游戏从虚拟端口读到

**游戏对接流**：loopMIDI 虚拟端口作为程序的输出目标；游戏把该端口作为 MIDI 输入打开 → 绑定的信号（或键盘模拟的消息）直接进入游戏

## 4. 界面布局

```
┌──────────────────────────────────────────┐
│ 端口栏: [输入设备▼][输出设备▼][虚拟端口●]          │
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

## 7. 测试

- **单元测试**（pytest）：`parser` 各类型消息解析（raw bytes → 结构化）；`bindings` JSON 往返；`matcher` 精确/通配匹配
- **集成测试**：用 loopMIDI 端口回环（程序写 → 程序读）验证双向收发
- **手动测试**：键盘模拟 → 虚拟端口 → 游戏接收

## 8. 开发环境

- Python 3.11.9（已确认可用，venv 隔离）
- 依赖：mido、python-rtmidi、PyQt6、keyboard、pytest
- 虚拟 MIDI 端口依赖 loopMIDI（Windows），程序负责检测与引导安装