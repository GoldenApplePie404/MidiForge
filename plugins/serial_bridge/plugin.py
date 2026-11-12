"""SerialBridge — 串口 ↔ MIDI 双向桥接器（v1 透传 + v2 MFSP 帧协议）。

定位：让只有 UART 的自制硬件（开发板合成器/控制器）进入 MidiForge 的
MIDI 世界。双协议可插拔（面板下拉切换）：
  - 透传：串口上跑标准 MIDI 1.0 字节流（Hairless 兼容，现成库对接）
  - MFSP：帧协议（CRC8 + 心跳 + debug 通道 + 虚拟端口），配 device/ 端
    mfsp_arduino.h；虚拟端口经「双向映射表」在电脑端翻译成 cc/note，
    固件不关心 MIDI 语义

线程模型（关键，与项目"队列单路径"惯例一致）：
    pyserial 读线程 → out_queue 原始字节块
    app 分发线程（midi.all 回调）→ 编码 → worker.write（锁保护）
    Qt 主线程 → _poll（50ms）统一消费：字节解码 inject + 映射翻译 +
    debug 文本 + 监视区刷新 + 状态行（MFSP 心跳在线判定 3s 超时）
    所有 UI 更新都在主线程轮询里做，不需要信号桥。

MIDI→串口 方向过滤 source in ("serial", "virtual")：
    serial 是设备自己的消息不能发回去；virtual 是宿主虚拟端口的回声
    （同一条消息已经从原路径出去过，再转发就重复了）。
"""

import json
import queue
import sys
import time
import collections
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from core.plugin import Plugin
from ui import style_qss as QSS

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _codec          # noqa: E402
import _serial         # noqa: E402


BAUD_RATES = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

DEFAULT_CONFIG = {
    "port": None,          # 串口名（COM3 等）
    "baud": 115200,
    "channel": None,       # None=保持设备原始通道；0-15=统一改写
    "to_midi": True,       # 串口 → MIDI
    "to_serial": False,    # MIDI → 串口
    "protocol": "passthrough",  # "passthrough" 透传 | "mfsp" 帧协议
    "map_in": [],          # 入向映射行（_codec.map_in 注释里的 dict 结构）
    "map_out": [],         # 出向映射行（_codec.map_out_frames 注释）
}

# MFSP 心跳在线判定秒数（与 _codec 侧常量一致）
HB_TIMEOUT = _codec._MFSP_HEARTBEAT_TIMEOUT


class SerialBridge(Plugin):
    """串口 ↔ MIDI 双向桥接插件。"""

    name = "serial_bridge"
    version = "0.2"
    status = "dev"
    description = "串口 ↔ MIDI 双向桥接：透传/MFSP 双协议 + 虚拟端口双向映射表"

    # ============ 生命周期 ============

    def on_activate(self, app) -> None:
        self.app = app
        self.log = app.log
        self._plugin_dir = Path(__file__).parent
        self._config_path = self._plugin_dir / "config.json"
        self.config = self._load_config()

        self._panel = None
        self._worker = None
        self._decoder = None
        self._sid = None
        self._protocol = None          # 连接期间的协议快照（None=未连接）
        self._edges = {}               # map_in 边缘状态 {(kind, port): bool}
        self._last_worker_state = None  # 上一轮 worker 状态（在线转变 → ping）
        self._map_loading = False      # 映射表回填期间抑制 collect

        self._rx_bytes = 0
        self._tx_bytes = 0
        self._decode_errors = 0
        # MIDI 线程 → 主线程的出向监视队列（deque.append 线程安全）
        self._mon_out = collections.deque(maxlen=200)

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)

        self._poll = QTimer()
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._tick)

        self._sid = app.subscribe("midi.all", self._on_midi_all)
        self._poll.start()

    def on_deactivate(self) -> None:
        try:
            self._stop_worker()
            self._poll.stop()
            self._save_now()
            self._save_timer.stop()
            if self.app is not None and self._sid is not None:
                self.app.bus.unsubscribe(self._sid)
        except Exception as exc:
            print(f"[serial_bridge] 清理失败: {exc}", file=sys.stderr)

    # ============ 配置 ============

    def _load_config(self) -> dict:
        """读 config.json；缺失/损坏回落默认值。"""
        data = {}
        if self._config_path.exists():
            try:
                with open(self._config_path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        cfg = dict(DEFAULT_CONFIG)
        if data.get("port"):
            cfg["port"] = str(data["port"])
        baud = data.get("baud")
        if isinstance(baud, int) and baud in BAUD_RATES:
            cfg["baud"] = baud
        ch = data.get("channel")
        cfg["channel"] = int(ch) if isinstance(ch, int) and 0 <= ch <= 15 else None
        cfg["to_midi"] = bool(data.get("to_midi", True))
        cfg["to_serial"] = bool(data.get("to_serial", False))
        proto = data.get("protocol")
        cfg["protocol"] = proto if proto in ("passthrough", "mfsp") else "passthrough"
        cfg["map_in"] = self._norm_map_rows(data.get("map_in"), inbound=True)
        cfg["map_out"] = self._norm_map_rows(data.get("map_out"), inbound=False)
        return cfg

    @staticmethod
    def _norm_map_rows(raw, inbound: bool) -> list:
        """映射行清洗：结构/值域不对的行丢弃或回落默认值。"""
        out = []
        if not isinstance(raw, list):
            return out
        for r in raw:
            if not isinstance(r, dict):
                continue

            def _num(key, lo, hi, dflt):
                v = r.get(key)
                return int(v) if isinstance(v, int) and lo <= v <= hi else dflt

            if inbound:
                out.append({
                    "kind": r.get("kind") if r.get("kind") in ("analog", "digital") else "analog",
                    "port": _num("port", 0, 15, 0),
                    "target": r.get("target") if r.get("target") in ("cc", "note") else "cc",
                    "num": _num("num", 0, 127, 0),
                    "enabled": bool(r.get("enabled", True)),
                })
            else:
                out.append({
                    "type": r.get("type") if r.get("type") in ("cc", "note") else "cc",
                    "num": _num("num", 0, 127, 0),
                    "target": r.get("target") if r.get("target") in ("analog", "digital") else "analog",
                    "port": _num("port", 0, 15, 0),
                    "enabled": bool(r.get("enabled", True)),
                })
        return out

    def _schedule_save(self) -> None:
        """400ms 防抖落盘（只在主线程调用）。"""
        if self._save_timer is not None:
            self._save_timer.start(400)

    def _save_now(self) -> None:
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            self.log.warning("配置写入失败: %s", exc)

    # ============ 数据通路 ============

    def _tick(self) -> None:
        """主线程 50ms 轮询：消费串口字节 + 刷新监视区/状态行。"""
        try:
            worker = self._worker
            if worker is not None:
                got = False
                rows_in = self.config.get("map_in", [])
                ch = self._mfsp_channel()
                while True:
                    try:
                        chunk = worker.out_queue.get_nowait()
                    except queue.Empty:
                        break
                    got = True
                    self._rx_bytes += len(chunk)
                    if self._panel is not None:
                        self._monitor.appendPlainText(
                            "→ " + chunk.hex(" ").upper())
                    if self.config.get("to_midi", True):
                        for parsed in self._decoder.feed(chunk):
                            # MFSP 虚拟端口事件不是 MIDI：按映射表翻译后才注入
                            if parsed.type in ("analog_in", "digital_in"):
                                msgs = _codec.map_in(parsed, rows_in, ch,
                                                     self._edges)
                            else:
                                msgs = (parsed,)
                            for m in msgs:
                                self.app.engine.inject(m)
                if got:
                    self._decode_errors = self._decoder.errors
                # MFSP：设备 debug 文本进监视区
                if (self._panel is not None and self._protocol == "mfsp"
                        and self._decoder is not None):
                    while self._decoder.debug_lines:
                        self._monitor.appendPlainText(
                            "» " + self._decoder.debug_lines.popleft())
                # 在线状态转变（首次上线/断线重连成功）→ MFSP 发 ping 探心跳
                st = worker.last_state
                if st != self._last_worker_state:
                    self._last_worker_state = st
                    if st == "在线" and self._protocol == "mfsp":
                        worker.write(_codec.MfspCodec.encode_ping())
            # 出向监视（MIDI 线程经 _mon_out 队列转来）
            if self._panel is not None:
                while self._mon_out:
                    try:
                        self._monitor.appendPlainText(self._mon_out.popleft())
                    except IndexError:
                        break
            self._refresh_status()
        except Exception as exc:
            self.log.warning("串口消费失败: %s", exc)

    def _mfsp_channel(self) -> int:
        """MFSP 通道不进协议：统一取 UI 配置，未配置时默认 ch0。"""
        ch = self.config.get("channel")
        return ch if ch is not None else 0

    def _on_midi_all(self, parsed) -> None:
        """MIDI→串口 方向：过滤回环来源后编码发出（app 分发线程）。"""
        try:
            if not self.config.get("to_serial", False):
                return
            if parsed.source in ("serial", "virtual"):
                return
            worker = self._worker
            if worker is None:
                return
            data = self._encode_outbound(parsed)
            if not data:
                return
            worker.write(data)
            self._tx_bytes += len(data)
            self._mon_out.append("← " + data.hex(" ").upper())
        except Exception as exc:
            self.log.warning("串口转发失败: %s", exc)

    def _encode_outbound(self, parsed) -> bytes:
        """按协议编码出向：MFSP 先查出向映射（命中改道虚拟端口帧），
        未命中走标准 CMD 直通；透传走 MIDI 字节流编码。"""
        if self._protocol == "mfsp":
            rows = [r for r in self.config.get("map_out", []) if r.get("enabled")]
            frames = _codec.map_out_frames(parsed, rows)
            if frames:
                return b"".join(frames)
            if self._decoder is not None:
                return self._decoder.encode_out(parsed)
            return b""
        return _codec.encode_parsed(parsed)

    # ============ 连接管理 ============

    def _on_connect(self) -> None:
        try:
            if self._worker is not None:
                self._stop_worker()
                return
            if not _serial._HAS_SERIAL:
                self._lbl_status.setText("pyserial 未安装：pip install pyserial")
                return
            port = self._combo_port.currentData()
            if not port:
                self._lbl_status.setText("请先选择串口")
                return
            self.config["port"] = port
            self.config["baud"] = self._combo_baud.currentData()
            proto = self._combo_proto.currentData()
            self.config["protocol"] = proto
            self._protocol = proto
            self._edges = {}               # 重连后边缘状态从零开始
            override = self.config.get("channel")
            if proto == "mfsp":
                self._decoder = _codec.MfspCodec(channel_override=override)
            else:
                self._decoder = _codec.PassthroughDecoder(channel_override=override)
            worker = _serial.SerialWorker(
                port, int(self.config["baud"]))
            self._worker = worker
            self._rx_bytes = self._tx_bytes = 0
            self._decode_errors = 0
            self._last_worker_state = None
            worker.start()
            self._btn_connect.setText("断开")
            self._combo_port.setEnabled(False)
            self._combo_baud.setEnabled(False)
            self._combo_proto.setEnabled(False)
            self._btn_refresh.setEnabled(False)
            self._refresh_status()
            self._schedule_save()
        except Exception as exc:
            self._worker = None
            self._decoder = None
            self._protocol = None
            self.log.warning("连接失败: %s", exc)
            if self._panel is not None:
                self._lbl_status.setText(f"连接失败：{exc}")

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        self._decoder = None
        self._protocol = None
        self._last_worker_state = None
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass
        if self._panel is not None:
            self._btn_connect.setText("连接")
            self._combo_port.setEnabled(True)
            self._combo_baud.setEnabled(True)
            self._combo_proto.setEnabled(True)
            self._btn_refresh.setEnabled(True)
            self._lbl_status.setText("已断开")

    def _refresh_status(self) -> None:
        if self._panel is None:
            return
        state = self._worker.last_state if self._worker is not None else "未连接"
        hb = ""
        if self._protocol == "mfsp" and self._decoder is not None:
            lh = self._decoder.last_heartbeat
            if lh is None:
                hb = " · 心跳: 无"
            elif time.monotonic() - lh <= HB_TIMEOUT:
                hb = " · 心跳: 正常"
            else:
                hb = " · 心跳: 超时"
        self._lbl_status.setText(
            f"{state}{hb} · 收 {self._rx_bytes} B · 发 {self._tx_bytes} B"
            f" · 解码错误 {self._decode_errors}")

    # ============ 面板 ============

    def create_panel(self) -> QWidget:
        box = QWidget()
        box.setStyleSheet(f"background-color: {QSS.BG}; border-radius: 8px;")
        root = QVBoxLayout(box)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QLabel("🔌 SerialBridge · 串口 ↔ MIDI 双向桥")
        title.setStyleSheet(f"color: {QSS.TEXT}; font-size: 14px; font-weight: 700;")
        root.addWidget(title)

        hint = QLabel("串口 ↔ MIDI 双向桥。透传协议：串口上跑标准 MIDI 字节流"
                      "（115200 波特，Hairless 兼容，设备端用现成库）；MFSP 帧协议："
                      "CRC 校验 + 心跳 + debug + 虚拟端口（analog/digital 0-15），"
                      "端口语义经「映射表」在电脑端翻译成 CC/音符，固件不用懂 MIDI。"
                      "桥进来的消息走统一分发，绑定/示波器/录制器自动生效。")
        hint.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ---- 工具条：串口 / 波特率 / 通道改写 / 连接 ----
        bar = QHBoxLayout()
        bar.setSpacing(6)

        lbl_port = QLabel("串口:")
        lbl_port.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        bar.addWidget(lbl_port)
        self._combo_port = QComboBox()
        self._combo_port.setMinimumWidth(170)
        bar.addWidget(self._combo_port)

        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setToolTip("重新枚举串口（热插拔后手动刷新）")
        self._btn_refresh.clicked.connect(self._refresh_ports)
        bar.addWidget(self._btn_refresh)

        lbl_baud = QLabel("波特率:")
        lbl_baud.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        bar.addWidget(lbl_baud)
        self._combo_baud = QComboBox()
        for rate in BAUD_RATES:
            self._combo_baud.addItem(str(rate), rate)
        pos = self._combo_baud.findData(self.config.get("baud"))
        self._combo_baud.setCurrentIndex(pos if pos >= 0 else 4)
        self._combo_baud.currentIndexChanged.connect(self._on_baud)
        bar.addWidget(self._combo_baud)

        lbl_ch = QLabel("通道改写:")
        lbl_ch.setToolTip("把设备发来的所有消息统一改写到指定 MIDI 通道（默认保持原样）")
        lbl_ch.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        bar.addWidget(lbl_ch)
        self._combo_channel = QComboBox()
        self._combo_channel.addItem("保持原样", None)
        for i in range(16):
            self._combo_channel.addItem(f"ch{i + 1}", i)
        pos = self._combo_channel.findData(self.config.get("channel"))
        self._combo_channel.setCurrentIndex(pos if pos >= 0 else 0)
        self._combo_channel.currentIndexChanged.connect(self._on_channel)
        bar.addWidget(self._combo_channel)

        lbl_proto = QLabel("协议:")
        lbl_proto.setToolTip("透传 = 标准 MIDI 字节流；MFSP = 帧协议（CRC/心跳/虚拟端口）")
        lbl_proto.setStyleSheet(f"color: {QSS.MUTED}; font-size: 11px;")
        bar.addWidget(lbl_proto)
        self._combo_proto = QComboBox()
        self._combo_proto.addItem("透传 · MIDI 字节流", "passthrough")
        self._combo_proto.addItem("MFSP · 帧协议", "mfsp")
        pos = self._combo_proto.findData(self.config.get("protocol"))
        self._combo_proto.setCurrentIndex(pos if pos >= 0 else 0)
        self._combo_proto.currentIndexChanged.connect(self._on_protocol)
        bar.addWidget(self._combo_proto)

        bar.addStretch(1)

        self._btn_connect = QPushButton("连接")
        self._btn_connect.clicked.connect(self._on_connect)
        bar.addWidget(self._btn_connect)
        root.addLayout(bar)

        # ---- 方向 ----
        dir_bar = QHBoxLayout()
        dir_bar.setSpacing(12)
        self._chk_to_midi = QCheckBox("串口 → MIDI（桥进宿主）")
        self._chk_to_midi.setChecked(bool(self.config.get("to_midi", True)))
        self._chk_to_midi.setStyleSheet(f"color: {QSS.TEXT};")
        self._chk_to_midi.toggled.connect(self._on_to_midi)
        dir_bar.addWidget(self._chk_to_midi)
        self._chk_to_serial = QCheckBox("MIDI → 串口（宿主驱动设备）")
        self._chk_to_serial.setChecked(bool(self.config.get("to_serial", False)))
        self._chk_to_serial.setStyleSheet(f"color: {QSS.TEXT};")
        self._chk_to_serial.toggled.connect(self._on_to_serial)
        dir_bar.addWidget(self._chk_to_serial)
        dir_bar.addStretch(1)
        root.addLayout(dir_bar)

        # ---- 状态行 ----
        self._lbl_status = QLabel("未连接")
        self._lbl_status.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        root.addWidget(self._lbl_status)

        # ---- 双向映射表（仅 MFSP 协议显示）----
        self._map_box = self._build_map_section()
        root.addWidget(self._map_box)
        self._map_box.setVisible(self._combo_proto.currentData() == "mfsp")

        # ---- 十六进制监视区 ----
        self._monitor = QPlainTextEdit()
        self._monitor.setReadOnly(True)
        self._monitor.setMaximumBlockCount(200)
        self._monitor.setPlaceholderText(
            "十六进制监视（最近 200 行）·  → 串口进   ← 串口出")
        self._monitor.setStyleSheet(
            f"background-color: {QSS.CARD}; color: {QSS.TEXT};"
            f"border: 1px solid {QSS.BORDER}; border-radius: 6px;"
            f"font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(self._monitor, 1)

        self._panel = box
        self._refresh_ports()
        self._refresh_status()
        return box

    # ---- 双向映射表 UI ----

    def _build_map_section(self) -> QWidget:
        """入向（虚拟端口 → MIDI）/ 出向（MIDI → 虚拟端口）两张表。

        映射全部在电脑端配置：固件只认端口号，换映射改表格即可。
        行内编辑 + 启用勾选，改动即防抖落盘。
        """
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        cap = QLabel("双向映射表（仅 MFSP）· 固件只认端口，语义在电脑端配置")
        cap.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        v.addWidget(cap)

        lbl_in = QLabel("入向：虚拟端口 → MIDI（analog→CC 连续流 / →音符阈值触发；"
                        "digital→音符或 CC，变化才发）")
        lbl_in.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        v.addWidget(lbl_in)
        self._tbl_in = self._make_map_table(inbound=True)
        v.addWidget(self._tbl_in)
        btn_in = QPushButton("＋ 添加入向映射")
        btn_in.clicked.connect(lambda: self._map_add_row(self._tbl_in, True))
        v.addWidget(btn_in)

        lbl_out = QLabel("出向：MIDI → 虚拟端口（CC→analog 电平 / →digital 阈值 64；"
                         "音符→digital 开关 / →analog 力度作电平）。命中映射的消息"
                         "只发映射帧，未命中走标准 note/cc 直通")
        lbl_out.setWordWrap(True)
        lbl_out.setStyleSheet(f"color: {QSS.MUTED}; font-size: 10px;")
        v.addWidget(lbl_out)
        self._tbl_out = self._make_map_table(inbound=False)
        v.addWidget(self._tbl_out)
        btn_out = QPushButton("＋ 添加出向映射")
        btn_out.clicked.connect(lambda: self._map_add_row(self._tbl_out, False))
        v.addWidget(btn_out)

        self._fill_map_tables()
        return box

    def _make_map_table(self, inbound: bool) -> QTableWidget:
        t = QTableWidget(0, 6)
        if inbound:
            t.setHorizontalHeaderLabels(["启用", "类型", "端口", "→ MIDI", "编号", ""])
        else:
            t.setHorizontalHeaderLabels(["启用", "消息", "编号", "→ 端口", "端口", ""])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.setMinimumHeight(64)
        t.setMaximumHeight(120)
        t.setStyleSheet(
            f"background-color: {QSS.CARD}; color: {QSS.TEXT};"
            f"border: 1px solid {QSS.BORDER}; border-radius: 6px;"
            f"font-size: 11px;")
        t.itemChanged.connect(self._on_map_changed)   # 启用勾选列
        return t

    def _map_add_row(self, table: QTableWidget, inbound: bool,
                     row_cfg: dict = None) -> None:
        """插一行：启用勾选 + 下拉/SpinBox 组合 + 删除按钮。"""
        r = table.rowCount()
        table.insertRow(r)
        cfg = row_cfg or {}

        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable
                     | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked if cfg.get("enabled", True)
                          else Qt.CheckState.Unchecked)
        table.setItem(r, 0, chk)

        def _combo(items, selected):
            c = QComboBox()
            for label, data in items:
                c.addItem(label, data)
            i = c.findData(selected)
            if i >= 0:
                c.setCurrentIndex(i)
            c.currentIndexChanged.connect(self._on_map_changed)
            return c

        def _spin(lo, hi, val):
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(int(val))
            s.valueChanged.connect(self._on_map_changed)
            return s

        if inbound:
            table.setCellWidget(r, 1, _combo([("模拟", "analog"), ("数字", "digital")],
                                             cfg.get("kind", "analog")))
            table.setCellWidget(r, 2, _spin(0, 15, cfg.get("port", 0)))
            table.setCellWidget(r, 3, _combo([("CC", "cc"), ("音符", "note")],
                                             cfg.get("target", "cc")))
            table.setCellWidget(r, 4, _spin(0, 127, cfg.get("num", 0)))
        else:
            table.setCellWidget(r, 1, _combo([("CC", "cc"), ("音符", "note")],
                                             cfg.get("type", "cc")))
            table.setCellWidget(r, 2, _spin(0, 127, cfg.get("num", 0)))
            table.setCellWidget(r, 3, _combo([("模拟", "analog"), ("数字", "digital")],
                                             cfg.get("target", "analog")))
            table.setCellWidget(r, 4, _spin(0, 15, cfg.get("port", 0)))
        btn = QPushButton("删")
        btn.setToolTip("删除此映射行")
        btn.clicked.connect(lambda _, tb=table, b=btn: self._map_del_row(tb, b))
        table.setCellWidget(r, 5, btn)

    def _map_del_row(self, table: QTableWidget, btn: QPushButton) -> None:
        # SerialBridge 不是 QWidget，没有 sender()；直接按控件身份定位行
        for r in range(table.rowCount()):
            if table.cellWidget(r, 5) is btn:
                table.removeRow(r)
                self._on_map_changed()
                return

    def _fill_map_tables(self) -> None:
        """从配置回填两张表（抑制 itemChanged 触发的回写循环）。"""
        self._map_loading = True
        try:
            self._tbl_in.setRowCount(0)
            for row in self.config.get("map_in", []):
                self._map_add_row(self._tbl_in, True, row)
            self._tbl_out.setRowCount(0)
            for row in self.config.get("map_out", []):
                self._map_add_row(self._tbl_out, False, row)
        finally:
            self._map_loading = False

    def _collect_map(self, table: QTableWidget, inbound: bool) -> list:
        rows = []
        for r in range(table.rowCount()):
            it = table.item(r, 0)
            w1, w2, w3, w4 = (table.cellWidget(r, i) for i in (1, 2, 3, 4))
            if w1 is None or w3 is None:
                continue
            enabled = it is not None and it.checkState() == Qt.CheckState.Checked
            if inbound:
                rows.append({"kind": w1.currentData(), "port": w2.value(),
                             "target": w3.currentData(), "num": w4.value(),
                             "enabled": enabled})
            else:
                rows.append({"type": w1.currentData(), "num": w2.value(),
                             "target": w3.currentData(), "port": w4.value(),
                             "enabled": enabled})
        return rows

    def _on_map_changed(self, *_args) -> None:
        """表格任何改动 → 回写配置 + 防抖落盘（回填期间抑制）。"""
        try:
            if self._map_loading or self._tbl_in is None:
                return
            self.config["map_in"] = self._collect_map(self._tbl_in, True)
            self.config["map_out"] = self._collect_map(self._tbl_out, False)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("映射表更新失败: %s", exc)

    # ---- 面板回调（主线程）----

    def _refresh_ports(self) -> None:
        try:
            self._combo_port.clear()
            if not _serial._HAS_SERIAL:
                self._combo_port.addItem("pyserial 未安装", None)
                self._btn_connect.setEnabled(False)
                return
            ports = _serial.list_serial_ports()
            if not ports:
                self._combo_port.addItem("未发现串口", None)
            else:
                for dev, desc in ports:
                    text = f"{dev}" + (f" — {desc}" if desc and desc != dev else "")
                    self._combo_port.addItem(text, dev)
            saved = self.config.get("port")
            idx = self._combo_port.findData(saved)
            if idx >= 0:
                self._combo_port.setCurrentIndex(idx)
            self._btn_connect.setEnabled(True)
        except Exception as exc:
            self.log.warning("串口枚举失败: %s", exc)

    def _on_baud(self, index: int) -> None:
        try:
            data = self._combo_baud.itemData(index)
            if data is not None:
                self.config["baud"] = int(data)
                self._schedule_save()
        except Exception as exc:
            self.log.warning("波特率设置失败: %s", exc)

    def _on_channel(self, index: int) -> None:
        try:
            self.config["channel"] = self._combo_channel.itemData(index)
            # 已连接时热生效：按当前协议重建解码器（只影响后续消息）
            if self._worker is not None:
                override = self.config.get("channel")
                if self._protocol == "mfsp":
                    self._decoder = _codec.MfspCodec(channel_override=override)
                else:
                    self._decoder = _codec.PassthroughDecoder(channel_override=override)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("通道改写失败: %s", exc)

    def _on_protocol(self, index: int) -> None:
        try:
            data = self._combo_proto.itemData(index)
            if data not in ("passthrough", "mfsp"):
                return
            self.config["protocol"] = data
            # 映射表只在 MFSP 协议下有意义；协议切换仅允许在断开状态
            # （连接时该下拉被禁用），所以不用重建解码器
            if self._map_box is not None:
                self._map_box.setVisible(data == "mfsp")
            self._schedule_save()
        except Exception as exc:
            self.log.warning("协议切换失败: %s", exc)

    def _on_to_midi(self, on: bool) -> None:
        try:
            self.config["to_midi"] = bool(on)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("方向设置失败: %s", exc)

    def _on_to_serial(self, on: bool) -> None:
        try:
            self.config["to_serial"] = bool(on)
            self._schedule_save()
        except Exception as exc:
            self.log.warning("方向设置失败: %s", exc)
