# MIDI 调试工具 实现计划

> **面向 AI 代理的工作者:** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标:** 制作一个可被 MIDI 设备接入的 Python 桌面程序，用于调试 MIDI 通道（双向收发、通道矩阵、消息日志命名、键盘模拟、绑定信号导出、loopMIDI 虚拟端口对接），并作为可二次开发平台（SDK 框架 + 插件宿主）。

**架构:** 四层分离——`midi/`（设备与协议）、`core/`（绑定/匹配/事件总线/插件协议，不依赖 GUI）、`ui/`（PyQt6 展示与交互）、`api.py`（SDK 门面：AppContext 聚合引擎/总线/配置/匹配器，统一事件分发）。rtmidi 回调线程入队 → 主线程统一分发到 UI 与 EventBus；插件经总线订阅事件、可选挂载面板，异常隔离。

**技术栈:** Python 3.11 + mido / python-rtmidi（MIDI 收发）+ PyQt6（GUI）+ keyboard（可选全局键盘钩子）+ pytest / pytest-qt（测试）。

**规格:** `docs/superpowers/specs/2026-09-16-midi-debugger-design.md`

**环境前提:** Windows；venv 位于 `.venv`；loopMIDI 用于虚拟端口（仅集成测试/验收需要，核心功能不依赖）。

---

## 文件结构

```
midi-debugger/
├── app.py                       # 入口：QApplication + MainWindow
├── api.py                       # SDK 统一入口：create_app() → AppContext（事件分发/发送/无 GUI 调度）
├── requirements.txt             # 运行依赖
├── requirements-dev.txt         # 测试依赖
├── .gitignore
├── midi/
│   ├── __init__.py
│   ├── parser.py                # ParsedMessage 解析（raw hex/类型/通道/值/描述/匹配键）+ MIDO_TYPE_MAP
│   ├── engine.py                # 端口枚举/打开/收发（队列转发）
│   ├── virtual_port.py          # loopMIDI 检测与引导说明
│   └── keyboard_input.py        # 按键归一化（Qt 焦点捕获 + 可选全局钩子）
├── core/
│   ├── __init__.py
│   ├── bindings.py              # BindingSource/Binding/BindingConfig + JSON 导入导出（损坏备份 .bak）
│   ├── matcher.py               # Matcher：ParsedMessage/按键 → 信号集合
│   ├── events.py                # EventBus：线程安全事件总线
│   ├── plugin.py                # Plugin 接口（生命周期钩子）
│   └── plugin_loader.py         # PluginHost：扫描加载插件（失败隔离）
├── ui/
│   ├── __init__.py
│   ├── main_window.py           # 主窗口（总线驱动分发 + 插件 Tab）
│   ├── port_panel.py            # 输入/输出设备选择、刷新、虚拟端口状态
│   ├── channel_matrix.py        # 16 通道实时矩阵
│   ├── log_view.py              # 消息日志（过滤/别名/清空）
│   ├── send_panel.py            # 测试消息发送
│   ├── bindings_view.py        # 绑定编辑器 + 导入导出
│   ├── plugin_tabs.py           # 插件 Tab 容器（挂载/错误显示）
│   └── style_qss.py             # 霓虹主题 QSS
├── config/
│   └── bindings.json            # 默认空绑定配置（首次启动生成）
├── examples/
│   ├── example_plugin/          # 示例插件（练习面板）
│   └── sdk_demo.py              # SDK 无 GUI 用法演示
└── tests/
    ├── conftest.py
    ├── test_parser.py
    ├── test_bindings.py
    ├── test_matcher.py
    ├── test_engine.py
    ├── test_keyboard_input.py
    ├── test_virtual_port.py
    ├── test_events.py
    ├── test_plugin_loader.py
    ├── test_api.py
    └── test_ui_components.py
```

---

## 任务 1：项目脚手架与依赖

**文件：**
- 创建：`requirements.txt`
- 创建：`requirements-dev.txt`
- 创建：`.gitignore`
- 创建：`midi/__init__.py`、`core/__init__.py`、`ui/__init__.py`、`tests/__init__.py`

- [ ] **步骤 1：创建依赖与配置文件**

`requirements.txt`：

```text
mido>=1.3.2
python-rtmidi>=1.5.9
PyQt6>=6.7.0
keyboard>=0.13.5
```

`requirements-dev.txt`：

```text
-r requirements.txt
pytest>=8.0.0
pytest-qt>=4.4.0
```

`.gitignore`：

```text
.venv/
__pycache__/
*.pyc
.pytest_cache/
config/bindings.json
```

- [ ] **步骤 2：创建 venv 并安装依赖**

运行：`python -m venv .venv; .\.venv\Scripts\Activate.ps1; python -m pip install --upgrade pip; pip install -r requirements-dev.txt`
预期：安装成功，无报错。

- [ ] **步骤 3：创建包骨架**

创建四个空 `__init__.py`（`midi/`、`core/`、`ui/`、`tests/`）。

- [ ] **步骤 4：验证导入**

运行：`.\.venv\Scripts\python -c "import mido, PyQt6; print(mido.__version__)"`
预期：打印 mido 版本号。

- [ ] **步骤 5：Commit**

```bash
git add .gitignore requirements.txt requirements-dev.txt midi/ core/ ui/ tests/
git commit -m "chore: 项目脚手架与依赖"
```

---

## 任务 2：消息解析器 `midi/parser.py`（TDD）

**文件：**
- 创建：`midi/parser.py`
- 测试：`tests/test_parser.py`

**设计：** `ParsedMessage` 提供结构化字段 + `event_key()` 供 matcher 匹配（格式 `type:channel:value`，无值段表示通配）。类型归一化：`note_on / note_off / poly_aftertouch / cc / program_change / aftertouch / pitch_bend`；系统实时消息归类为 `system`。

- [ ] **步骤 1：编写失败的测试**

`tests/test_parser.py`：

```python
import mido
import pytest
from midi.parser import parse

def test_note_on():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert p.type == "note_on"
    assert p.channel == 9
    assert p.values["note"] == 36
    assert p.values["velocity"] == 100
    assert p.event_key() == "note_on:9:36"

def test_note_off():
    p = parse(mido.Message("note_off", channel=0, note=60))
    assert p.type == "note_off"
    assert p.event_key() == "note_off:0:60"

def test_cc():
    p = parse(mido.Message("control_change", channel=1, control=7, value=64))
    assert p.type == "cc"
    assert p.event_key() == "cc:1:7"

def test_pitch_bend():
    p = parse(mido.Message("pitchwheel", channel=3, pitch=8192))
    assert p.type == "pitch_bend"
    assert p.values["pitch"] == 8192
    assert p.event_key() == "pitch_bend:3"

def test_program_change():
    p = parse(mido.Message("program_change", channel=5, program=10))
    assert p.type == "program_change"
    assert p.event_key() == "program_change:5:10"

def test_channel_aftertouch():
    p = parse(mido.Message("aftertouch", channel=4, value=50))
    assert p.type == "aftertouch"
    assert p.event_key() == "aftertouch:4"

def test_poly_aftertouch():
    p = parse(mido.Message("polytouch", channel=6, note=60, value=40))
    assert p.type == "poly_aftertouch"
    assert p.event_key() == "poly_aftertouch:6:60"

def test_system_message():
    p = parse(mido.Message("clock"))
    assert p.type == "system"
    assert p.channel is None

def test_raw_hex():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert p.raw_hex == "99 24 64"

def test_description_contains_info():
    p = parse(mido.Message("note_on", channel=9, note=36, velocity=100))
    assert "36" in p.description
    assert "ch9" in p.description
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_parser.py -v`
预期：FAIL（`from midi.parser import parse` 报 ModuleNotFoundError）。

- [ ] **步骤 3：编写最少实现**

`midi/parser.py`：

```python
from dataclasses import dataclass, field
from typing import Optional

from mido import Message

# mido 类型名 → 归一化类型名（用于匹配与显示）
_TYPE_MAP = {
    "note_on": "note_on",
    "note_off": "note_off",
    "polytouch": "poly_aftertouch",
    "control_change": "cc",
    "program_change": "program_change",
    "aftertouch": "aftertouch",
    "pitchwheel": "pitch_bend",
}

_SYSTEM_TYPES = {"clock", "start", "stop", "continue", "active_sensing", "reset", "sysex", "tune_request"}

# 归一化类型名 → mido 原生类型名（发送时反向转换）
MIDO_TYPE_MAP = {v: k for k, v in _TYPE_MAP.items()}


@dataclass
class ParsedMessage:
    """解析后的 MIDI 消息，供 UI 展示与匹配引擎使用。"""

    type: str
    channel: Optional[int]
    values: dict = field(default_factory=dict)
    raw_hex: str = ""
    description: str = ""

    def event_key(self) -> str:
        """匹配键：type:channel:主值。省略尾段表示通配匹配。"""
        parts = [self.type]
        if self.channel is not None:
            parts.append(str(self.channel))
            if self.type in ("note_on", "note_off", "poly_aftertouch") and "note" in self.values:
                parts.append(str(self.values["note"]))
            elif self.type == "cc" and "control" in self.values:
                parts.append(str(self.values["control"]))
        return ":".join(parts)


def _describe(msg: Message) -> str:
    if msg.type == "note_on":
        return f"音符开 ch{msg.channel} note {msg.note} vel {msg.velocity}"
    if msg.type == "note_off":
        return f"音符关 ch{msg.channel} note {msg.note} vel {msg.velocity}"
    if msg.type == "control_change":
        return f"CC ch{msg.channel} CC{msg.control} = {msg.value}"
    if msg.type == "pitchwheel":
        return f"弯音 ch{msg.channel} pitch {msg.pitch}"
    if msg.type == "program_change":
        return f"音色 ch{msg.channel} program {msg.program}"
    if msg.type == "aftertouch":
        return f"通道触后 ch{msg.channel} value {msg.value}"
    if msg.type == "polytouch":
        return f"复音触后 ch{msg.channel} note {msg.note} value {msg.value}"
    return f"系统消息 {msg.type}"


def parse(msg: Message) -> ParsedMessage:
    """把 mido.Message 解析为 ParsedMessage。"""
    raw_hex = " ".join(f"{b:02X}" for b in msg.bytes())
    if msg.type in _SYSTEM_TYPES:
        return ParsedMessage(type="system", channel=None, raw_hex=raw_hex, description=f"系统消息 {msg.type}")
    mtype = _TYPE_MAP[msg.type]
    channel = getattr(msg, "channel", None)
    values = {k: v for k, v in msg.dict().items() if k != "channel"}
    return ParsedMessage(type=mtype, channel=channel, values=values, raw_hex=raw_hex, description=_describe(msg))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_parser.py -v`
预期：PASS（11 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add midi/parser.py tests/test_parser.py
git commit -m "feat: 消息解析器解析 MIDI 消息为结构化数据"
```

---

## 任务 3：绑定模型与 JSON `core/bindings.py`（TDD）

**文件：**
- 创建：`core/bindings.py`
- 测试：`tests/test_bindings.py`

**设计：** `BindingSource`（type=midi|keyboard；midi 字段 None 表示通配）、`Binding`（signal + sources + virtual_midi）、`BindingConfig`（JSON 往返、损坏时备份 `.bak` 并抛异常）。注意：把"匹配逻辑"留给任务 4 的 matcher，本任务只定义结构与序列化。

- [ ] **步骤 1：编写失败的测试**

`tests/test_bindings.py`：

```python
import json
import pytest
from core.bindings import Binding, BindingConfig, BindingSource

def test_source_from_dict_midi():
    s = BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})
    assert s.type == "midi" and s.channel == 9 and s.note == 36

def test_source_from_dict_keyboard():
    s = BindingSource.from_dict({"type": "keyboard", "key": "space"})
    assert s.type == "keyboard" and s.key == "space"

def test_source_to_dict_roundtrip():
    s = BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})
    assert BindingSource.from_dict(s.to_dict()) == s

def test_binding_to_dict_roundtrip():
    b = Binding(signal="drum_hit", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36}),
        BindingSource.from_dict({"type": "keyboard", "key": "space"}),
    ], virtual_midi={"type": "note_on", "channel": 0, "note": 60, "velocity": 100})
    assert Binding.from_dict(b.to_dict()) == b

def test_config_roundtrip_file(tmp_path):
    cfg = BindingConfig(bindings=[
        Binding(signal="drum_hit", sources=[BindingSource.from_dict({"type": "keyboard", "key": "space"})])
    ])
    path = tmp_path / "bindings.json"
    cfg.to_file(str(path))
    again = BindingConfig.from_file(str(path))
    assert again == cfg

def test_config_corrupt_file_backs_up(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="绑定配置损坏"):
        BindingConfig.from_file(str(path))
    assert (tmp_path / "bindings.json.bak").exists()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_bindings.py -v`
预期：FAIL（ModuleNotFoundError: core.bindings）。

- [ ] **步骤 3：编写最少实现**

`core/bindings.py`：

```python
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BindingSource:
    """绑定源。type=midi 时 channel/event/note/cc 任一为 None 表示通配；type=keyboard 时用 key。"""

    type: str
    channel: Optional[int] = None
    event: Optional[str] = None
    note: Optional[int] = None
    cc: Optional[int] = None
    key: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "BindingSource":
        try:
            src = cls(
                type=d["type"],
                channel=d.get("channel"),
                event=d.get("event"),
                note=d.get("note"),
                cc=d.get("cc"),
                key=d.get("key"),
            )
        except KeyError as exc:
            raise ValueError(f"绑定源缺少字段: {exc}") from exc
        if src.type not in ("midi", "keyboard"):
            raise ValueError(f"未知绑定源类型: {src.type}")
        return src

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Binding:
    signal: str
    sources: list = field(default_factory=list)
    virtual_midi: Optional[dict] = None

    @classmethod
    def from_dict(cls, d: dict) -> "Binding":
        return cls(
            signal=d["signal"],
            sources=[BindingSource.from_dict(s) for s in d.get("sources", [])],
            virtual_midi=d.get("virtual_midi"),
        )

    def to_dict(self) -> dict:
        d = {"signal": self.signal, "sources": [s.to_dict() for s in self.sources]}
        if self.virtual_midi:
            d["virtual_midi"] = self.virtual_midi
        return d


@dataclass
class BindingConfig:
    version: int = 1
    bindings: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "BindingConfig":
        return cls(version=d.get("version", 1), bindings=[Binding.from_dict(b) for b in d.get("bindings", [])])

    def to_dict(self) -> dict:
        return {"version": self.version, "bindings": [b.to_dict() for b in self.bindings]}

    @classmethod
    def from_file(cls, path: str) -> "BindingConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
            raise ValueError(f"绑定配置损坏: {exc}") from exc

    def to_file(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_bindings.py -v`
预期：PASS（6 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add core/bindings.py tests/test_bindings.py
git commit -m "feat: 绑定模型与 JSON 导入导出（损坏备份）"
```

---

## 任务 4：匹配引擎 `core/matcher.py`（TDD）

**文件：**
- 创建：`core/matcher.py`
- 修改：`tests/test_matcher.py`（创建）
- 依赖：任务 2 的 `ParsedMessage`、任务 3 的 `BindingConfig`

**设计：** `Matcher(config)` 提供 `signals_for_parsed(parsed)` 与 `signals_for_key(key)`。MIDI 匹配：源 event 必须精确等于 parsed.type；channel/note/cc 依次对比，`None`（通配）跳过；`event_key` 的"主值"仅当源指定了对应字段时对比。

- [ ] **步骤 1：编写失败的测试**

`tests/test_matcher.py`：

```python
from core.bindings import Binding, BindingConfig, BindingSource
from core.matcher import Matcher
from midi.parser import ParsedMessage


def make_config(sources, signal="sig"):
    return BindingConfig(bindings=[Binding(signal=signal, sources=[BindingSource.from_dict(s) for s in sources])])


def test_exact_note_on_match():
    cfg = make_config([{"type": "midi", "event": "note_on", "channel": 9, "note": 36}])
    m = Matcher(cfg)
    parsed = ParsedMessage(type="note_on", channel=9, values={"note": 36, "velocity": 100})
    assert m.signals_for_parsed(parsed) == {"sig"}


def test_wildcard_channel():
    cfg = make_config([{"type": "midi", "event": "cc", "cc": 1}])  # channel 未指定 = 任意
    m = Matcher(cfg)
    p1 = ParsedMessage(type="cc", channel=3, values={"control": 1, "value": 64})
    p2 = ParsedMessage(type="cc", channel=7, values={"control": 1, "value": 10})
    assert m.signals_for_parsed(p1) == {"sig"}
    assert m.signals_for_parsed(p2) == {"sig"}


def test_no_match_on_different_note():
    cfg = make_config([{"type": "midi", "event": "note_on", "note": 36}])
    m = Matcher(cfg)
    parsed = ParsedMessage(type="note_on", channel=0, values={"note": 60, "velocity": 1})
    assert m.signals_for_parsed(parsed) == set()


def test_multi_source_one_signal():
    cfg = make_config([
        {"type": "midi", "event": "note_on", "note": 36},
        {"type": "keyboard", "key": "space"},
    ])
    m = Matcher(cfg)
    assert m.signals_for_parsed(ParsedMessage(type="note_on", channel=9, values={"note": 36})) == {"sig"}
    assert m.signals_for_key("space") == {"sig"}


def test_event_type_must_match():
    cfg = make_config([{"type": "midi", "event": "note_on"}])
    m = Matcher(cfg)
    assert m.signals_for_parsed(ParsedMessage(type="cc", channel=0, values={"control": 1})) == set()


def test_keyboard_key_normalized():
    cfg = make_config([{"type": "keyboard", "key": "Left"}])
    m = Matcher(cfg)
    assert m.signals_for_key("left") == {"sig"}
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_matcher.py -v`
预期：FAIL（ModuleNotFoundError: core.matcher）。

- [ ] **步骤 3：编写最少实现**

`core/matcher.py`：

```python
from typing import Set

from core.bindings import BindingConfig, BindingSource
from midi.parser import ParsedMessage


class Matcher:
    """把 MIDI 事件或按键映射为命中的信号集合。"""

    def __init__(self, config: BindingConfig):
        self._config = config

    def signals_for_parsed(self, parsed: ParsedMessage) -> Set[str]:
        hits = set()
        for b in self._config.bindings:
            if any(_source_matches_parsed(s, parsed) for s in b.sources):
                hits.add(b.signal)
        return hits

    def signals_for_key(self, key: str) -> Set[str]:
        key = key.lower()
        hits = set()
        for b in self._config.bindings:
            if any(_source_matches_key(s, key) for s in b.sources):
                hits.add(b.signal)
        return hits


def _source_matches_parsed(src: BindingSource, parsed: ParsedMessage) -> bool:
    if src.type != "midi":
        return False
    if src.event and src.event != parsed.type:
        return False
    if src.channel is not None and src.channel != parsed.channel:
        return False
    if src.note is not None and parsed.values.get("note") != src.note:
        return False
    if src.cc is not None and parsed.values.get("control") != src.cc:
        return False
    return True


def _source_matches_key(src: BindingSource, key: str) -> bool:
    return src.type == "keyboard" and src.key is not None and src.key.lower() == key
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_matcher.py -v`
预期：PASS（6 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add core/matcher.py tests/test_matcher.py
git commit -m "feat: 绑定匹配引擎（MIDI 事件与键盘按键 → 信号）"
```

---

## 任务 5：MIDI 端口引擎 `midi/engine.py`（TDD + 回环集成）

**文件：**
- 创建：`midi/engine.py`
- 测试：`tests/test_engine.py`
- 依赖：任务 2 的 `parse`

**设计：** `MidiEngine` 封装 mido 输入/输出端口。输入回调把 `ParsedMessage` 推入 `queue.Queue`（线程安全），UI 用 QTimer 轮询 `drain()`。`list_inputs/list_outputs` 枚举端口；`open_input/open_output/send_message/close_all/refresh`。集成测试：检测到 loopMIDI 端口则做写→读回环；否则 skip（`pytest.mark.skipif`）。

- [ ] **步骤 1：编写失败的测试**

`tests/test_engine.py`：

```python
import time

import pytest
import mido

from midi.engine import MidiEngine


def _has_loopmidi() -> bool:
    return any("loopMIDI" in n or "Virtual" in n for n in mido.get_output_names())


@pytest.mark.skipif(not _has_loopmidi(), reason="需要 loopMIDI 虚拟端口")
def test_loopback_roundtrip():
    port = next(n for n in mido.get_output_names() if "loopMIDI" in n or "Virtual" in n)
    eng = MidiEngine()
    eng.open_output(port)
    eng.open_input(port)  # 消息由回调进队列
    eng.send_message("note_on", channel=9, note=36, velocity=100)
    time.sleep(0.3)
    msgs = eng.drain()
    assert any(p.type == "note_on" and p.values.get("note") == 36 for p in msgs)
    eng.close_all()


def test_send_missing_output_raises():
    eng = MidiEngine()
    with pytest.raises(RuntimeError, match="未打开输出端口"):
        eng.send_message("note_on", channel=0, note=60)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_engine.py -v`
预期：FAIL（ModuleNotFoundError: midi.engine）或 RuntimeError 用例未通过。

- [ ] **步骤 3：编写最少实现**

`midi/engine.py`：

```python
import queue
from typing import Callable, List, Optional

import mido

from midi.parser import MIDO_TYPE_MAP, ParsedMessage, parse


class MidiEngine:
    """枚举/打开/收发 MIDI 端口。输入回调运行在 rtmidi 线程，消息经队列转交 UI。"""

    def __init__(self):
        self._queue: "queue.Queue[ParsedMessage]" = queue.Queue()
        self._in_ports: List[mido.ports.BaseInput] = []
        self._out_port: Optional[mido.ports.BaseOutput] = None

    # ---- 枚举 ----
    def list_inputs(self) -> List[str]:
        return mido.get_input_names()

    def list_outputs(self) -> List[str]:
        return mido.get_output_names()

    # ---- 打开/关闭 ----
    def open_input(self, name: str, callback: Optional[Callable[[ParsedMessage], None]] = None) -> None:
        def _on_msg(msg: mido.Message) -> None:
            parsed = parse(msg)
            self._queue.put(parsed)
            if callback:
                callback(parsed)

        port = mido.open_input(name, callback=_on_msg)
        self._in_ports.append(port)

    def open_output(self, name: str) -> None:
        self.close_output()
        self._out_port = mido.open_output(name)

    def close_inputs(self) -> None:
        for p in self._in_ports:
            p.close()
        self._in_ports.clear()

    def close_output(self) -> None:
        if self._out_port is not None:
            self._out_port.close()
            self._out_port = None

    def close_all(self) -> None:
        self.close_inputs()
        self.close_output()

    # ---- 消费 ----
    def drain(self) -> List[ParsedMessage]:
        out = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    # ---- 发送 ----
    def send_message(self, type: str, channel: int = 0, **kwargs) -> None:
        if self._out_port is None:
            raise RuntimeError("未打开输出端口")
        mido_type = MIDO_TYPE_MAP.get(type, type)
        self._out_port.send(mido.Message(mido_type, channel=channel, **kwargs))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_engine.py -v`
预期：回环测试在装有 loopMIDI 的机器上 PASS；未装时 skip；`test_send_missing_output_raises` 恒 PASS。

- [ ] **步骤 5：Commit**

```bash
git add midi/engine.py tests/test_engine.py
git commit -m "feat: MIDI 端口引擎（双向收发 + 线程安全队列）"
```

---

## 任务 6：键盘归一化与监听 `midi/keyboard_input.py`（TDD）

**文件：**
- 创建：`midi/keyboard_input.py`
- 测试：`tests/test_keyboard_input.py`

**设计：** 提供 `normalize_key(name)`（Qt 键名/`keyboard` 库名 → 规范小写名），`KeyboardWatcher` 同时支持 Qt 事件过滤器（应用焦点内）与可选全局钩子（`keyboard.hook`）。全局钩子失败（如权限）不阻塞启动，降级为焦点内捕获。

- [ ] **步骤 1：编写失败的测试**

`tests/test_keyboard_input.py`：

```python
from midi.keyboard_input import normalize_key

def test_qt_key_names():
    assert normalize_key("Qt.Key_Left") == "left"
    assert normalize_key("Qt.Key_Space") == "space"
    assert normalize_key("Qt.Key_A") == "a"

def test_keyboard_lib_names():
    assert normalize_key("left") == "left"
    assert normalize_key("SPACE") == "space"

def test_single_char():
    assert normalize_key("w") == "w"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_keyboard_input.py -v`
预期：FAIL（ModuleNotFoundError: midi.keyboard_input）。

- [ ] **步骤 3：编写最少实现**

`midi/keyboard_input.py`：

```python
import re
from typing import Callable, Dict, Optional


def normalize_key(name: str) -> str:
    """把 Qt 键名或 keyboard 库键名规范为小写写法。"""
    name = name.strip()
    if name.startswith("Qt.Key_"):
        name = name[len("Qt.Key_"):]
    elif name.startswith("Key."):
        name = name[len("Key."):]
    name = name.lower()
    # keyboard 库的空格/连字符/枚举写法
    name = re.sub(r"[\s-]+", "_", name)
    name = re.sub(r"(_|\s)+", "_", name).strip("_")
    if name.endswith("_key"):
        name = name[: -len("_key")]
    return name


class KeyboardWatcher:
    """键盘监听：焦点内捕获（Qt 事件过滤器）与可选全局钩子。"""

    def __init__(self, on_key: Callable[[str], None], use_global_hook: bool = False):
        self.on_key = on_key
        self._global_hook: Optional[object] = None
        self._cur_keys: Dict[str, str] = {}
        if use_global_hook:
            self._try_start_global()

    def _try_start_global(self) -> bool:
        try:
            import keyboard  # type: ignore  # 仅 Windows/Linux 可用

            keyboard.hook(self._global_callback)
            self._global_hook = True
            return True
        except Exception:
            self._global_hook = None
            return False

    def _global_callback(self, event) -> None:
        if event.event_type == "down":
            self.on_key(normalize_key(event.name))

    def qevent_key(self, key_name: str) -> None:
        """由 Qt 事件过滤器在返回之前调用（焦点内捕获）。"""
        self.on_key(normalize_key(key_name))

    def stop_global(self) -> None:
        if self._global_hook:
            import keyboard  # type: ignore

            keyboard.unhook_all()
            self._global_hook = None

    @property
    def global_active(self) -> bool:
        return self._global_hook is not None
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_keyboard_input.py -v`
预期：PASS（3 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add midi/keyboard_input.py tests/test_keyboard_input.py
git commit -m "feat: 键盘按键归一化与监听"
```

---

## 任务 7：loopMIDI 虚拟端口检测 `midi/virtual_port.py`（TDD）

**文件：**
- 创建：`midi/virtual_port.py`
- 测试：`tests/test_virtual_port.py`

**设计：** `detect_virtual_out_port(output_names)` 在输出端口列表中识别 loopMIDI/Virtual 名字；`setup_guide()` 返回下载指引文本（用于 UI 提示）。

- [ ] **步骤 1：编写失败的测试**

`tests/test_virtual_port.py`：

```python
from midi.virtual_port import detect_virtual_out_port, setup_guide

def test_detect_loopmidi():
    names = ["Microsoft GS Wavetable Synth", "loopMIDI Port", "MIDI 4x4"]
    assert detect_virtual_out_port(names) == "loopMIDI Port"

def test_detect_other_virtual():
    names = ["VirtualMIDISynth", "foo"]
    assert detect_virtual_out_port(names) == "VirtualMIDISynth"

def test_no_virtual():
    assert detect_virtual_out_port(["Microsoft GS Wavetable Synth"]) is None

def test_guide_has_download_hint():
    assert "loopMIDI" in setup_guide() and "http" in setup_guide()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_virtual_port.py -v`
预期：FAIL（ModuleNotFoundError: midi.virtual_port）。

- [ ] **步骤 3：编写最少实现**

`midi/virtual_port.py`：

```python
from typing import List, Optional

LOOPMIDI_URL = "https://www.tobias-erichsen.de/software/loopmidi.html"


def detect_virtual_out_port(output_names: List[str]) -> Optional[str]:
    """在输出端口名中识别虚拟 MIDI 端口（loopMIDI / Virtual MIDI）。"""
    for name in output_names:
        lower = name.lower()
        if "loopmidi" in lower or "virtual" in lower:
            return name
    return None


def setup_guide() -> str:
    return f"未检测到虚拟 MIDI 端口。请安装 loopMIDI（{LOOPMIDI_URL}）并创建至少一个虚拟端口，然后重启本程序。"
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_virtual_port.py -v`
预期：PASS（4 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add midi/virtual_port.py tests/test_virtual_port.py
git commit -m "feat: loopMIDI 虚拟端口检测与引导"
```

---

## 任务 8：霓虹主题样式 `ui/style_qss.py`

**文件：**
- 创建：`ui/style_qss.py`

**设计：** 统一 QSS 常量主题。主题色（青-紫霓虹）：主色 `#00E5FF`、强调色 `#B388FF`、背景 `#0B0F1A`、面板 `#141A2A`、文字 `#E6E9F0`、错误 `#FF5252`。控件统一圆角/字号。

- [ ] **步骤 1：创建样式模块**

`ui/style_qss.py`：

```python
"""霓虹主题 QSS（统一主题色，少量星空渐变点缀）。"""

BG = "#0B0F1A"
PANEL = "#141A2A"
CARD = "#1B2338"
PRIMARY = "#00E5FF"
ACCENT = "#B388FF"
TEXT = "#E6E9F0"
MUTED = "#7A8499"
ERROR = "#FF5252"
BORDER = "#232D47"

GLOBAL_QSS = f"""
* {{ font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }}
QMainWindow, QWidget {{ background-color: {BG}; color: {TEXT}; }}
QFrame#panel {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                 stop:0 {PANEL}, stop:1 {CARD});
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QPushButton {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}}
QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}
QPushButton:checked {{ background-color: {PRIMARY}; color: {BG}; border-color: {PRIMARY}; }}
QComboBox, QSpinBox, QLineEdit {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
}}
QComboBox:hover, QSpinBox:hover, QLineEdit:focus {{ border-color: {PRIMARY}; }}
QTableWidget {{
    background-color: {PANEL};
    gridline-color: {BORDER};
    alternate-background-color: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QHeaderView::section {{
    background-color: {CARD};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 8px;
}}
QStatusBar {{ background-color: {PANEL}; color: {MUTED}; }}
QLabel#sectionTitle {{ color: {PRIMARY}; font-size: 13px; font-weight: 600; }}
QCheckBox {{ color: {TEXT}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
"""
```

- [ ] **步骤 2：Commit**

```bash
git add ui/style_qss.py
git commit -m "feat: 霓虹主题 QSS"
```

---

## 任务 9：UI 组件——端口栏与通道矩阵

**文件：**
- 创建：`ui/port_panel.py`
- 创建：`ui/channel_matrix.py`
- 测试：`tests/test_ui_components.py`

**设计：** `PortPanel`：输入/输出 QComboBox + 刷新按钮 + 虚拟端口状态 label。`ChannelMatrix`：16 列 QLabel，收到消息调 `pulse(channel)` 点亮主题色，QTimer 0.5s 后暗淡。

- [ ] **步骤 1：编写失败的测试**

`tests/test_ui_components.py`（pytest-qt 冒烟测试）：

```python
from PyQt6.QtWidgets import QApplication
from ui.channel_matrix import ChannelMatrix


def test_channel_matrix_16_cells(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    assert len(m._cells) == 16


def test_channel_matrix_pulse_colors(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    m.pulse(3)
    assert m._cells[3].styleSheet() != ""
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.channel_matrix）。

- [ ] **步骤 3：编写最少实现**

`ui/port_panel.py`：

```python
from typing import Callable, List

from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from midi.virtual_port import detect_virtual_out_port, setup_guide
from ui import style_qss as QSS


class PortPanel(QFrame):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.engine = engine
        self.input_combo = QComboBox()
        self.output_combo = QComboBox()
        self.virtual_label = QLabel("虚拟端口: 未检测")
        self.virtual_label.setObjectName("virtualStatus")
        self.refresh_btn = QPushButton("刷新设备")
        self.refresh_btn.clicked.connect(self.refresh)
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(QLabel("输入设备"))
        layout.addWidget(self.input_combo, 3)
        layout.addWidget(QLabel("输出设备"))
        layout.addWidget(self.output_combo, 3)
        self.virtual_label.setStyleSheet(f"color: {QSS.MUTED};")
        layout.addWidget(self.virtual_label, 2)
        layout.addWidget(self.refresh_btn)

    def refresh(self) -> None:
        inputs = self.engine.list_inputs()
        outputs = self.engine.list_outputs()
        for combo, names in ((self.input_combo, inputs), (self.output_combo, outputs)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（无）")
            combo.addItems(names)
            combo.blockSignals(False)
        virtual = detect_virtual_out_port(outputs)
        if virtual:
            self.virtual_label.setText(f"虚拟端口: {virtual}")
            self.virtual_label.setStyleSheet(f"color: {QSS.PRIMARY};")
        else:
            self.virtual_label.setText(setup_guide())
            self.virtual_label.setStyleSheet(f"color: {QSS.ERROR};")
```

`ui/channel_matrix.py`：

```python
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QGridLayout, QLabel

from ui import style_qss as QSS


class ChannelMatrix(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        grid = QGridLayout(self)
        self._cells = []
        for ch in range(16):
            cell = QLabel(f"CH{ch + 1}")
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMinimumSize(44, 28)
            cell.setStyleSheet(self._dull_style())
            grid.addWidget(cell, ch // 4, ch % 4)
            self._cells.append(cell)

    @staticmethod
    def _dull_style() -> str:
        return f"background-color: {QSS.PANEL}; color: {QSS.MUTED}; border-radius:5px; border:1px solid {QSS.BORDER};"

    @staticmethod
    def _lit_style() -> str:
        return (f"background-color: {QSS.PRIMARY}; color: {QSS.BG}; font-weight:600;"
                f"border-radius:5px; border:1px solid {QSS.PRIMARY};")

    def pulse(self, channel: int) -> None:
        if not 0 <= channel < 16:
            return
        cell = self._cells[channel]
        cell.setStyleSheet(self._lit_style())
        QTimer.singleShot(500, lambda: cell.setStyleSheet(self._dull_style()))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS（2 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add ui/port_panel.py ui/channel_matrix.py tests/test_ui_components.py
git commit -m "feat: 端口栏与 16 通道实时矩阵"
```

---

## 任务 10：消息日志视图 `ui/log_view.py`

**文件：**
- 创建：`ui/log_view.py`
- 测试：追加到 `tests/test_ui_components.py`

**设计：** QTableWidget：时间 / 来源 / 通道 / 类型 / 值 / 原始 hex / 别名（绑定命中时显示信号名，未命中显示描述）。支持类型、通道、关键词过滤与清空。列配置在 `col_index.py` 常量。

- [ ] **步骤 1：编写失败的测试（追加）**

`tests/test_ui_components.py` 追加：

```python
from midi.parser import ParsedMessage
from ui.log_view import LogView

LOG_COLUMNS = ("time", "source", "channel", "type", "value", "hex", "alias")


def log_row_dict(view, row):
    return {c: view.table.item(row, i).text() for i, c in enumerate(LOG_COLUMNS)}


def test_log_append_message(qtbot):
    v = LogView()
    qtbot.addWidget(v)
    v.append_message(ParsedMessage(type="note_on", channel=9, values={"note": 36, "velocity": 100},
                                   raw_hex="99 24 64", description="音符开 ch9 note 36"))
    assert v.table.rowCount() == 1
    assert log_row_dict(v, 0)["type"] == "note_on"
    assert log_row_dict(v, 0)["channel"] == "9"
    assert log_row_dict(v, 0)["hex"] == "99 24 64"


def test_log_alias_display(qtbot):
    v = LogView()
    qtbot.addWidget(v)
    v.append_message(ParsedMessage(type="cc", channel=1, values={"control": 7}, raw_hex="B1 07",
                                   description="CC ch1 CC7"), alias="鼓音量")
    assert log_row_dict(v, 0)["alias"] == "鼓音量"


def test_log_clear(qtbot):
    v = LogView()
    qtbot.addWidget(v)
    v.append_message(ParsedMessage(type="cc", channel=0, values={"control": 1}, raw_hex="B0 01"))
    v.clear_log()
    assert v.table.rowCount() == 0


def test_log_filter_type(qtbot):
    v = LogView()
    qtbot.addWidget(v)
    v.append_message(ParsedMessage(type="cc", channel=0, values={"control": 1}, raw_hex="B0 01"))
    v.append_message(ParsedMessage(type="note_on", channel=0, values={"note": 60}, raw_hex="90 3C"))
    v.set_type_filter(["cc"])
    assert v.table.rowCount() == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.log_view）。

- [ ] **步骤 3：编写最少实现**

`ui/log_view.py`：

```python
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QFrame

from midi.parser import ParsedMessage
from ui import style_qss as QSS


class LogView(QFrame):
    COLUMNS = ("时间", "来源", "通道", "类型", "数值", "原始 hex", "别名/描述")
    KEYS = ("time", "source", "channel", "type", "value", "hex", "alias")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._rows: List[dict] = []
        self._type_filter: set = set()

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self._title = QLabel("消息日志")
        self._title.setObjectName("sectionTitle")
        self.clear_btn = QPushButton("清空")
        bar.addWidget(self._title)
        bar.addStretch(1)
        bar.addWidget(self.clear_btn)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)
        self.clear_btn.clicked.connect(self.clear_log)

    # ---- 数据 ----
    def append_message(self, parsed: ParsedMessage, source: str = "in", alias: Optional[str] = None) -> None:
        value = _format_values(parsed)
        row = {
            "time": _now_str(),
            "source": source,
            "channel": str(parsed.channel + 1) if parsed.channel is not None else "-",
            "type": parsed.type,
            "value": value,
            "hex": parsed.raw_hex,
            "alias": alias or parsed.description,
        }
        self._rows.append(row)
        if parsed.type not in self._type_filter and self._type_filter:
            return
        self._add_table_row(row)

    def set_type_filter(self, types: List[str]) -> None:
        self._type_filter = set(types)
        self._rebuild()

    def clear_log(self) -> None:
        self._rows.clear()
        self.table.setRowCount(0)

    def _add_table_row(self, row: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for i, key in enumerate(self.KEYS):
            item = QTableWidgetItem(row[key])
            if key == "channel":
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, i, item)

    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for row in self._rows:
            if self._type_filter and row["type"] not in self._type_filter:
                continue
            self._add_table_row(row)


def _now_str() -> str:
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _format_values(parsed: ParsedMessage) -> str:
    excluded = {"velocity"} if parsed.type in ("note_on", "note_off") else set()
    parts = [f"{k}={v}" for k, v in parsed.values.items() if k not in excluded]
    return " ".join(parts)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS（新增 4 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add ui/log_view.py tests/test_ui_components.py
git commit -m "feat: 消息日志视图（别名/过滤/清空）"
```

---

## 任务 11：测试消息发送面板 `ui/send_panel.py`

**文件：**
- 创建：`ui/send_panel.py`
- 测试：追加到 `tests/test_ui_components.py`

**设计：** 通道 QSpinBox(0-15)、类型 QComboBox（note_on/note_off/cc/pitch_bend/program_change）、参数 SpinBox（note/velocity 或 control/value）、发送按钮。通过注入的 `send_cb` 回调发出（避免 UI 直接依赖 engine）。

- [ ] **步骤 1：编写失败的测试（追加）**

```python
from ui.send_panel import SendPanel


def test_send_panel_callback(qtbot):
    calls = []
    p = SendPanel(send_cb=lambda type_, ch, kw: calls.append((type_, ch, kw)))
    qtbot.addWidget(p)
    p.type_combo.setCurrentText("note_on")
    p.channel_spin.setValue(9)
    p.param1_spin.setValue(36)
    p.param2_spin.setValue(100)
    p._do_send()
    assert calls == [("note_on", 9, {"note": 36, "velocity": 100})]


def test_send_panel_cc(qtbot):
    calls = []
    p = SendPanel(send_cb=lambda type_, ch, kw: calls.append((type_, ch, kw)))
    qtbot.addWidget(p)
    p.type_combo.setCurrentText("cc")
    p.channel_spin.setValue(1)
    p.param1_spin.setValue(7)
    p.param2_spin.setValue(64)
    p._do_send()
    assert calls == [("cc", 1, {"control": 7, "value": 64})]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.send_panel）。

- [ ] **步骤 3：编写最少实现**

`ui/send_panel.py`：

```python
from typing import Callable, Dict

from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox

from ui import style_qss as QSS

_TYPE_DEFS = {
    "note_on": {"p1": "音符", "p2": "力度", "p1_max": 127, "p2_max": 127, "kw": ("note", "velocity")},
    "note_off": {"p1": "音符", "p2": "力度", "p1_max": 127, "p2_max": 127, "kw": ("note", "velocity")},
    "cc": {"p1": "CC号", "p2": "值", "p1_max": 127, "p2_max": 127, "kw": ("control", "value")},
    "pitch_bend": {"p1": "弯音", "p2": None, "p1_max": 16383, "p2_max": 1, "kw": ("pitch",)},
    "program_change": {"p1": "音色", "p2": None, "p1_max": 127, "p2_max": 1, "kw": ("program",)},
}


class SendPanel(QFrame):
    def __init__(self, send_cb: Callable[[str, int, Dict[str, int]], None], parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.send_cb = send_cb
        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(0, 15)
        self.channel_spin.setValue(0)
        self.type_combo = QComboBox()
        self.type_combo.addItems(list(_TYPE_DEFS.keys()))
        self.param1_spin = QSpinBox()
        self.param2_spin = QSpinBox()
        self.send_btn = QPushButton("发送")
        self.send_btn.setStyleSheet(f"background-color:{QSS.PRIMARY}; color:{QSS.BG}; font-weight:600;")
        self.send_btn.clicked.connect(self._do_send)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(QLabel("发送测试:"))
        layout.addWidget(QLabel("通道"))
        layout.addWidget(self.channel_spin)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.param1_spin)
        layout.addWidget(self.param2_spin)
        layout.addWidget(self.send_btn)
        layout.addStretch(1)
        self.type_combo.currentTextChanged.connect(self._update_params)
        self._update_params()

    def _update_params(self) -> None:
        d = _TYPE_DEFS[self.type_combo.currentText()]
        self.param1_spin.setRange(0, d["p1_max"])
        self.param1_spin.setValue(0)
        if d["p2"] is None:
            self.param2_spin.setEnabled(False)
            self.param2_spin.setValue(0)
        else:
            self.param2_spin.setEnabled(True)
            self.param2_spin.setRange(0, d["p2_max"])
            self.param2_spin.setValue(0)

    def _do_send(self) -> None:
        d = _TYPE_DEFS[self.type_combo.currentText()]
        kw = {d["kw"][0]: self.param1_spin.value()}
        if d["p2"] is not None:
            kw[d["kw"][1]] = self.param2_spin.value()
        self.send_cb(self.type_combo.currentText(), self.channel_spin.value(), kw)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS（新增 2 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add ui/send_panel.py tests/test_ui_components.py
git commit -m "feat: 测试消息发送面板"
```

---

## 任务 12：绑定编辑器 `ui/bindings_view.py`

**文件：**
- 创建：`ui/bindings_view.py`
- 测试：追加到 `tests/test_ui_components.py`

**设计：** 表格展示配置（信号名 / 源类别 / 源描述 / 虚拟发送）。按钮：新增、删除、导出 JSON（QFileDialog）、导入 JSON。新增行内联可编辑：信号名 QLineEdit、源类型 QComboBox、通道/事件/音符/CC/按键字段。

- [ ] **步骤 1：编写失败的测试（追加）**

```python
from core.bindings import Binding, BindingConfig, BindingSource
from ui.bindings_view import BindingsView


def test_bindings_view_renders_config(qtbot):
    cfg = BindingConfig(bindings=[Binding(signal="drum_hit", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})])])
    v = BindingsView(config=cfg)
    qtbot.addWidget(v)
    assert v.table.rowCount() == 1
    assert v.table.item(0, 0).text() == "drum_hit"


def test_bindings_view_save_config(qtbot, tmp_path):
    v = BindingsView(config=BindingConfig())
    qtbot.addWidget(v)
    out = tmp_path / "b.json"
    v.save_to(str(out))
    loaded = BindingConfig.from_file(str(out))
    assert loaded == v.current_config()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.bindings_view）。

- [ ] **步骤 3：编写最少实现**

`ui/bindings_view.py`：

```python
import json
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                             QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                             QTableWidget, QTableWidgetItem, QVBoxLayout)

from core.bindings import Binding, BindingConfig, BindingSource
from ui import style_qss as QSS

_MIDI_EVENTS = ["", "note_on", "note_off", "cc", "pitch_bend", "program_change", "aftertouch", "poly_aftertouch"]
_SOURCE_TYPES = ["midi", "keyboard"]


def _source_desc(s: BindingSource) -> str:
    if s.type == "keyboard":
        return f"按键 {s.key}"
    parts = []
    if s.channel is not None:
        parts.append(f"ch{s.channel + 1}")
    if s.event:
        parts.append(s.event)
    if s.note is not None:
        parts.append(f"note={s.note}")
    if s.cc is not None:
        parts.append(f"cc={s.cc}")
    return " ".join(parts) or "任意 MIDI"


class BindingsView(QFrame):
    """绑定配置编辑器：表格 + 新增/删除 + JSON 导出/导入。"""

    HEADERS = ("信号名", "源类型", "源描述", "虚拟输出")

    def __init__(self, config: BindingConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._config = config

        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        title = QLabel("信号绑定")
        title.setObjectName("sectionTitle")
        self.add_btn = QPushButton("新增")
        self.del_btn = QPushButton("删除")
        self.export_btn = QPushButton("导出 JSON")
        self.import_btn = QPushButton("导入 JSON")
        for w in (title,):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.add_btn, self.del_btn, self.export_btn, self.import_btn):
            bar.addWidget(w)
        root.addLayout(bar)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table)

        self.add_btn.clicked.connect(self._add_row)
        self.del_btn.clicked.connect(self._remove_row)
        self.export_btn.clicked.connect(self._export)
        self.import_btn.clicked.connect(self._import)
        self._rebuild()

    # ---- 展示 ----
    def _rebuild(self) -> None:
        self.table.setRowCount(0)
        for b in self._config.bindings:
            for s in b.sources:
                r = self.table.rowCount()
                self.table.insertRow(r)
                self.table.setItem(r, 0, QTableWidgetItem(b.signal))
                self.table.setItem(r, 1, QTableWidgetItem(s.type))
                self.table.setItem(r, 2, QTableWidgetItem(_source_desc(s)))
                self.table.setItem(r, 3, QTableWidgetItem(json.dumps(b.virtual_midi, ensure_ascii=False) if b.virtual_midi else "-"))

    # ---- 编辑 ----
    def _add_row(self) -> None:
        # 简化版：追加一个带默认值的绑定（signal=新信号，空源），后续可通过导出/导入 JSON 精调源参数
        self._config.bindings.append(Binding(signal="新信号", sources=[BindingSource(type="midi")]))
        self._rebuild()

    def _remove_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        signal = self.table.item(row, 0).text()
        self._config.bindings = [b for b in self._config.bindings if b.signal != signal]
        self._rebuild()

    def current_config(self) -> BindingConfig:
        """从表格读取为配置对象（为简化，保留内存模型；导出手文件直接序列化配置）。"""
        return self._config

    # ---- 导入导出 ----
    def save_to(self, path: str) -> None:
        self._config.to_file(path)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出绑定配置", "bindings.json", "JSON 文件 (*.json)")
        if path:
            self.save_to(path)

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入绑定配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            self._config = BindingConfig.from_file(path)
        except ValueError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self._rebuild()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS（新增 2 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add ui/bindings_view.py tests/test_ui_components.py
git commit -m "feat: 绑定配置编辑器（导入导出 JSON）"
```

---

## 任务 13：主窗口组装与数据流接线 `app.py` + `ui/main_window.py`

**文件：**
- 创建：`ui/main_window.py`
- 创建：`app.py`
- 创建：`config/bindings.json`（默认空配置，由程序生成）
- 修改：`tests/test_ui_components.py`（追加冒烟测试）

**设计：** `MainWindow` 组装全部面板，QTimer(30ms) 轮询 `engine.drain()` 分发给 channel_matrix + log_view；matcher 命中时把信号名作为别名写入日志并执行 `virtual_midi` 发送到虚拟端口；PortPanel 切换设备时打开/关闭端口；SendPanel 回调走 engine；键盘事件过滤器（QEvent.KeyPress）→ matcher → 同样经绑定转发。

- [ ] **步骤 1：编写失败的测试（追加）**

```python
from app import create_binding_config_path
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow


def test_main_window_constructs(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BINDINGS_PATH", str(tmp_path / "bindings.json"))
    win = MainWindow(bindings_path=str(tmp_path / "bindings.json"))
    qtbot.addWidget(win)
    assert win.windowTitle() != ""
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.main_window / app）。

- [ ] **步骤 3：编写最少实现**

`ui/main_window.py`：

```python
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QMainWindow, QSplitter, QStatusBar, QVBoxLayout, QWidget

from core.bindings import BindingConfig
from core.matcher import Matcher

from midi.engine import MidiEngine
from midi.keyboard_input import KeyboardWatcher, normalize_key
from midi.parser import MIDO_TYPE_MAP, ParsedMessage
from ui.bindings_view import BindingsView
from ui.channel_matrix import ChannelMatrix
from ui.log_view import LogView
from ui.port_panel import PortPanel
from ui.send_panel import SendPanel
from ui import style_qss as QSS


class MainWindow(QMainWindow):
    def __init__(self, bindings_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MIDI 调试工具")
        self.resize(1080, 720)
        self._bindings_path = bindings_path
        self._config = BindingConfig.from_file(bindings_path)
        self._matcher = Matcher(self._config)
        self.engine = MidiEngine()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        self.port_panel = PortPanel(self.engine)
        split = QSplitter(Qt.Orientation.Vertical)
        top_split = QSplitter(Qt.Orientation.Horizontal)
        self.matrix = ChannelMatrix()
        self.log_view = LogView()
        top_split.addWidget(self.matrix)
        top_split.addWidget(self.log_view)
        top_split.setSizes([220, 800])
        split.addWidget(top_split)
        self.send_panel = SendPanel(self._on_send)
        split.addWidget(self.send_panel)
        self.bindings_view = BindingsView(self._config)
        split.addWidget(self.bindings_view)
        split.setSizes([420, 60, 220])
        root.addWidget(self.port_panel)
        root.addWidget(split)

        self.setStatusBar(QStatusBar())

        # 输入轮询
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll_inputs)
        self._timer.start()

        # 设备切换
        self.port_panel.input_combo.currentTextChanged.connect(self._on_input_changed)
        self.port_panel.output_combo.currentTextChanged.connect(self._on_output_changed)

        # 键盘（焦点内捕获）
        self._watcher = KeyboardWatcher(on_key=self._on_key_down, use_global_hook=False)

    # ---- 设备 ----
    def _on_input_changed(self, name: str) -> None:
        self.engine.close_inputs()
        if name and name != "（无）":
            self.engine.open_input(name)
            self.statusBar().showMessage(f"已打开输入端口: {name}", 3000)

    def _on_output_changed(self, name: str) -> None:
        if name and name != "（无）":
            self.engine.open_output(name)
            self.statusBar().showMessage(f"已打开输出端口: {name}", 3000)

    # ---- 收发 ----
    def _on_send(self, type_: str, channel: int, kw: dict) -> None:
        try:
            self.engine.send_message(type_, channel=channel, **kw)
            self.log_view.append_message(_make_parsed(type_, channel, kw), source="out")
        except RuntimeError as exc:
            self.statusBar().showMessage(str(exc), 3000)

    def _poll_inputs(self) -> None:
        msgs = self.engine.drain()
        for parsed in msgs:
            self._handle_input(parsed)

    def _handle_input(self, parsed: ParsedMessage) -> None:
        if parsed.channel is not None:
            self.matrix.pulse(parsed.channel)
        signals = self._matcher.signals_for_parsed(parsed)
        alias = " / ".join(sorted(signals)) if signals else None
        self.log_view.append_message(parsed, source="in", alias=alias)
        if signals and self._config.bindings:
            self._fire_virtual_midi(signals)

    def _fire_virtual_midi(self, signals: set) -> None:
        for b in self._config.bindings:
            if b.signal in signals and b.virtual_midi:
                vm = b.virtual_midi
                try:
                    self.engine.send_message(vm.get("type", "note_on"), channel=vm.get("channel", 0),
                                             **{k: v for k, v in vm.items() if k not in ("type", "channel")})
                except RuntimeError as exc:
                    self.statusBar().showMessage(f"虚拟输出失败: {exc}", 3000)

    # ---- 键盘 ----
    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._on_key_down(normalize_key(event.keyCombination().toString()))
        super().keyPressEvent(event)

    def _on_key_down(self, key: str) -> None:
        signals = self._matcher.signals_for_key(key)
        if not signals:
            return
        self.statusBar().showMessage(f"按键 {key} → 信号: {', '.join(sorted(signals))}", 2000)
        self._fire_virtual_midi(signals)

    def closeEvent(self, event) -> None:
        self._watcher.stop_global()
        self.engine.close_all()
        self._config.to_file(self._bindings_path)
        super().closeEvent(event)


def _make_parsed(type_: str, channel: int, kw: dict) -> ParsedMessage:
    """构造回显解析消息（发送面板的本地回显）。"""
    raw_hex = " ".join(f"{b:02X}" for b in _mido_bytes(type_, channel, kw))
    return ParsedMessage(type=type_, channel=channel, values=kw, raw_hex=raw_hex, description=f"发送 {type_}")


def _mido_bytes(type_: str, channel: int, kw: dict):
    import mido

    return mido.Message(MIDO_TYPE_MAP.get(type_, type_), channel=channel, **kw).bytes()
```

`app.py`：

```python
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from ui import style_qss as QSS
from ui.main_window import MainWindow

BINDINGS_PATH = Path(__file__).parent / "config" / "bindings.json"


def create_binding_config_path(path: Path) -> None:
    """确保默认绑定配置文件存在（空配置）。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"version": 1, "bindings": []}', encoding="utf-8")


def main() -> int:
    create_binding_config_path(BINDINGS_PATH)
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS.GLOBAL_QSS)
    win = MainWindow(bindings_path=str(BINDINGS_PATH))
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS（新增 1 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add app.py ui/main_window.py tests/test_ui_components.py
git commit -m "feat: 主窗口组装与数据流接线（输入轮询/键盘/虚拟输出）"
```

---

## 任务 14：运行应用并手动验收

**文件：** 无新增（应用所有组件已就位）

- [ ] **步骤 1：启动应用**

运行：`.\.venv\Scripts\python -m pytest -v`（全部测试通过）后运行 `.\.venv\Scripts\python app.py`
预期：窗口出现，霓虹主题生效。

- [ ] **步骤 2：验收清单（手动）**

1. 接入真实 MIDI 设备，输入端选择设备 → 弹奏时通道矩阵点亮、日志显示结构化消息与原始 hex
2. 无物理设备时：安装 loopMIDI 并创建端口 → 输出端选 loopMIDI 端口、输入端也选它 → 发送面板发 note_on → 日志出现回环消息（验证双向）
3. 绑定面板新增绑定（如 `ccc` 信号绑定 cc ch1）→ 弹奏 CC 旋钮 → 日志别名列显示信号名
4. 绑定含 `virtual_midi` 的键盘源 → 按下按键（窗口聚焦）→ 状态栏提示信号 → 若虚拟端口已接入（游戏），游戏收到消息
5. 导出 bindings.json → 用编辑器打开检查 JSON 结构符合规格第 5 节
6. 错误路径：拔掉设备后刷新端口、损坏 JSON 导入 → 出现 `.bak` 且界面提示

- [ ] **步骤 3：Commit**

```bash
git add -A
git commit -m "chore: 手动验收完成"
```

---

## 任务 15：事件总线 `core/events.py`（TDD）

**文件：**
- 创建：`core/events.py`
- 测试：`tests/test_events.py`

**设计：** `EventBus` 支持任意字符串主题的订阅/退订/发布；`subscribe` 返回订阅号用于 `unsubscribe`；`publish` 在发布时快照订阅者列表（允许回调中退订）；内部用锁保证线程安全。

- [ ] **步骤 1：编写失败的测试**

`tests/test_events.py`：

```python
from core.events import EventBus


def test_subscribe_publish():
    bus = EventBus()
    got = []
    bus.subscribe("midi.message", got.append)
    bus.publish("midi.message", 42)
    assert got == [42]


def test_multiple_subscribers():
    bus = EventBus()
    a, b = [], []
    bus.subscribe("t", a.append)
    bus.subscribe("t", b.append)
    bus.publish("t", 1)
    assert a == [1] and b == [1]


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    got = []
    sid = bus.subscribe("t", got.append)
    bus.publish("t", 1)
    bus.unsubscribe(sid)
    bus.publish("t", 2)
    assert got == [1]


def test_different_topic_isolated():
    bus = EventBus()
    got = []
    bus.subscribe("a", got.append)
    bus.publish("b", 1)
    assert got == []


def test_publish_without_subscribers_safe():
    EventBus().publish("nobody", 1)


def test_clear():
    bus = EventBus()
    got = []
    bus.subscribe("t", got.append)
    bus.clear()
    bus.publish("t", 1)
    assert got == []
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_events.py -v`
预期：FAIL（ModuleNotFoundError: core.events）。

- [ ] **步骤 3：编写最少实现**

`core/events.py`：

```python
from threading import Lock
from typing import Any, Callable, Dict, List


class EventBus:
    """线程安全的事件总线：任意字符串主题、订阅/退订/发布。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: Dict[str, List[tuple]] = {}
        self._next_id = 0

    def subscribe(self, topic: str, cb: Callable[[Any], None]) -> int:
        with self._lock:
            self._next_id += 1
            sid = self._next_id
            self._subs.setdefault(topic, []).append((sid, cb))
            return sid

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            for topic, lst in list(self._subs.items()):
                lst[:] = [(i, cb) for i, cb in lst if i != sid]
                if not lst:
                    del self._subs[topic]

    def publish(self, topic: str, payload: Any = None) -> None:
        with self._lock:
            subs = list(self._subs.get(topic, []))
        for _, cb in subs:
            cb(payload)

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()

    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._subs.keys())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_events.py -v`
预期：PASS（6 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add core/events.py tests/test_events.py
git commit -m "feat: 线程安全事件总线"
```

---

## 任务 16：插件接口与加载器 `core/plugin.py` + `core/plugin_loader.py`（TDD）

**文件：**
- 创建：`core/plugin.py`
- 创建：`core/plugin_loader.py`
- 测试：`tests/test_plugin_loader.py`

**设计：** `Plugin` 抽象基类：`name` 元数据 + `on_activate(app)` / `on_deactivate()` / `create_panel()`（默认返回 None）。`PluginHost` 扫描 `plugins/` 下每个子目录的 `plugin.py`，用 importlib 导入并实例化，逐个激活；任何异常都被捕获，记录到 `items` 的 `error` 字段并标记 `disabled`，不影响其他插件。

- [ ] **步骤 1：编写失败的测试**

`tests/test_plugin_loader.py`：

```python
import textwrap

import pytest

from core.plugin_loader import PluginHost


def write_plugin(tmp_path, name, source):
    d = tmp_path / name
    d.mkdir()
    (d / "plugin.py").write_text(source, encoding="utf-8")
    return d


GOOD_SOURCE = textwrap.dedent('''
    from core.plugin import Plugin

    class GoodPlugin(Plugin):
        name = "good"
        def on_activate(self, app):
            app.bus.publish("plugin.activated", self.name)
''')

BAD_SOURCE = textwrap.dedent('''
    from core.plugin import Plugin

    class BadPlugin(Plugin):
        name = "bad"
        def on_activate(self, app):
            raise RuntimeError("boom")
''')


def test_discover_finds_plugins(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    assert {i["name"] for i in host.items} == {"good"}


def test_failure_isolated(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    write_plugin(tmp_path, "b", BAD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    by_name = {i["name"]: i for i in host.items}
    assert by_name["bad"]["disabled"] is True
    assert "boom" in by_name["bad"]["error"]
    assert by_name["good"]["disabled"] is False


def test_module_missing_plugin_class(tmp_path):
    d = write_plugin(tmp_path, "a", "import math\n")
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    assert host.items == []


def test_deactivate_all(tmp_path):
    write_plugin(tmp_path, "a", GOOD_SOURCE)
    host = PluginHost(str(tmp_path))
    host.load_and_activate(app=None)
    host.deactivate_all()
    assert host.items == []
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_plugin_loader.py -v`
预期：FAIL（ModuleNotFoundError: core.plugin_loader）。

- [ ] **步骤 3：编写最少实现**

`core/plugin.py`：

```python
from abc import ABC


class Plugin(ABC):
    """插件基类。name 必填；子类实现生命周期钩子。"""

    name: str = ""

    def on_activate(self, app) -> None:  # app: api.AppContext
        """激活时调用，可在此订阅事件、访问引擎。"""

    def on_deactivate(self) -> None:
        """停用时调用，用于清理。"""

    def create_panel(self):
        """可选：返回一个 QWidget 挂进主窗口“插件”Tab；无面板插件返回 None。"""
        return None
```

`core/plugin_loader.py`：

```python
import importlib.util
from pathlib import Path
from typing import List, Optional

from core.plugin import Plugin


class PluginHost:
    """扫描 plugins/ 目录、加载并激活插件；异常隔离，不影响宿主。"""

    def __init__(self, plugins_dir: str):
        self._dir = Path(plugins_dir)
        self.items: List[dict] = []  # {"name","instance","error","disabled"}

    def _discover(self) -> List[Path]:
        if not self._dir.exists():
            return []
        return sorted(p / "plugin.py" for p in self._dir.iterdir()
                      if p.is_dir() and (p / "plugin.py").exists())

    def load_and_activate(self, app) -> None:
        for plugin_py in self._discover():
            item = {"name": plugin_py.parent.name, "instance": None,
                    "error": None, "disabled": False}
            try:
                mod = self._import_plugin(plugin_py)
                inst = next((c for c in mod.__dict__.values()
                             if isinstance(c, type) and issubclass(c, Plugin) and c is not Plugin), None)
                if inst is None:
                    self.items.append(item)  # 未定义插件类：跳过
                    continue
                item["name"] = inst.name or item["name"]
                plugin = inst()
                plugin.on_activate(app)
                item["instance"] = plugin
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                item["disabled"] = True
            self.items.append(item)

    def deactivate_all(self) -> None:
        for item in self.items:
            inst = item["instance"]
            if inst is not None and not item.get("disabled"):
                try:
                    inst.on_deactivate()
                except Exception:
                    pass
        self.items.clear()

    @staticmethod
    def _import_plugin(path: Path):
        module_name = f"_plugin_{path.parent.name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_plugin_loader.py -v`
预期：PASS（4 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add core/plugin.py core/plugin_loader.py tests/test_plugin_loader.py
git commit -m "feat: 插件接口与加载器（失败隔离）"
```

---

## 任务 17：SDK 应用上下文 `api.py`（TDD）

**文件：**
- 创建：`api.py`
- 测试：`tests/test_api.py`
- 依赖：任务 5 `MidiEngine`、任务 3/4 `BindingConfig`/`Matcher`、任务 15 `EventBus`

**设计：** `AppContext` 聚合引擎/总线/配置/匹配器，统一分发输入消息到总线（`midi.message` → matcher → `midi.signal`），并提供 `send_midi`（发送 + 发布 `midi.sent`）。`api.create_app()` 构造上下文并记住默认实例，`api.on(topic)` 装饰器订阅默认实例。`app.start()/stop()` 提供无 GUI 的调度线程。

- [ ] **步骤 1：编写失败的测试**

`tests/test_api.py`：

```python
import mido
import pytest

import api
from midi.parser import ParsedMessage


def test_create_app_context():
    app = api.create_app()
    assert app.engine is not None and app.bus is not None and app.matcher is not None


def test_on_decorator_subscribes_default(tmp_path):
    got = []
    api.create_app(bindings_path=str(tmp_path / "b.json"))
    captured = {}

    @api.on("midi.message")
    def cb(msg):
        got.append(msg)

    captured["parsed"] = ParsedMessage(type="note_on", channel=9, values={"note": 36})
    api._DEFAULT_APP.bus.publish("midi.message", captured["parsed"])
    assert got and got[0].type == "note_on"


def test_dispatch_publishes_signal():
    app = api.create_app()
    from core.bindings import Binding, BindingSource
    app.config.bindings = [Binding(signal="boom", sources=[
        BindingSource.from_dict({"type": "midi", "event": "note_on", "note": 36})])]
    app.matcher = api.Matcher(app.config)
    got = []
    app.bus.subscribe("midi.signal", got.append)
    app._on_parsed(ParsedMessage(type="note_on", channel=0, values={"note": 36, "velocity": 1}))
    assert got and "boom" in got[0]["signals"]


def test_send_midi_publishes_sent(monkeypatch):
    app = api.create_app()
    got = []
    app.bus.subscribe("midi.sent", got.append)
    monkeypatch.setattr(app.engine, "send_message", lambda *args, **kwargs: None)
    app.send_midi("note_on", channel=0, note=60)
    assert got and got[0]["type"] == "note_on"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_api.py -v`
预期：FAIL（ModuleNotFoundError: api）。

- [ ] **步骤 3：编写最少实现**

`api.py`：

```python
"""SDK 统一入口：AppContext 聚合引擎/事件总线/绑定配置/匹配器。"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set

from core.bindings import BindingConfig
from core.events import EventBus
from core.matcher import Matcher
from midi.engine import MidiEngine
from midi.parser import ParsedMessage

_DEFAULT_APP: Optional["AppContext"] = None

TOPIC_MESSAGE = "midi.message"
TOPIC_SIGNAL = "midi.signal"
TOPIC_SENT = "midi.sent"
TOPIC_KEY = "key.pressed"
TOPIC_BINDING = "binding.changed"


@dataclass
class AppContext:
    engine: MidiEngine
    bus: EventBus
    config: BindingConfig
    matcher: Matcher
    bindings_path: Optional[str] = field(default=None, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, repr=False)
    _running: bool = field(default=False, repr=False)

    # ---- 事件订阅便捷方法 ----
    def on(self, topic: str, cb: Callable) -> int:
        return self.bus.subscribe(topic, cb)

    def save_bindings(self) -> None:
        if self.bindings_path:
            self.config.to_file(self.bindings_path)

    # ---- 输入分发（GUI 主循环或 headless 线程调用）----
    def _on_parsed(self, parsed: ParsedMessage) -> None:
        self.bus.publish(TOPIC_MESSAGE, parsed)
        signals = self.matcher.signals_for_parsed(parsed)
        if signals:
            self.bus.publish(TOPIC_SIGNAL, {"signals": signals, "parsed": parsed})

    def on_key(self, key: str) -> None:
        self.bus.publish(TOPIC_KEY, key)
        signals = self.matcher.signals_for_key(key)
        if signals:
            self.bus.publish(TOPIC_SIGNAL, {"signals": signals, "key": key})

    def open_input(self, name: str) -> None:
        self.engine.open_input(name, callback=self._on_parsed)

    def close_inputs(self) -> None:
        self.engine.close_inputs()

    # ---- 发送 ----
    def send_midi(self, type_: str, channel: int = 0, **kwargs) -> None:
        self.engine.send_message(type_, channel=channel, **kwargs)
        self.bus.publish(TOPIC_SENT, {"type": type_, "channel": channel, "values": kwargs})

    # ---- 无 GUI 调度线程 ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            for parsed in self.engine.drain():
                self._on_parsed(parsed)
            time.sleep(0.02)


def create_app(bindings_path: Optional[str] = None) -> AppContext:
    global _DEFAULT_APP
    config = BindingConfig.from_file(bindings_path) if bindings_path else BindingConfig()
    app = AppContext(engine=MidiEngine(), bus=EventBus(), config=config,
                     matcher=Matcher(config), bindings_path=bindings_path)
    _DEFAULT_APP = app
    return app


def on(topic: str):
    """装饰器：订阅默认实例（需先调用 create_app()）。"""

    def deco(cb: Callable) -> Callable:
        if _DEFAULT_APP is None:
            raise RuntimeError("请先调用 api.create_app()")
        _DEFAULT_APP.bus.subscribe(topic, cb)
        return cb

    return deco


# 重导出常用符号，供第三方直接使用
from core.bindings import Binding, BindingSource  # noqa: E402  (pylint 忽略导入顺序)
from core.events import EventBus  # noqa: E402
from midi.parser import ParsedMessage  # noqa: E402
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.\.venv\Scripts\python -m pytest tests/test_api.py -v`
预期：PASS（4 个用例全过）。

- [ ] **步骤 5：Commit**

```bash
git add api.py tests/test_api.py
git commit -m "feat: SDK 应用上下文（事件分发/发送/无 GUI 调度）"
```

---

## 任务 18：插件宿主 UI 与主窗口接线 `ui/plugin_tabs.py`（TDD）

**文件：**
- 创建：`ui/plugin_tabs.py`
- 修改：`ui/main_window.py`（主窗口改经 Api/AppContext 分发并挂插件 Tab）
- 修改：`app.py`（加载插件目录、关闭时卸载插件）
- 测试：追加到 `tests/test_ui_components.py`

**设计：** 主窗口改为"主界面/插件"两类 Tab。`PluginTabs(QTabWidget)`：`mount(name, widget)` 挂插件面板；`show_error(name, error)` 显示加载失败信息。主窗口分发改为订阅 `AppContext.bus` 的 `midi.message` / `midi.signal` / `key.pressed`（矩阵、日志、虚拟输出都靠总线事件驱动），发送面板改走 `app.send_midi`。

- [ ] **步骤 1：编写失败的测试（追加）**

```python
from PyQt6.QtWidgets import QLabel
from ui.plugin_tabs import PluginTabs


def test_plugin_tabs_mount(qtbot):
    t = PluginTabs()
    qtbot.addWidget(t)
    t.mount("插件A", QLabel("hello"))
    assert t.count() == 1
    assert t.tabText(0) == "插件A"


def test_plugin_tabs_error(qtbot):
    t = PluginTabs()
    qtbot.addWidget(t)
    t.show_error("bad", "boom")
    assert "bad" in t.tabText(0)
    assert "boom" in t.widget(0).text()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：FAIL（ModuleNotFoundError: ui.plugin_tabs）。

- [ ] **步骤 3：编写最少实现**

`ui/plugin_tabs.py`：

```python
from PyQt6.QtWidgets import QLabel, QTabWidget

from ui import style_qss as QSS


class PluginTabs(QTabWidget):
    """承载“主界面”与各插件面板的 Tab 容器。"""

    def mount(self, name: str, widget) -> None:
        self.addTab(widget, name)

    def show_error(self, name: str, error: str) -> None:
        box = QLabel(f"插件 {name} 加载失败：{error}")
        box.setWordWrap(True)
        box.setStyleSheet(f"color: {QSS.ERROR}; padding: 8px;")
        self.addTab(box, name)
```

改写 `ui/main_window.py`（替换任务 13 的实现，统一经 AppContext 走总线）：

`ui/main_window.py`（任务 18 完整实现）：

```python
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QMainWindow, QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget

import api
from core.plugin_loader import PluginHost
from midi.keyboard_input import KeyboardWatcher, normalize_key
from midi.parser import MIDO_TYPE_MAP, ParsedMessage
from ui.bindings_view import BindingsView
from ui.channel_matrix import ChannelMatrix
from ui.log_view import LogView
from ui.plugin_tabs import PluginTabs
from ui.port_panel import PortPanel
from ui.send_panel import SendPanel


class MainWindow(QMainWindow):
    def __init__(self, app: "api.AppContext", plugins_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MIDI 调试工具")
        self.resize(1080, 720)
        self.app = app

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        self.port_panel = PortPanel(app.engine)

        self.tabs = QTabWidget()
        main_page = QWidget()
        main_layout = QVBoxLayout(main_page)
        split = QSplitter(Qt.Orientation.Vertical)
        top_split = QSplitter(Qt.Orientation.Horizontal)
        self.matrix = ChannelMatrix()
        self.log_view = LogView()
        top_split.addWidget(self.matrix)
        top_split.addWidget(self.log_view)
        top_split.setSizes([220, 800])
        split.addWidget(top_split)
        self.send_panel = SendPanel(send_cb=self._on_send)
        split.addWidget(self.send_panel)
        self.bindings_view = BindingsView(app.config)
        split.addWidget(self.bindings_view)
        split.setSizes([420, 60, 220])
        main_layout.addWidget(split)
        self.tabs.addTab(main_page, "主界面")

        # 插件加载与挂载（失败隔离）
        self.plugin_tabs = PluginTabs()
        self.plugin_host = PluginHost(plugins_dir)
        self.plugin_host.load_and_activate(app)
        for item in self.plugin_host.items:
            if item.get("disabled"):
                self.plugin_tabs.show_error(item["name"], item.get("error") or "未知错误")
                continue
            try:
                panel = item["instance"].create_panel()
            except Exception as exc:
                self.plugin_tabs.show_error(item["name"], f"面板构造失败: {exc}")
                continue
            if panel is not None:
                self.plugin_tabs.mount(item["name"], panel)
        if self.plugin_tabs.count():
            self.tabs.addTab(self.plugin_tabs, "插件")

        root.addWidget(self.port_panel)
        root.addWidget(self.tabs, 1)
        self.setStatusBar(QStatusBar())

        # 输入轮询：统一经 app._on_parsed 分发到总线
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll_inputs)
        self._timer.start()

        # 设备切换
        self.port_panel.input_combo.currentTextChanged.connect(self._on_input_changed)
        self.port_panel.output_combo.currentTextChanged.connect(self._on_output_changed)

        # 总线订阅（UI 自身也是订阅者）
        app.on(api.TOPIC_MESSAGE, self._on_message)
        app.on(api.TOPIC_SIGNAL, self._on_signal)
        app.on(api.TOPIC_SENT, self._on_sent)

        # 键盘（焦点内捕获，交由 app 分发）
        self._watcher = KeyboardWatcher(on_key=self._on_key_down, use_global_hook=False)

    # ---- 设备 ----
    def _on_input_changed(self, name: str) -> None:
        self.app.close_inputs()
        if name and name != "（无）":
            self.app.open_input(name)
            self.statusBar().showMessage(f"已打开输入端口: {name}", 3000)

    def _on_output_changed(self, name: str) -> None:
        if name and name != "（无）":
            self.app.engine.open_output(name)
            self.statusBar().showMessage(f"已打开输出端口: {name}", 3000)

    # ---- 总线事件 ----
    def _poll_inputs(self) -> None:
        for parsed in self.app.engine.drain():
            self.app._on_parsed(parsed)

    def _on_message(self, parsed: ParsedMessage) -> None:
        if parsed.channel is not None:
            self.matrix.pulse(parsed.channel)
        signals = self.app.matcher.signals_for_parsed(parsed)
        alias = " / ".join(sorted(signals)) if signals else None
        self.log_view.append_message(parsed, source="in", alias=alias)

    def _on_signal(self, payload: dict) -> None:
        signals = payload.get("signals", set())
        for b in self.app.config.bindings:
            if b.signal in signals and b.virtual_midi:
                vm = b.virtual_midi
                try:
                    self.app.engine.send_message(vm.get("type", "note_on"),
                                                 channel=vm.get("channel", 0),
                                                 **{k: v for k, v in vm.items() if k not in ("type", "channel")})
                except RuntimeError as exc:
                    self.statusBar().showMessage(f"虚拟输出失败: {exc}", 3000)

    def _on_sent(self, payload: dict) -> None:
        self.log_view.append_message(_sent_message(payload), source="out")

    def _on_send(self, type_: str, channel: int, kw: dict) -> None:
        try:
            self.app.send_midi(type_, channel=channel, **kw)
        except RuntimeError as exc:
            self.statusBar().showMessage(str(exc), 3000)

    # ---- 键盘 ----
    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._on_key_down(normalize_key(event.keyCombination().toString()))
        super().keyPressEvent(event)

    def _on_key_down(self, key: str) -> None:
        self.app.on_key(key)

    def closeEvent(self, event) -> None:
        self._watcher.stop_global()
        self.plugin_host.deactivate_all()
        self.app.engine.close_all()
        self.app.save_bindings()
        super().closeEvent(event)


def _sent_message(payload: dict) -> ParsedMessage:
    import mido

    type_ = payload["type"]
    channel = payload["channel"]
    values = payload["values"]
    raw_hex = " ".join(f"{b:02X}" for b in
                       mido.Message(MIDO_TYPE_MAP.get(type_, type_), channel=channel, **values).bytes())
    return ParsedMessage(type=type_, channel=channel, values=values, raw_hex=raw_hex,
                         description=f"发送 {type_}")
```

- 构造函数改为 `MainWindow(app: AppContext, plugins_dir: str)`；`app.py` 相应改为先 `create_app(...)` 再 `MainWindow(app=app, plugins_dir="plugins")`，并在 `closeEvent` 保存配置（`config.to_file` 由主窗口触发；`AppContext` 不持有路径时跳过保存）

- [ ] **步骤 4：运行测试验证通过（含原主窗口测试修正）**

更新任务 13 的 `test_main_window_constructs` 为：

```python
def test_main_window_constructs(qtbot, tmp_path):
    import api
    app = api.create_app(bindings_path=str(tmp_path / "b.json"))
    win = MainWindow(app=app, plugins_dir=str(tmp_path / "plugins"))
    qtbot.addWidget(win)
    assert win.windowTitle() != ""
```

运行：`.\.venv\Scripts\python -m pytest tests/test_ui_components.py -v`
预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add ui/plugin_tabs.py ui/main_window.py app.py tests/test_ui_components.py
git commit -m "feat: 插件宿主 UI（Tab 挂载/错误隔离）并统一总线分发"
```

---

## 任务 19：示例插件与 SDK 演示、验收更新

**文件：**
- 创建：`examples/example_plugin/plugin.py`（简单练习面板插件）
- 创建：`examples/sdk_demo.py`（无 GUI 用法演示）
- 修改：`app.py`（默认插件目录 `plugins/`，不存在则静默跳过）

- [ ] **步骤 1：编写示例插件**

`examples/example_plugin/plugin.py`：

```python
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core.plugin import Plugin
from ui import style_qss as QSS


class NoteSentry(Plugin):
    """示例插件：显示最后收到的 MIDI 消息，并可点击发送音符。"""

    name = "note_sentry"

    def on_activate(self, app):
        self.app = app
        app.on("midi.message", self.on_message)

    def on_message(self, parsed):
        label = getattr(self, "label", None)
        if label is None:
            return  # 面板尚未构造
        label.setText(f"最后消息: ch{parsed.channel + 1 if parsed.channel is not None else '-'} "
                      f"{parsed.type} {parsed.raw_hex}")

    def create_panel(self):
        box = QWidget()
        v = QVBoxLayout(box)
        self.label = QLabel("等待 MIDI 消息…")
        self.label.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px;")
        hint = QLabel("点击下方按钮向输出端口发送 C4 音符。")
        hint.setStyleSheet(f"color: {QSS.MUTED};")
        self.btn = QPushButton("发送 C4")
        self.btn.setStyleSheet(f"background-color: {QSS.ACCENT}; color: {QSS.BG}; font-weight: 600;")
        self.btn.clicked.connect(lambda: self.app.send_midi("note_on", channel=0, note=60, velocity=100))
        v.addWidget(self.label)
        v.addWidget(hint)
        v.addWidget(self.btn)
        v.addStretch(1)
        return box
```

`examples/sdk_demo.py`：

```python
"""SDK 无 GUI 用法演示：python examples/sdk_demo.py [midi输入端口名]"""

import sys
import time

import api


def main() -> None:
    app = api.create_app()

    @api.on("midi.message")
    def on_msg(parsed):
        print(f"[{parsed.type}] ch={parsed.channel} values={parsed.values} hex={parsed.raw_hex}")

    @api.on("midi.signal")
    def on_sig(payload):
        print(">> 信号:", payload["signals"])

    port = sys.argv[1] if len(sys.argv) > 1 else None
    if port:
        app.open_input(port)
        app.start()
        print(f"监听 {port}，Ctrl+C 退出")
        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            app.stop()
    else:
        print("用法: python examples/sdk_demo.py <MIDI输入端口名>")
        print("可用输入端口:", app.engine.list_inputs())


if __name__ == "__main__":
    main()
```

- [ ] **步骤 2：验证示例可运行（不接设备时仅打印端口列表）**

运行：`.\.venv\Scripts\python examples/sdk_demo.py`
预期：打印可用输入端口列表，无异常。

- [ ] **步骤 3：Commit**

```bash
git add examples/
git commit -m "feat: 示例插件与 SDK 演示"
```

---

## 任务 20：端到端运行与平台验收

**文件：** 无新增

- [ ] **步骤 1：全量测试**

运行：`.\.venv\Scripts\python -m pytest -v`
预期：全部 PASS（含回环用例需 loopMIDI，未装则 skip）。

- [ ] **步骤 2：平台验收清单（手动）**

1. 启动 `python app.py`，把 `examples/example_plugin` 复制到 `plugins/`（或重启前建好）→ "插件"Tab 出现 example 子 Tab，面板显示"等待 MIDI 消息…"
2. loopMIDI 回环 → 发送面板发 note_on → 插件面板实时显示"最后消息"；日志显示消息；通道矩阵点亮
3. 点击插件面板"发送 C4" → 回环输入端再次收到（验证插件可通过 SDK 发送）
4. 在 `plugins/` 放一个 `on_activate` 里抛异常的插件 → 重启 → 插件 Tab 显示"加载失败"，其他功能不受影响
5. 无 GUI：`python examples/sdk_demo.py "loopMIDI Port"` → 回环发送 → 终端打印 `[note_on]` 与信号行

- [ ] **步骤 3：Commit**

```bash
git add -A
git commit -m "chore: 平台端到端验收完成"
```

---

## 自检

**1. 规格覆盖度：**
- 双向收发 → 任务 5（engine）+任务 11（send panel）+任务 13（接线）✓
- 通道矩阵 → 任务 9 ✓
- 消息日志+别名+过滤 → 任务 10 ✓
- 绑定信号（MIDI/键盘，多源一信号，通配）→ 任务 3/4 ✓
- 导出 JSON 供游戏 → 任务 3/12 ✓
- 键盘模拟 → virtual_midi + 任务 6/13 ✓
- loopMIDI 虚拟端口 → 任务 5/7/13 ✓
- venv → 任务 1 ✓
- 霓虹风格 QSS → 任务 8 ✓
- 错误处理（设备拔出/loopMIDI 缺失/JSON 损坏/发送失败/插件异常隔离）→ 任务 5/7/3/13/16/18 ✓
- 事件总线（订阅/发布/线程安全）→ 任务 15 ✓
- 插件接口与加载器（失败隔离/生命周期）→ 任务 16 ✓
- 插件宿主 UI（Tab 挂载/错误显示/纯逻辑插件无面板）→ 任务 18 ✓
- SDK（create_app/AppContext/on 装饰器/start 无 GUI 调度/事件链）→ 任务 17 ✓
- 示例插件与 SDK 演示 → 任务 19 ✓
- 平台端到端验收 → 任务 20 ✓
- 测试（单元+回环+事件链+插件+手动）→ 各任务 ✓

**2. 占位符扫描：** 无 TODO/待定；每个代码步骤含完整代码。

**3. 类型一致性：**
- `ParsedMessage` 字段 `type/channel/values/raw_hex/description`、`event_key()` 在任务 2 定义，任务 4/10/13/17/18 一致使用 ✓
- `BindingConfig.from_file/to_file`、`Binding/BindingSource.from_dict/to_dict` 在任务 3 定义，任务 12/13/17 一致使用 ✓
- `MidiEngine.open_input(name, callback)/open_output/send_message(type, channel, **kw)/drain/close_all/list_inputs/list_outputs` 任务 5 定义，任务 9/13/17/18 一致使用 ✓
- `normalize_key` 任务 6 定义，任务 13/18 使用 ✓
- `detect_virtual_out_port/setup_guide` 任务 7 定义，任务 9 使用 ✓
- UI 组件构造函数签名：`ChannelMatrix()`、`LogView()`、`SendPanel(send_cb=...)`、`BindingsView(config=...)`、`PortPanel(engine)`、`PluginTabs()` 在各自任务定义，任务 13/18 一致使用 ✓
- `EventBus.subscribe/unsubscribe/publish/clear` 任务 15 定义，任务 16/17/18 一致使用 ✓
- `PluginHost.load_and_activate(app)/deactivate_all/items` 任务 16 定义，任务 18 一致使用 ✓
- `AppContext` 字段与方法（engine/bus/config/matcher/bindings_path、on/_on_parsed/on_key/open_input/close_inputs/send_midi/start/stop/save_bindings）任务 17 定义，任务 18/19/20 一致使用 ✓
- 事件主题常量 `TOPIC_MESSAGE/TOPIC_SIGNAL/TOPIC_SENT` 任务 17 定义，任务 18 一致使用 ✓
- `MIDO_TYPE_MAP` 任务 2 定义，任务 5/13/18 一致使用 ✓