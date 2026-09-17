from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QMainWindow, QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget

import api
from core.bindings import Binding, BindingSource
from core.plugin_loader import PluginHost
from midi.parser import MIDO_TYPE_MAP, ParsedMessage
from ui.bindings_view import BindingsView, BindingEditDialog
from ui.channel_matrix import ChannelMatrix
from ui.log_view import LogView
from ui.plugin_tabs import PluginTabs
from ui.port_panel import PortPanel
from ui.send_panel import SendPanel


class _UiBridge(QObject):
    """把 rtmidi / keyboard 线程的总线事件安全地转发到 Qt 主线程。
    EventBus.publish 可以在任意线程调用，所有 UI 订阅者通过此桥接进主线程处理。
    """
    msg_received = pyqtSignal(object)          # ParsedMessage
    signal_hit = pyqtSignal(dict)
    sent_msg = pyqtSignal(dict)
    action_done = pyqtSignal(dict)

    def __init__(self):
        super().__init__()


class MainWindow(QMainWindow):
    def __init__(self, app: "api.AppContext", plugins_dirs: list, parent=None):
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
        self.bindings_view.set_app(app)  # 注入 app 用于测试触发 + 自动保存
        split.addWidget(self.bindings_view)
        split.setSizes([420, 60, 220])
        main_layout.addWidget(split)
        self.tabs.addTab(main_page, "主界面")

        # 日志右键 → 快速创建绑定
        self.log_view.request_create_binding.connect(self._on_create_binding_from_log)

        # 插件加载与挂载（失败隔离）
        self.plugin_tabs = PluginTabs()
        self.plugin_host = PluginHost(plugins_dirs)
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

        # 总线订阅（通过 _UiBridge 把任意线程的事件转发到主线程）
        self._bridge = _UiBridge()
        app.subscribe(api.TOPIC_MESSAGE, self._bridge.msg_received.emit)
        app.subscribe(api.TOPIC_SIGNAL, self._bridge.signal_hit.emit)
        app.subscribe(api.TOPIC_SENT, self._bridge.sent_msg.emit)
        app.subscribe(api.TOPIC_ACTION, self._bridge.action_done.emit)
        # Qt 信号槽自动跨线程排队
        self._bridge.msg_received.connect(self._on_message)
        self._bridge.signal_hit.connect(self._on_signal)
        self._bridge.sent_msg.connect(self._on_sent)
        self._bridge.action_done.connect(self._on_action)

    # ---- 设备 ----
    def _on_input_changed(self, name: str) -> None:
        try:
            self.app.close_inputs()
            if name and name != "（无）":
                self.app.open_input(name)
                self.statusBar().showMessage(f"已打开输入端口: {name}", 3000)
        except Exception as exc:
            self.statusBar().showMessage(f"打开输入端口失败: {exc}", 5000)

    def _on_output_changed(self, name: str) -> None:
        try:
            if name and name != "（无）":
                self.app.engine.open_output(name)
                self.app.default_virtual_out_port = name  # 绑定转发默认也走这个端口
                self.statusBar().showMessage(f"已打开输出端口: {name}", 3000)
            else:
                self.app.default_virtual_out_port = None
        except Exception as exc:
            self.statusBar().showMessage(f"打开输出端口失败: {exc}", 5000)

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
        if signals:
            self.statusBar().showMessage(f"命中信号: {' / '.join(sorted(signals))}", 2000)

    def _on_action(self, payload: dict) -> None:
        """绑定动作执行反馈。"""
        actions = payload.get("actions", [])
        for a in actions:
            if "error" in a:
                self.statusBar().showMessage(f"[{a.get('binding')}] {a.get('error')}", 4000)

    def _on_sent(self, payload: dict) -> None:
        self.log_view.append_message(_sent_message(payload), source="out")

    def _on_send(self, type_: str, channel: int, kw: dict) -> None:
        try:
            self.app.send_midi(type_, channel=channel, **kw)
        except RuntimeError as exc:
            self.statusBar().showMessage(str(exc), 3000)

    # ---- 键盘 ----
    def keyPressEvent(self, event: QKeyEvent) -> None:
        from ui.bindings_view import _qt_event_to_key_str  # 复用录制对话框的转换函数
        key = _qt_event_to_key_str(event)
        if key:
            self._on_key_down(key)
        super().keyPressEvent(event)

    def _on_key_down(self, key: str) -> None:
        self.app.on_key(key)

    def _on_create_binding_from_log(self, parsed: ParsedMessage) -> None:
        """从日志右键的 MIDI 消息预填充 BindingEditDialog。"""
        # 根据 parsed 类型构造 BindingSource
        src_kwargs = {"type": "midi", "channel": parsed.channel}
        if parsed.type in ("note_on", "note_off"):
            src_kwargs["event"] = parsed.type
            src_kwargs["note"] = parsed.values.get("note")
        elif parsed.type == "cc":
            src_kwargs["event"] = "cc"
            src_kwargs["cc"] = parsed.values.get("control")
            # 踏板/开关类控制器：只在按下时（≥64）触发一次
            val = parsed.values.get("value", 0)
            if val >= 64:
                src_kwargs["value_min"] = 64
        elif parsed.type in ("pitch_bend", "program_change", "aftertouch", "poly_aftertouch"):
            src_kwargs["event"] = parsed.type
        source = BindingSource.from_dict(src_kwargs)
        # 信号名：用参数生成可读名字，比如 "cc0_21"
        if parsed.type == "cc":
            sig = f"cc{parsed.channel}_{parsed.values.get('control', '?')}"
        elif parsed.type in ("note_on", "note_off"):
            sig = f"note{parsed.values.get('note', '?')}"
        else:
            sig = f"{parsed.type}_{parsed.channel}"
        binding = Binding(signal=sig, sources=[source])
        dlg = BindingEditDialog(binding, parent=self)
        if dlg.exec() == BindingEditDialog.DialogCode.Accepted:
            self.app.config.bindings.append(dlg.result_binding())
            self.bindings_view._rebuild()
            self.bindings_view._save()
            # 切到绑定 Tab 让用户看到
            self.tabs.setCurrentWidget(self.bindings_view.parentWidget())

    def closeEvent(self, event) -> None:
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