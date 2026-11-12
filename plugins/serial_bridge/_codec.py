"""串口桥编解码器（纯逻辑：无 Qt、无 pyserial），双协议可插拔：

协议 1（v1）标准 MIDI 字节流透传 —— PassthroughDecoder / encode_parsed
协议 2（v2）MFSP 帧协议 —— MfspCodec / mfsp_frame / map_in / map_out_frames

两个解码器同一 feed(bytes) -> list 接口，plugin.py 按协议下拉切换；
反向编码：透传用 encode_parsed，MFSP 用 MfspCodec.encode_out +
map_out_frames（虚拟端口映射）。

====================== 协议 1：透传 ======================

串口上跑的就是 MIDI 1.0 字节流（Hairless MIDI-Serial Bridge 兼容）：
设备端用 Arduino MIDI Library / espMIDI 等现成库即可对接。UART 是无边界
字节流，本模块用状态机按 MIDI 字节规则自对齐：

  - 状态字节 bit7=1：
      0x80-0xEF 通道消息 → 新消息起始（重设 Running Status）
      0xF0 SysEx 开始 → 收集到 0xF7 为止
      0xF8-0xFF 实时消息（时钟/active sensing）→ 静默忽略，不打断 Running Status
      0xF1-0xF6 系统公共 → v1 忽略，清 Running Status（后续残留数据字节按垃圾丢弃）
  - 数据字节 bit7=0：填当前消息；没有当前消息但有 Running Status 时
    按 Running Status 起一条新消息；都没有就是断流残留垃圾 → 计入 errors 丢弃

类型名/值域与 midi/parser.py 的归一化命名完全一致（cc / pitch_bend /
poly_aftertouch / aftertouch …），raw_hex、description 风格也对齐，
保证下游插件无差别对待来源是串口的消息。

====================== 协议 2：MFSP v1 帧协议 ======================

包式帧，双向对称（详见 device/README.md 与共享测试向量 mfsp_vectors.json）：

    | SYNC 0x7E | LEN | CMD | PAYLOAD(0-32B) | CRC8 |
                 覆盖 CMD+PAYLOAD        多项式 0x07，初值 0x00

设备→宿主：0x10 note_on / 0x11 note_off / 0x20 cc / 0x30 pitchbend /
0x40 heartbeat(uptime) / 0x50 analog_in(port, 0-4095 小端) /
0x51 digital_in(port, state) / 0xF0 debug(UTF-8 文本)
宿主→设备：0x80/0x81/0x82 note_on/note_off/cc / 0x83 analog_out /
0x84 digital_out / 0x8F ping(设备回 heartbeat)

要点：
  - 通道不进协议：解码出的 MIDI 通道统一取 UI 配置（默认 ch0）
  - 虚拟端口（analog/digital 0-15）不是 MIDI 消息：解码成 type 为
    "analog_in"/"digital_in" 的原始端口事件，由 map_in 按映射表翻译成
    cc/note 才注入引擎；未映射的端口事件直接丢弃
  - SYNC 扫描状态机：坏 CRC / 非法长度 / 帧中途出现 SYNC 都会重同步；
    坏 CRC 时从帧内第一个字节起重扫（负载里可能嵌着下一个真帧的 SYNC）
  - heartbeat/debug 不产生 MIDI 消息：心跳刷新 last_heartbeat（3s 超时
    判在线），debug 文本进 debug_lines（监视区显示）
"""

import collections
import time

from midi.parser import ParsedMessage

# 通道消息：状态字节高半字节 → 数据字节个数（0x8-0xB、0xE 两个，0xC/0xD 一个）
_DATA_BYTES = {0x8: 2, 0x9: 2, 0xA: 2, 0xB: 2, 0xC: 1, 0xD: 1, 0xE: 2}

# SysEx 收集长度上限：F7 丢失时防止无限膨胀（正常 SysEx 远小于此）
_SYSEX_MAX = 1024


class PassthroughDecoder:
    """MIDI 字节流 → ParsedMessage 状态机。

    channel_override: None=保持设备原始通道；0-15=统一改写到指定通道
    （DIY 单片机设备通道乱飞时，把映射固定到 chN 方便绑定）。改写只影响
    ParsedMessage 的 channel/event_key，raw_hex 仍是线上原始字节。
    """

    def __init__(self, channel_override: int = None):
        self.errors = 0            # 垃圾数据字节 / 损坏 SysEx 计数（UI 显示用）
        self._override = channel_override
        self._status = None        # Running Status（0x80-0xEF）
        self._need = 0             # 当前消息还差几个数据字节
        self._data = []            # 已收数据字节
        self._sysex = None         # 非 None 时正在收集 SysEx payload（不含 F0/F7）

    def reset(self) -> None:
        """清空半截消息状态（重连时调用；errors 计数保留）。"""
        self._status = None
        self._need = 0
        self._data = []
        self._sysex = None

    def feed(self, chunk: bytes) -> list:
        """喂入任意切分的字节块，返回解析出的消息列表（1 字节粒度可复现）。"""
        out = []
        for b in chunk:
            msg = self._byte(b)
            if msg is not None:
                out.append(msg)
        return out

    # ---- 状态机内核 ----

    def _byte(self, b: int):
        if b & 0x80:                       # 状态字节
            return self._status_byte(b)
        return self._data_byte(b)

    def _status_byte(self, b: int):
        if b == 0xF7:                      # SysEx 结束（没在收 SysEx 则忽略）
            if self._sysex is not None:
                msg = self._emit_sysex()
                return msg
            return None
        if b == 0xF0:                      # SysEx 开始（未终止的旧 SysEx 丢弃）
            if self._sysex is not None:
                self.errors += 1
            self._sysex = []
            self._clear_channel()
            return None
        if b >= 0xF8:                      # 实时消息：忽略，不影响任何状态
            return None
        if b >= 0xF0:                      # 系统公共（F1-F6）：v1 忽略
            self._sysex = None             # 被 status 字节打断的 SysEx 算损坏
            self._clear_channel()
            return None
        # 0x80-0xEF：通道消息起始（正在收的 SysEx 被打断 → 丢弃计数）
        if self._sysex is not None:
            self.errors += 1
            self._sysex = None
        self._status = b
        self._data = []
        self._need = _DATA_BYTES[b >> 4]
        return None

    def _data_byte(self, b: int):
        if self._sysex is not None:
            self._sysex.append(b)
            if len(self._sysex) > _SYSEX_MAX:
                self.errors += 1
                self._sysex = None
            return None
        if self._need > 0:
            self._data.append(b)
            self._need -= 1
            if self._need == 0:
                return self._emit_channel(self._status, self._data)
            return None
        if self._status is not None:       # Running Status：无新状态字节续传
            self._data = [b]
            self._need = _DATA_BYTES[self._status >> 4] - 1
            if self._need == 0:
                return self._emit_channel(self._status, self._data)
            return None
        self.errors += 1                   # 无状态无消息：断流残留垃圾
        return None

    def _clear_channel(self) -> None:
        self._status = None
        self._need = 0
        self._data = []

    # ---- 组装 ----

    def _emit_channel(self, status: int, data: list):
        """状态字节 + 数据字节 → ParsedMessage（值域对齐 midi/parser.py）。"""
        ch = status & 0x0F
        if self._override is not None:
            ch = self._override
        kind = status & 0xF0
        d0 = data[0] & 0x7F
        d1 = data[1] & 0x7F if len(data) > 1 else None
        raw_hex = " ".join([f"{status:02X}"] + [f"{d:02X}" for d in data])

        if kind == 0x90:
            mtype, values = "note_on", {"note": d0, "velocity": d1}
            desc = f"音符开 ch{ch} note {d0} vel {d1}"
        elif kind == 0x80:
            mtype, values = "note_off", {"note": d0, "velocity": d1}
            desc = f"音符关 ch{ch} note {d0} vel {d1}"
        elif kind == 0xB0:
            mtype, values = "cc", {"control": d0, "value": d1}
            desc = f"CC ch{ch} CC{d0} = {d1}"
        elif kind == 0xE0:
            pitch = (d1 << 7 | d0) - 8192
            mtype, values = "pitch_bend", {"pitch": pitch}
            desc = f"弯音 ch{ch} pitch {pitch}"
        elif kind == 0xC0:
            mtype, values = "program_change", {"program": d0}
            desc = f"音色 ch{ch} program {d0}"
        elif kind == 0xD0:
            mtype, values = "aftertouch", {"value": d0}
            desc = f"通道触后 ch{ch} value {d0}"
        else:  # 0xA0
            mtype, values = "poly_aftertouch", {"note": d0, "value": d1}
            desc = f"复音触后 ch{ch} note {d0} value {d1}"

        # 消息已完整，清空收集区；Running Status（self._status）保留
        self._data = []
        self._need = 0
        return ParsedMessage(type=mtype, channel=ch, values=values,
                             raw_hex=raw_hex, description=desc, source="serial")

    def _emit_sysex(self):
        payload = self._sysex
        self._sysex = None
        raw_hex = " ".join(["F0"] + [f"{b:02X}" for b in payload] + ["F7"])
        return ParsedMessage(type="system", channel=None,
                             values={"raw_type": "sysex"}, raw_hex=raw_hex,
                             description="系统消息 sysex", source="serial")


def encode_parsed(parsed: ParsedMessage) -> bytes:
    """ParsedMessage → MIDI 字节流（MIDI→串口方向）。

    只编码通道消息；system/未知类型返回 b""（v1 透传协议不外发 SysEx）。
    值统一夹到 0-127，容忍手工构造的 ParsedMessage 缺键。
    """
    def _b(v, default=0) -> int:
        try:
            return max(0, min(127, int(v)))
        except (TypeError, ValueError):
            return default

    t = parsed.type
    ch = parsed.channel if parsed.channel is not None else 0
    v = parsed.values or {}
    if t == "note_on":
        return bytes([0x90 | ch, _b(v.get("note")), _b(v.get("velocity"))])
    if t == "note_off":
        return bytes([0x80 | ch, _b(v.get("note")), _b(v.get("velocity"))])
    if t == "cc":
        return bytes([0xB0 | ch, _b(v.get("control")), _b(v.get("value"))])
    if t == "pitch_bend":
        pitch = v.get("pitch", 0)
        try:
            pitch = int(pitch)
        except (TypeError, ValueError):
            pitch = 0
        pitch = max(-8192, min(8191, pitch)) + 8192
        return bytes([0xE0 | ch, pitch & 0x7F, (pitch >> 7) & 0x7F])
    if t == "program_change":
        return bytes([0xC0 | ch, _b(v.get("program"))])
    if t == "aftertouch":
        return bytes([0xD0 | ch, _b(v.get("value"))])
    if t == "poly_aftertouch":
        return bytes([0xA0 | ch, _b(v.get("note")), _b(v.get("value"))])
    return b""


# ======================================================================
# 协议 2：MFSP v1 帧协议
# ======================================================================

def _clamp(v, lo, hi, default=0) -> int:
    """容忍手工构造的 values 缺键/类型错，夹到 [lo, hi]。"""
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def crc8(data: bytes) -> int:
    """CRC-8：多项式 0x07，初值 0x00，MSB 先行，无反射。

    标准检查值：crc8(b"123456789") == 0xF4（与设备端 mfsp_arduino.h 同一实现）。
    """
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) if crc & 0x80 else (crc << 1)
            crc &= 0xFF
    return crc


def mfsp_frame(cmd: int, payload=b"") -> bytes:
    """组一帧：SYNC + LEN(覆盖 CMD+PAYLOAD) + CMD + PAYLOAD + CRC8。"""
    body = bytes([cmd & 0xFF]) + bytes(payload)
    return bytes([0x7E, len(body)]) + body + bytes([crc8(body)])


# 设备→宿主 CMD
_MFSP_NOTE_ON = 0x10
_MFSP_NOTE_OFF = 0x11
_MFSP_CC = 0x20
_MFSP_PITCHBEND = 0x30
_MFSP_HEARTBEAT = 0x40
_MFSP_ANALOG_IN = 0x50
_MFSP_DIGITAL_IN = 0x51
_MFSP_DEBUG = 0xF0
# 宿主→设备 CMD
MFSP_OUT_NOTE_ON = 0x80
MFSP_OUT_NOTE_OFF = 0x81
MFSP_OUT_CC = 0x82
MFSP_OUT_ANALOG = 0x83
MFSP_OUT_DIGITAL = 0x84
MFSP_PING = 0x8F

_MFSP_PAYLOAD_MAX = 32          # 单帧负载上限（LEN ≤ 33）
_MFSP_HEARTBEAT_TIMEOUT = 3.0   # 心跳超时秒数（UI 在线判定）


class MfspCodec:
    """MFSP 帧协议编解码器（设备↔宿主）。

    入向 feed() 与 PassthroughDecoder 同一接口；出向 encode_out() 把
    MIDI 消息编成标准 CMD 帧（note_on/off、cc）。虚拟端口帧的编解码见
    map_in / map_out_frames（映射表由 plugin.py 配置层提供）。

    附加状态（UI 用）：
      last_heartbeat  最近一次心跳的 time.monotonic()，None=从未收到
      device_uptime   设备上报的运行秒数
      debug_lines     debug 文本队列（maxlen=100，监视区消费）
    """

    def __init__(self, channel_override: int = None):
        self.errors = 0                # 坏 CRC / 非法长度 / 被打断的帧计数
        self.last_heartbeat = None
        self.device_uptime = None
        self.debug_lines = collections.deque(maxlen=100)
        self._override = channel_override
        self._state = "sync"           # sync / len / body
        self._buf = bytearray()        # 正在收集的帧（含 SYNC）
        self._need = 0                 # 还需收集的字节数（CMD+PAYLOAD+CRC）
        self._rescan = None            # 坏 CRC 时待重扫的字节（feed 循环消费）

    def reset(self) -> None:
        """清空半截帧状态（重连时调用；errors/心跳信息保留）。"""
        self._state = "sync"
        self._buf = bytearray()
        self._need = 0

    # ---- 入向：字节流 → 消息 ----

    def feed(self, chunk) -> list:
        """喂入任意切分的字节块，返回解析出的消息列表（1 字节粒度可复现）。"""
        out = []
        stream = collections.deque(chunk)
        while stream:
            b = stream.popleft() & 0xFF
            self._rescan = None
            msg = self._byte(b)
            if msg is not None:
                out.append(msg)
            if self._rescan:           # 坏 CRC：从帧内第一个字节起重扫
                stream.extendleft(reversed(self._rescan))
        return out

    def _byte(self, b: int):
        if self._state == "sync":
            if b == 0x7E:
                self._buf = bytearray([b])
                self._state = "len"
            return None
        if self._state == "len":
            if b == 0x7E:              # 连续 SYNC：重新对齐
                self._buf = bytearray([b])
                return None
            if not 1 <= b <= 1 + _MFSP_PAYLOAD_MAX:
                self.errors += 1       # 非法长度 → 丢弃重新扫描
                self._state = "sync"
                self._buf = bytearray()
                return None
            self._need = b + 1         # LEN 字节 + CRC 尾
            self._buf.append(b)
            self._state = "body"
            return None
        # body
        if b == 0x7E:                  # 帧中途出现 SYNC：本帧残废，重新对齐
            self.errors += 1
            self._buf = bytearray([b])
            self._state = "len"
            return None
        self._buf.append(b)
        if len(self._buf) < 2 + self._need:
            return None
        return self._frame_done()

    def _frame_done(self):
        frame = bytes(self._buf)
        self._state = "sync"
        self._buf = bytearray()
        if crc8(frame[2:-1]) != frame[-1]:
            self.errors += 1
            self._rescan = frame[1:]   # 帧内可能嵌着下一个真帧的 SYNC
            return None
        return self._dispatch(frame[2], frame[3:-1])

    def _dispatch(self, cmd: int, pl: bytes):
        raw_hex = " ".join(f"{x:02X}" for x in
                           ([0x7E, 1 + len(pl), cmd] + list(pl) +
                            [crc8(bytes([cmd]) + pl)]))
        ch = self._override if self._override is not None else 0
        if cmd == _MFSP_NOTE_ON and len(pl) == 2:
            note, vel = pl[0] & 0x7F, pl[1] & 0x7F
            return ParsedMessage(type="note_on", channel=ch,
                                 values={"note": note, "velocity": vel},
                                 raw_hex=raw_hex,
                                 description=f"音符开 ch{ch} note {note} vel {vel}",
                                 source="serial")
        if cmd == _MFSP_NOTE_OFF and len(pl) == 1:
            note = pl[0] & 0x7F
            return ParsedMessage(type="note_off", channel=ch,
                                 values={"note": note, "velocity": 0},
                                 raw_hex=raw_hex,
                                 description=f"音符关 ch{ch} note {note} vel 0",
                                 source="serial")
        if cmd == _MFSP_CC and len(pl) == 2:
            cc, val = pl[0] & 0x7F, pl[1] & 0x7F
            return ParsedMessage(type="cc", channel=ch,
                                 values={"control": cc, "value": val},
                                 raw_hex=raw_hex,
                                 description=f"CC ch{ch} CC{cc} = {val}",
                                 source="serial")
        if cmd == _MFSP_PITCHBEND and len(pl) == 2:
            pitch = ((pl[0] & 0x7F) | (pl[1] & 0x7F) << 7) - 8192
            return ParsedMessage(type="pitch_bend", channel=ch,
                                 values={"pitch": pitch},
                                 raw_hex=raw_hex,
                                 description=f"弯音 ch{ch} pitch {pitch}",
                                 source="serial")
        if cmd == _MFSP_HEARTBEAT and len(pl) == 2:
            self.last_heartbeat = time.monotonic()
            self.device_uptime = pl[0] | pl[1] << 8
            return None
        if cmd == _MFSP_ANALOG_IN and len(pl) == 3:
            port = pl[0] & 0x0F
            val = min(pl[1] | pl[2] << 8, 4095)
            return ParsedMessage(type="analog_in", channel=None,
                                 values={"port": port, "value": val},
                                 raw_hex=raw_hex,
                                 description=f"模拟端口 {port} = {val}",
                                 source="serial")
        if cmd == _MFSP_DIGITAL_IN and len(pl) == 2:
            port = pl[0] & 0x0F
            state = 1 if pl[1] else 0
            return ParsedMessage(type="digital_in", channel=None,
                                 values={"port": port, "state": state},
                                 raw_hex=raw_hex,
                                 description=f"数字端口 {port} = {state}",
                                 source="serial")
        if cmd == _MFSP_DEBUG:
            text = bytes(pl).decode("utf-8", errors="replace")
            self.debug_lines.append(text)
            return None
        self.errors += 1               # 未知 CMD / 长度不符
        return None

    # ---- 出向：MIDI 消息 → 帧（宿主→设备标准 CMD）----

    def encode_out(self, parsed: ParsedMessage) -> bytes:
        """ParsedMessage → MFSP 帧；虚拟端口/系统类消息返回 b""。

        映射表命中的消息不走这里（plugin.py 先经 map_out_frames 改道
        analog_out/digital_out 帧），未命中的 note/cc 在此直通。
        """
        v = parsed.values or {}
        t = parsed.type
        if t == "note_on":
            return mfsp_frame(MFSP_OUT_NOTE_ON,
                              [_clamp(v.get("note"), 0, 127),
                               _clamp(v.get("velocity"), 0, 127)])
        if t == "note_off":
            return mfsp_frame(MFSP_OUT_NOTE_OFF, [_clamp(v.get("note"), 0, 127)])
        if t == "cc":
            return mfsp_frame(MFSP_OUT_CC,
                              [_clamp(v.get("control"), 0, 127),
                               _clamp(v.get("value"), 0, 127)])
        return b""                      # pitch_bend 等协议未定义出向 CMD

    @staticmethod
    def encode_analog_out(port: int, value: int) -> bytes:
        """执行器帧：12bit 值（0-4095）小端。"""
        value = _clamp(value, 0, 4095)
        return mfsp_frame(MFSP_OUT_ANALOG,
                          [port & 0x0F, value & 0xFF, (value >> 8) & 0xFF])

    @staticmethod
    def encode_digital_out(port: int, state) -> bytes:
        return mfsp_frame(MFSP_OUT_DIGITAL, [port & 0x0F, 1 if state else 0])

    @staticmethod
    def encode_ping() -> bytes:
        return mfsp_frame(MFSP_PING)


# ---- 虚拟端口双向映射（映射表来自 plugin.py 配置，纯函数便于测试）----
#
# 入向行 map_in：  {"kind": "analog"|"digital", "port": 0-15,
#                   "target": "cc"|"note", "num": 0-127, "enabled": bool}
# 出向行 map_out： {"type": "cc"|"note", "num": 0-127,
#                   "target": "analog"|"digital", "port": 0-15, "enabled": bool}
#
# 语义：
#   analog→cc    连续流（每帧上报，值 12bit→7bit 右移 5）
#   analog→note  阈值边缘触发（≥2048 算按下）
#   digital→cc   边缘触发（变化才发，0/127）
#   digital→note 边缘触发（1=on 0=off）
#   cc→analog    值左移 5（0-127→0-4064）；cc→digital 阈值 ≥64
#   note→digital on/off；note→analog 力度作为电平（0-127→0-4064）
#
# 命中映射行的消息只走映射（改道虚拟端口帧）；未命中的走标准 CMD 直通。

def map_in(parsed: ParsedMessage, rows, channel: int, edges: dict) -> list:
    """虚拟端口事件 → MIDI 消息列表。edges 为边缘状态字典（plugin 持有）。"""
    out = []
    v = parsed.values or {}
    if parsed.type == "analog_in":
        port, val = v.get("port"), _clamp(v.get("value"), 0, 4095)
        for r in rows:
            if not r.get("enabled") or r.get("kind") != "analog":
                continue
            if _clamp(r.get("port"), 0, 15) != port:
                continue
            num = _clamp(r.get("num"), 0, 127)
            if r.get("target") == "cc":
                out.append(_mapped_msg("cc", channel,
                                       {"control": num, "value": val >> 5},
                                       parsed))
            else:                      # note：阈值边缘
                on = val >= 2048
                key = ("analog", port)
                prev = edges.get(key)
                edges[key] = on
                if on != prev:
                    out.append(_mapped_msg(
                        "note_on" if on else "note_off", channel,
                        {"note": num, "velocity": max(1, val >> 5) if on else 0},
                        parsed))
    elif parsed.type == "digital_in":
        port, state = v.get("port"), 1 if v.get("state") else 0
        for r in rows:
            if not r.get("enabled") or r.get("kind") != "digital":
                continue
            if _clamp(r.get("port"), 0, 15) != port:
                continue
            num = _clamp(r.get("num"), 0, 127)
            key = ("digital", port)
            prev = edges.get(key)
            edges[key] = state
            if state == prev:          # 数字口一律边缘触发
                continue
            if r.get("target") == "cc":
                out.append(_mapped_msg("cc", channel,
                                       {"control": num, "value": state * 127},
                                       parsed))
            else:
                out.append(_mapped_msg(
                    "note_on" if state else "note_off", channel,
                    {"note": num, "velocity": 127 if state else 0},
                    parsed))
    return out


def _mapped_msg(mtype: str, channel: int, values: dict, src: ParsedMessage):
    """映射产物：保留原始帧 hex，description 标注映射来源。"""
    if mtype == "note_on":
        desc = f"音符开 ch{channel} note {values['note']} vel {values['velocity']}"
    elif mtype == "note_off":
        desc = f"音符关 ch{channel} note {values['note']} vel 0"
    else:
        desc = f"CC ch{channel} CC{values['control']} = {values['value']}"
    return ParsedMessage(type=mtype, channel=channel, values=values,
                         raw_hex=src.raw_hex,
                         description=f"{desc} ← {src.description}",
                         source="serial")


def map_out_frames(parsed: ParsedMessage, rows) -> list:
    """MIDI 消息按出向映射表 → 虚拟端口帧列表（命中才产出，未命中为空）。"""
    frames = []
    t = parsed.type
    v = parsed.values or {}
    for r in rows:
        if not r.get("enabled"):
            continue
        num = _clamp(r.get("num"), 0, 127)
        port = _clamp(r.get("port"), 0, 15)
        target = r.get("target")
        if r.get("type") == "cc" and t == "cc":
            if _clamp(v.get("control"), 0, 127) != num:
                continue
            val = _clamp(v.get("value"), 0, 127)
            if target == "analog":
                frames.append(MfspCodec.encode_analog_out(port, val << 5))
            else:
                frames.append(MfspCodec.encode_digital_out(port, val >= 64))
        elif r.get("type") == "note" and t in ("note_on", "note_off"):
            if _clamp(v.get("note"), 0, 127) != num:
                continue
            on = t == "note_on" and _clamp(v.get("velocity"), 0, 127) > 0
            if target == "digital":
                frames.append(MfspCodec.encode_digital_out(port, on))
            else:                      # note→analog：力度作电平
                vel = _clamp(v.get("velocity"), 0, 127) if on else 0
                frames.append(MfspCodec.encode_analog_out(port, vel << 5))
    return frames
