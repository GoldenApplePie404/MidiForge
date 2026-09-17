from ui.channel_matrix import ChannelMatrix


def test_channel_matrix_16_cells(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    assert len(m._cells) == 16


def test_channel_matrix_pulse_colors(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    m.pulse(3)
    assert m._cells[3].styleSheet() == ChannelMatrix._lit_style()


def test_channel_matrix_pulse_resets_on_repeat(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    m.pulse(3)
    m.pulse(3)
    assert len(m._timers) == 1
    qtbot.wait(600)
    assert m._cells[3].styleSheet() == ChannelMatrix._dull_style()


def test_channel_matrix_pulse_out_of_range(qtbot):
    m = ChannelMatrix()
    qtbot.addWidget(m)
    m.pulse(99)
    assert m._timers == {}
    assert m._cells[0].styleSheet() == ChannelMatrix._dull_style()


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
    # 驱动类型下拉选 "cc"
    idx = v._type_combo.findData("cc")
    v._type_combo.setCurrentIndex(idx)
    assert v.table.rowCount() == 1


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


from core.bindings import Binding, BindingConfig, BindingSource
import ui.bindings_view
from ui.bindings_view import BindingsView


def test_bindings_view_renders_config(qtbot):
    cfg = BindingConfig(bindings=[Binding(signal="drum_hit", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 9, "event": "note_on", "note": 36})])])
    v = BindingsView(config=cfg)
    qtbot.addWidget(v)
    assert v.table.rowCount() == 1
    assert v.table.item(0, 1).text() == "drum_hit"  # col 0=checkbox, col 1=signal


def test_bindings_view_save_config(qtbot, tmp_path):
    v = BindingsView(config=BindingConfig())
    qtbot.addWidget(v)
    out = tmp_path / "b.json"
    v.save_to(str(out))
    loaded = BindingConfig.from_file(str(out))
    assert loaded == v.current_config()


def test_bindings_view_remove_whole_signal(qtbot):
    cfg = BindingConfig(bindings=[Binding(signal="a", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 9}),
        BindingSource.from_dict({"type": "keyboard", "key": "space"})])])
    v = BindingsView(config=cfg)
    qtbot.addWidget(v)
    v.table.selectRow(0)
    # 新表格按 binding 聚合，一个 binding 只占一行
    assert v.table.rowCount() == 1
    # 跳过 QMessageBox 确认对话框
    import ui.bindings_view
    ui.bindings_view.QMessageBox.question = lambda *a, **k: ui.bindings_view.QMessageBox.StandardButton.Yes
    v._remove_row()
    assert v.current_config().bindings == []
    assert v.table.rowCount() == 0


def test_bindings_view_import_corrupt(qtbot, tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(ui.bindings_view.QFileDialog, "getOpenFileName",
                        lambda *a, **k: (str(bad), ""))
    warnings = []
    monkeypatch.setattr(ui.bindings_view.QMessageBox, "warning",
                        lambda *a, **k: warnings.append(a))
    v = BindingsView(config=BindingConfig())
    qtbot.addWidget(v)
    v._import()
    assert v.current_config().bindings == []
    assert len(warnings) == 1


def test_bindings_view_edit_dialog(qtbot, monkeypatch):
    """验证新增对话框：mock 对话框返回 OK，binding 被正确加进 config。"""
    from ui.bindings_view import BindingEditDialog
    cfg = BindingConfig()
    v = BindingsView(config=cfg)
    qtbot.addWidget(v)

    # mock _add_row 内部用 BindingEditDialog.exec 返回 OK
    fake_binding = Binding(signal="solo", sources=[
        BindingSource.from_dict({"type": "midi", "channel": 0, "event": "note_on", "note": 60})],
        virtual_midi={"channel": 3})

    class FakeDialog:
        def __init__(self, *a, **k): pass
        def exec(self): return BindingEditDialog.DialogCode.Accepted
        def result_binding(self): return fake_binding

    monkeypatch.setattr(ui.bindings_view, "BindingEditDialog", FakeDialog)
    v._add_row()
    assert len(cfg.bindings) == 1
    assert cfg.bindings[0].signal == "solo"
    assert cfg.bindings[0].virtual_midi == {"channel": 3}
    # 表格已刷新
    assert v.table.rowCount() == 1
    assert v.table.item(0, 1).text() == "solo"


from app import create_binding_config_path
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow


def test_main_window_constructs(qtbot, tmp_path):
    import api
    app = api.create_app(bindings_path=str(tmp_path / "b.json"))
    win = MainWindow(app=app, plugins_dirs=[str(tmp_path / "plugins")])
    qtbot.addWidget(win)
    assert win.windowTitle() != ""


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


