import sys
from PyQt6.QtWidgets import QApplication
from core.bindings import Binding, BindingSource, BindingConfig
from ui.bindings_view import BindingsView, BindingEditDialog

app = QApplication(sys.argv)

# 模拟 BindingsView 初始化
cfg = BindingConfig(bindings=[])
bv = BindingsView(cfg)
bv.set_app(None)  # 不注入 app，看 _save 的行为

print('=== 初始 bindings ===')
print('len:', len(bv._config.bindings))

# 模拟用户点"新增绑定" → 打开对话框 → 填好 → 确定
dlg = BindingEditDialog(binding=None, parent=bv)
# 手动填值（模拟用户编辑）
dlg.signal_edit.setText('test_binding')
dlg.vm_enable.setChecked(True)
dlg.vm_type.setCurrentIndex(1)  # note_on
dlg.vm_channel.setValue(5)
dlg.key_enable.setChecked(True)
dlg.key_edit.setText('f2')
dlg.midi_channel.setValue(3)
dlg.midi_event_combo.setCurrentIndex(dlg.midi_event_combo.findData('cc'))
dlg.midi_cc.setValue(11)
dlg.midi_value_min.setValue(64)
dlg._save_source_edits()

# 调 _accept
dlg._accept()
result = dlg.result_binding()
print()
print('=== result_binding() ===')
print('signal:', result.signal)
print('sources:', [s.to_dict() for s in result.sources])
print('virtual_midi:', result.virtual_midi)
print('key_out:', result.key_out)
print()
print('dlg result code:', dlg.result(), '(1=Accepted)')

# 模拟 _add_row 里的逻辑
bv._config.bindings.append(result)
print('=== append 后 ===')
print('len:', len(bv._config.bindings))

bv._rebuild()
print('=== _rebuild 后 table row count ===')
print('rows:', bv.table.rowCount())

# 现在注入 app 再试一次 _save
class FakeApp:
    def __init__(self):
        self.saved = False
    def save_bindings(self):
        self.saved = True
        print('  save_bindings CALLED')
    def execute_actions(self, *a, **k):
        print('  execute_actions called')
    class _Bus:
        def publish(self, *a, **k): pass
    bus = _Bus()

fake = FakeApp()
bv.set_app(fake)
bv._save()
print('_save after set_app: saved=', fake.saved)

# 最后看表格里第一行的文本
if bv.table.rowCount() > 0:
    for c in range(bv.table.columnCount()):
        item = bv.table.item(0, c)
        print(f'  col{c}:', repr(item.text() if item else None))
else:
    print('TABLE EMPTY!')
