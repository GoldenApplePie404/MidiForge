"""MIDI 文件 I/O — 用 mido 读写 .mid 文件，转换为 ParsedMessage。

=== 插件使用 ===
    mf = app.midi_file
    tracks = mf.load("loop.mid")          # → [ParsedMessage, ...]（带时间戳）
    mf.save(tracks, "recording.mid")       # 保存为 .mid
    mf.play(tracks, send_midi_fn=app.send_midi, bpm=120)  # 按原时间播放
"""

import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from midi.parser import ParsedMessage

try:
    import mido
    _MIDO_AVAILABLE = True
except ImportError:
    _MIDO_AVAILABLE = False


class _MidiFileService:
    """MIDI 文件读写服务。"""

    def __init__(self):
        self._available = _MIDO_AVAILABLE

    @property
    def available(self) -> bool:
        return self._available

    # ---- 读 .mid → ParsedMessage 列表 ----

    def load(self, path: str) -> List[ParsedMessage]:
        """读一个 .mid 文件，返回 ParsedMessage 列表（每条带 tick_time 属性）。

        返回的消息带一个额外属性:
            .tick_time    float   从文件开头算起的秒数
        """
        if not self._available:
            raise RuntimeError("mido 未安装")

        mid = mido.MidiFile(path)
        events: List[ParsedMessage] = []

        # 先算 tempo 变化（影响 tick → 秒转换）
        tempos = self._collect_tempos(mid)

        for track_idx, track in enumerate(mid.tracks):
            abs_tick = 0
            for msg in track:
                abs_tick += msg.time
                # tempo event 跳过（已经收集）
                if msg.type == "set_tempo":
                    continue

                seconds = self._tick_to_second(abs_tick, mid.ticks_per_beat, tempos)
                parsed = self._mido_to_parsed(msg)
                if parsed is not None:
                    parsed.tick_time = seconds  # 扩展属性
                    events.append(parsed)

        events.sort(key=lambda m: getattr(m, "tick_time", 0))
        return events

    # ---- ParsedMessage 列表 → 写 .mid ----

    def save(self, events: List[ParsedMessage], path: str,
             tempo: float = 120.0, resolution: int = 480) -> None:
        """把 ParsedMessage 列表保存为 .mid 文件。

        events 应该带 tick_time 属性（秒），否则按输入顺序均匀排列。
        """
        if not self._available:
            raise RuntimeError("mido 未安装")

        mid = mido.MidiFile(ticks_per_beat=resolution)
        track = mido.MidiTrack()
        mid.tracks.append(track)

        # Tempo meta
        track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo), time=0))

        prev_second = 0.0
        for ev in events:
            cur_second = getattr(ev, "tick_time", prev_second + 0.05)
            delta = cur_second - prev_second
            if delta < 0:
                delta = 0
            prev_second = cur_second

            msg = self._parsed_to_mido(ev)
            if msg is None:
                continue
            msg.time = int(delta * resolution * tempo / 60.0)
            track.append(msg)

        mid.save(path)

    # ---- 按时间发送（给 Looper 等用）----

    def play(self, events: List[ParsedMessage],
             send_midi_fn: Callable[[str, int, ...], None],
             speed: float = 1.0) -> List[float]:
        """同步播放（阻塞）。返回每条消息的实际发送时间戳列表。

        插件应该在后台线程调，避免阻塞 UI。
        """
        sent_times = []
        if not events:
            return sent_times

        base = events[0].tick_time if hasattr(events[0], "tick_time") else 0.0
        wall_start = time.monotonic()

        for ev in events:
            t = getattr(ev, "tick_time", 0) - base
            target = wall_start + t / speed
            # 等到目标时间
            while time.monotonic() < target:
                time.sleep(0.001)

            # 发送
            try:
                self._dispatch(send_midi_fn, ev)
                sent_times.append(time.monotonic() - wall_start)
            except Exception:
                pass

        return sent_times

    # ---- 内部转换 ----

    def _collect_tempos(self, mid) -> List[Tuple[int, float]]:
        """收集 (abs_tick, bpm) 列表，供 tick→second 用。"""
        tempos = [(0, 120.0)]
        for track in mid.tracks:
            abs_tick = 0
            for msg in track:
                abs_tick += msg.time
                if msg.type == "set_tempo":
                    bpm = mido.tempo2bpm(msg.tempo)
                    # 用最大的覆盖（简单策略）
                    if tempos[-1][0] == abs_tick:
                        tempos[-1] = (abs_tick, bpm)
                    elif abs_tick > tempos[-1][0]:
                        tempos.append((abs_tick, bpm))
        return tempos

    def _tick_to_second(self, abs_tick: int, ticks_per_beat: int,
                        tempos: List[Tuple[int, float]]) -> float:
        seconds = 0.0
        prev_tick = 0
        prev_bpm = 120.0
        for tick, bpm in tempos:
            if tick >= abs_tick:
                break
            if tick > prev_tick:
                beats = (tick - prev_tick) / ticks_per_beat
                seconds += beats * 60.0 / prev_bpm
                prev_tick = tick
            prev_bpm = bpm
        beats = (abs_tick - prev_tick) / ticks_per_beat
        seconds += beats * 60.0 / prev_bpm
        return seconds

    def _mido_to_parsed(self, msg) -> Optional[ParsedMessage]:
        type_map = {
            "note_on": "note_on",
            "note_off": "note_off",
            "control_change": "cc",
            "program_change": "program_change",
            "pitchwheel": "pitch_bend",
            "polytouch": "poly_aftertouch",
            "aftertouch": "channel_aftertouch",
        }
        t = type_map.get(msg.type)
        if t is None:
            return None

        values = {}
        if t in ("note_on", "note_off"):
            values["note"] = msg.note
            values["velocity"] = msg.velocity
        elif t == "cc":
            values["control"] = msg.control
            values["value"] = msg.value
        elif t == "program_change":
            values["program"] = msg.program
        elif t == "pitch_bend":
            values["pitch"] = msg.pitch
        elif t == "poly_aftertouch":
            values["note"] = msg.note
            values["value"] = msg.value
        elif t == "channel_aftertouch":
            values["value"] = msg.value

        return ParsedMessage(type=t, channel=getattr(msg, "channel", None),
                             values=values, raw_hex="")

    def _parsed_to_mido(self, parsed: ParsedMessage):
        t = parsed.type
        ch = parsed.channel
        v = parsed.values
        if t in ("note_on", "note_off"):
            return mido.Message(t, channel=ch, note=v.get("note", 0),
                                velocity=v.get("velocity", 64))
        if t == "cc":
            return mido.Message("control_change", channel=ch,
                                control=v.get("control", 0), value=v.get("value", 0))
        if t == "program_change":
            return mido.Message("program_change", channel=ch, program=v.get("program", 0))
        if t in ("pitch_bend", "pitchwheel"):
            return mido.Message("pitchwheel", channel=ch, pitch=v.get("pitch", 0))
        if t in ("aftertouch", "channel_aftertouch"):
            return mido.Message("aftertouch", channel=ch, value=v.get("value", 0))
        if t in ("poly_aftertouch", "polytouch"):
            return mido.Message("polytouch", channel=ch, note=v.get("note", 0),
                                value=v.get("value", 0))
        return None

    def _dispatch(self, send_midi_fn, parsed: ParsedMessage):
        t = parsed.type
        ch = parsed.channel or 0
        v = parsed.values
        if t in ("note_on", "note_off"):
            send_midi_fn(t, channel=ch, note=v.get("note", 0),
                         velocity=v.get("velocity", 64))
        elif t == "cc":
            send_midi_fn("cc", channel=ch, control=v.get("control", 0),
                         value=v.get("value", 0))
        elif t == "program_change":
            send_midi_fn("program_change", channel=ch, program=v.get("program", 0))
        elif t in ("pitch_bend", "pitchwheel"):
            send_midi_fn("pitch_bend", channel=ch, pitch=v.get("pitch", 0))
        elif t in ("aftertouch", "channel_aftertouch"):
            send_midi_fn("channel_aftertouch", channel=ch, value=v.get("value", 0))
        elif t in ("poly_aftertouch", "polytouch"):
            send_midi_fn("poly_aftertouch", channel=ch, note=v.get("note", 0),
                         value=v.get("value", 0))
