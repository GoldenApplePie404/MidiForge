# API 参考

SDK 统一入口：`from api import ...`

## 一、快速开始

```python
from api import create_app, on, TOPIC_SIGNAL

# 1. 创建应用上下文（自动加载 config/bindings.json）
app = create_app("config/bindings.json")

# 2. 打开 MIDI 端口
app.open_input("KL Essential 61 mk3 MIDI 0")
app.engine.open_output("KL Essential 61 mk3 MIDI 1")

# 3. 订阅事件总线
@on(TOPIC_SIGNAL)
def on_signal_hit(payload):
    print("命中信号:", payload["signals"])

# 4. 启动后台线程（无 GUI 模式）
app.start()

# ... 等待事件 ...

# 5. 停止
app.stop()
```

也可以直接用 GUI：

```bash
启动MIDI调试工具.bat
```

---

## 二、入口函数

### `create_app(bindings_path=None) → AppContext`

创建 SDK 应用上下文。

| 参数 | 类型 | 说明 |
|---|---|---|
| `bindings_path` | `str \| None` | 绑定 JSON 文件路径。传 `None` 则创建空配置，不自动加载 |

**返回**：`AppContext`

**副作用**：设置全局默认实例（配合 `@on` 装饰器使用）。

### `@on(topic)`

模块级装饰器，订阅默认 AppContext 的事件总线。必须先调用 `create_app()`。

```python
@on("midi.message")
def log_msg(parsed):
    print(parsed.description)
```

---

## 三、AppContext

聚合了引擎、事件总线、绑定配置、匹配器，是 SDK 的主入口。

### 属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `engine` | `MidiEngine` | MIDI 输入输出引擎 |
| `bus` | `EventBus` | 事件总线 |
| `config` | `BindingConfig` | 绑定配置（直接改这个对象） |
| `matcher` | `Matcher` | 匹配器 |
| `bindings_path` | `str \| None` | 绑定 JSON 路径（供 `save_bindings()` 回写） |
| `default_virtual_out_port` | `str \| None` | 默认虚拟 MIDI 转发目标端口 |

### 方法

#### `open_input(name) → None`

打开 MIDI 输入端口。消息进入 engine 队列，需配合 `start()` 或 GUI QTimer 轮询分发。

#### `close_inputs() → None`

关闭所有已打开的输入端口。

#### `send_midi(type, channel=0, **kwargs) → None`

发送一条 MIDI 消息到当前输出端口。

| 参数 | 说明 |
|---|---|
| `type` | `"note_on"` / `"note_off"` / `"cc"` / `"pitch_bend"` / `"program_change"` / `"channel_aftertouch"` / `"poly_aftertouch"` |
| `channel` | 0-15 |
| `**kwargs` | 根据 type 不同，传 `note=`、`velocity=`、`control=`、`value=`、`pitch=`、`program=` 等 |

```python
app.send_midi("cc", channel=0, control=7, value=100)
app.send_midi("note_on", note=60, velocity=127)
```

#### `execute_actions(signals, source_parsed=None, source_key=None) → list`

手动触发一批信号对应的绑定动作。返回实际执行的动作描述列表。

**用途**：UI "测试触发"按钮、插件里手动触发。

```python
executed = app.execute_actions({"kick"})
# → [{"action": "key_out", "key": "space"}]
```

#### `on_key(key) → None`

手动注入键盘事件，让匹配器处理。

```python
app.on_key("ctrl+k")
```

#### `subscribe(topic, cb) → int`

订阅事件总线（等价于 `app.bus.subscribe(topic, cb)`）。返回订阅 ID。

#### `save_bindings() → None`

把 `app.config` 回写到 `bindings_path` 指定的 JSON 文件。

#### `start() → None` / `stop() → None`

无 GUI 模式的后台线程。`start()` 后每 20ms 轮询 engine 队列并分发。

```python
app.start()
# 此时即使没有 GUI，MIDI 进来也会触发绑定
# ...
app.stop()
```

---

## 四、事件 Topic 常量

全部在 `api` 模块顶层导出。

| 常量 | 值 | payload 类型 | 说明 |
|---|---|---|---|
| `TOPIC_MESSAGE` | `"midi.message"` | `ParsedMessage` | 收到一条 MIDI 消息 |
| `TOPIC_SIGNAL` | `"midi.signal"` | `dict` | 有信号命中（MIDI 或键盘） |
| `TOPIC_SENT` | `"midi.sent"` | `dict` | 发出了一条 MIDI |
| `TOPIC_KEY` | `"key.pressed"` | `str` | 键盘事件 |
| `TOPIC_ACTION` | `"binding.action"` | `dict` | 绑定动作执行完成 |
| `TOPIC_BINDING` | `"binding.changed"` | `None` | 绑定配置变更 |

### payload 详细结构

**TOPIC_MESSAGE** → `ParsedMessage`

```python
ParsedMessage(
    type="cc",                  # 消息类型
    channel=0,                  # 0-15
    values={"control": 21, "value": 127},  # 类型相关的参数字典
    raw_hex="B0 15 7F",         # 原始字节
    description="CC ch0 CC21 = 127",       # 人类可读
    source="midi_in",           # "midi_in" / "virtual"
)
```

**TOPIC_SIGNAL**

```python
# MIDI 触发
{"signals": {"kick"}, "parsed": ParsedMessage(...)}

# 键盘触发
{"signals": {"vol_max"}, "key": "f8"}
```

**TOPIC_SENT**

```python
{"type": "cc", "channel": 0, "values": {"control": 7, "value": 127}}
```

**TOPIC_ACTION**

```python
{
    "signals": {"kick"},
    "actions": [
        {"action": "key_out", "key": "space"},
        {"action": "virtual_midi", "type": "note_on", "channel": 3, "kwargs": {"note": 36}},
    ],
}
```

---

## 五、MIDI 引擎

`from api import Binding` 或直接 `from midi.engine import MidiEngine`。

### `MidiEngine.input_names() → list[str]`

枚举系统所有 MIDI 输入端口名称。

### `MidiEngine.output_names() → list[str]`

枚举系统所有 MIDI 输出端口名称。

### `MidiEngine.open_input(name) → None`

打开一个输入端口。消息内部入队，需 `drain()` 取出。

### `MidiEngine.close_inputs() → None` / `close_outputs() → None` / `close_all() → None`

关闭端口。

### `MidiEngine.drain() → list[ParsedMessage]`

非阻塞取出所有缓存的消息。用完即清空。

### `MidiEngine.send_message(type, channel=0, **kwargs) → None`

发 MIDI。自动处理 loopMIDI 回声去重（防止同一条消息经虚拟端口回来又被自己匹配）。

### `MidiEngine.detect_loopback_pair() → tuple[str, str] | None`

自动找同 base 名的输入输出对（如 loopMIDI）。返回 `(input_name, output_name)`。

---

## 六、数据模型

### `BindingSource`

```python
BindingSource(
    type="midi" | "keyboard",
    channel=0 | None,       # MIDI 时：0-15，None=通配
    event="cc" | None,      # MIDI 时：note_on / note_off / cc / ... ，None=通配
    note=60 | None,         # note_on/off 时匹配
    cc=21 | None,           # cc 时匹配 control 号
    value_min=64 | None,    # 数值下限（含），None=不限
    key="ctrl+k" | None,    # keyboard 时：按键名（归一化后）
)
```

- frozen dataclass，不可变
- `to_dict()` / `from_dict(d)` 序列化

### `Binding`

```python
Binding(
    signal="kick",                          # 信号名
    sources=[BindingSource(...)],           # 匹配源列表（任一命中即可）
    virtual_midi={"channel": 3, "note": 36}, # 可选：虚拟 MIDI 转发
    key_out={"key": "space"},                # 可选：模拟键盘
)
```

### `BindingConfig`

```python
cfg = BindingConfig(bindings=[Binding(...), ...])

cfg.to_file("config/bindings.json")   # 写入
cfg2 = BindingConfig.from_file(path)  # 读取（损坏自动备份）
cfg.to_dict()                         # 导出为 dict
```

---

## 七、事件总线

`from api import EventBus`

### `EventBus.subscribe(topic, cb) → int`

| 参数 | 说明 |
|---|---|
| `topic` | 字符串，可以带通配符 `"*"` 匹配所有，或前缀 `"midi.*"` |
| `cb` | 回调函数，签名 `(payload) -> None` |

返回订阅 ID（供 unsubscribe 用）。

### `EventBus.unsubscribe(id) → None`

取消订阅。

### `EventBus.publish(topic, payload) → None`

发布事件。线程安全——内部快照订阅者列表，发布过程中新增/取消不影响当前分发。

### `EventBus.clear() → None`

清空所有订阅。

---

## 八、键盘模块

`from midi.keyboard_input import ...`

### `normalize_key(name) → str`

按键名归一化。处理 Qt key name、keyboard 库名、中文别名 → 统一 `ctrl+shift+f1` 格式。

| 输入 | 输出 |
|---|---|
| `"Ctrl+Shift+F1"` | `"ctrl+shift+f1"` |
| `"Ctrl-Shift-F1"` | `"ctrl+shift+f1"` |
| `"空格"` / `"Space"` | `"space"` |

### `send_key(name) → bool`

模拟一次按键（按下+释放）。**Windows 上需要管理员权限**才能全局生效。

### `key_hold(name, down) → bool`

按住（`down=True`）或松开（`down=False`）某个键。

### `KeyboardWatcher`

全局键盘监听。自动降级：优先 `keyboard.hook()`（全局），失败则 `QShortcut`（窗口聚焦内）。

```python
watcher = KeyboardWatcher()
watcher.start(cb=lambda key: print(key))
# ...
watcher.stop()
```

---

## 九、虚拟端口检测

`from midi.virtual_port import ...`

### `detect_virtual_out_port(outputs, inputs=None) → str | None`

在输出端口列表里找虚拟端口。策略：
1. 先找名称含 `loopmidi` / `virtual midi` / `midi-loopback`（大小写不敏感）
2. 否则找和某个输入端口**同名/同 base 名**的输出端口

### `setup_guide() → str`

没检测到虚拟端口时返回安装指南文本，可直接显示给用户。

---

## 十、插件开发

### 继承 `core.plugin.Plugin`

```python
from core.plugin import Plugin
from PyQt6.QtWidgets import QWidget, QLabel

class MyPlugin(Plugin):
    name = "my_plugin"
    version = "0.1"
    description = "示例插件"

    def on_activate(self, app):
        """加载时调用。app 是 AppContext，可以订阅事件总线。"""
        self._sub_id = app.subscribe("midi.message", self._on_msg)

    def on_deactivate(self):
        """卸载时调用。清理资源。"""
        # EventBus 没 unsubscribe 方法？直接让 bus.clear() 处理
        pass

    def create_panel(self):
        """可选：返回一个 QWidget，挂到主窗口 Tab 里。"""
        w = QWidget()
        # ... 搭建 UI ...
        return w
```

### 放置路径

- **自动扫描**：`plugins/` 目录 + `examples/` 目录
- **文件结构**：`plugins/my_plugin/plugin.py`（单个文件，类名随意但必须继承 `Plugin`）

### 插件宿主

```python
from core.plugin_loader import PluginHost

host = PluginHost(["plugins/", "examples/"])
plugins = host.discover()  # 返回 Plugin 实例列表
host.activate_all(app)     # 调每个插件的 on_activate(app)
```

加载失败自动隔离——单个插件 import 错误不会影响其他插件或主程序。

---

## 十一、完整示例

### 示例 1：Headless 监听 + 绑定触发

```python
from api import create_app, on, TOPIC_MESSAGE, TOPIC_ACTION

app = create_app("config/bindings.json")

# 打印所有进来的消息
@on(TOPIC_MESSAGE)
def print_msg(parsed):
    print(f"[{parsed.type}] ch{parsed.channel} {parsed.raw_hex} {parsed.description}")

# 打印所有动作执行
@on(TOPIC_ACTION)
def print_action(payload):
    print(f"✓ 信号 {payload['signals']} → 动作 {payload['actions']}")

app.open_input("KL Essential 61 mk3 MIDI 0")
app.engine.open_output("KL Essential 61 mk3 MIDI 1")

try:
    app.start()
    input("按 Enter 退出\n")
finally:
    app.stop()
    app.close_inputs()
```

### 示例 2：手动构造绑定

```python
from api import create_app, Binding, BindingSource, TOPIC_SIGNAL

app = create_app("config/bindings.json")

# 新建绑定：CC#21 (≥64) → 模拟空格
src = BindingSource(
    type="midi", channel=0, event="cc",
    cc=21, value_min=64,
)
b = Binding(signal="pad_hit", sources=[src], key_out={"key": "space"})

app.config.bindings.append(b)
app.save_bindings()  # 持久化

# 立即生效（matcher 引用的就是同一个 config 对象）
app.matcher.signals_for_parsed(...)  # 能匹配到 pad_hit
```

### 示例 3：自定义插件

```python
# plugins/pad_monitor/plugin.py
from core.plugin import Plugin
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

class PadMonitor(Plugin):
    name = "pad_monitor"
    description = "显示打击垫信号"

    def on_activate(self, app):
        self.app = app
        app.subscribe("midi.message", self._handle)

    def _handle(self, parsed):
        if parsed.type == "cc" and parsed.values.get("control") == 21:
            print(f"打击垫: {parsed.values['value']}")

    def create_panel(self):
        w = QWidget()
        self.label = QLabel("等待打击垫...")
        QVBoxLayout(w).addWidget(self.label)
        return w
```

---

## 十二、错误处理与注意事项

### 线程模型

| 组件 | 线程 |
|---|---|
| `MidiEngine` 回调 | rtmidi 独立线程（只入队，不碰 UI） |
| `AppContext._loop()` | `start()` 启动的 daemon 线程 |
| `GUI QTimer` | 主线程 |
| `EventBus.publish` | **谁调谁执行**——回调在发布者线程里执行 |

**跨线程订阅 UI**：不要直接让 MIDI 线程调 Qt 控件。GUI 里用 `_UiBridge(QObject)` + `pyqtSignal` 把消息排队到主线程。

### 端口名称

Windows rtmidi 会给每个端口加索引号后缀（如 `"KL Essential 61 mk3 MIDI 0"`）。枚举和打开时用**完整字符串**，不是 base 名。

### 虚拟 MIDI 回声

`engine.send_message()` 发出去的消息，经 loopMIDI 回来会：
1. 被 engine 内部指纹检测到
2. 自动标记 `parsed.source = "virtual"`
3. matcher 对 `source == "virtual"` 的消息直接跳过匹配

**不要手动绕过这个机制**——如果你的绑定导致 virtual_midi 转发，转发回来的回声不该再触发同一个绑定。

### 键盘权限

- `keyboard` 库的全局 Hook 需要**管理员权限**
- 非管理员会自动降级为窗口聚焦内监听
- 跨进程 `send_key` 模拟对某些游戏（反作弊）可能无效

### 配置损坏

`BindingConfig.from_file(path)` 如果遇到坏 JSON，会把原文件备份为 `path.bak.<时间戳>`，然后返回空配置。**程序不会崩，但绑定会丢失**——备份文件在原位置可以恢复。

### 关闭顺序

```python
app.stop()           # 停后台线程
app.close_inputs()   # 关输入
app.engine.close_all()  # 关所有端口
app.save_bindings()  # 最后保存
```
