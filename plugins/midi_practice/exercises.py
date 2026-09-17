"""练习源：统一 ExerciseSource 接口 + 三种实现。"""
import random
from pathlib import Path
from typing import List, Optional, Tuple

from midi_practice.engine import Exercise, NoteTarget


class BuiltInScales:
    """内置题库。硬编码若干预设练习。"""

    ITEMS: List[Tuple[str, str, str]] = [
        ("c_major_ascending", "C 大调音阶 ascending", "piano"),
        ("a_minor_blues", "A 小调五声 blues", "piano"),
        ("i_iv_v_chords", "I-IV-V 和弦进行 (C-F-G)", "piano"),
        ("pad_cc_identification", "Pad CC 识别 8 个", "pad"),
        ("eighth_note_rhythm", "八分音符节奏", "both"),
    ]

    def list_available(self) -> List[Tuple[str, str, str]]:
        return list(self.ITEMS)

    def load(self, key: str, tempo: Optional[float] = None) -> Exercise:
        t = tempo if tempo else 120.0
        beat = 60.0 / t

        if key == "c_major_ascending":
            notes_midi = [60, 62, 64, 65, 67, 69, 71, 72]
            targets = [NoteTarget(time=i * beat, note=n, velocity=100)
                       for i, n in enumerate(notes_midi)]
            return Exercise(name="C 大调音阶 ascending", tempo=t,
                            scope="piano", notes=targets)

        if key == "a_minor_blues":
            penta = [69, 60, 62, 64, 67]
            notes_seq = penta + penta[:3]
            targets = [NoteTarget(time=i * beat, note=n, velocity=100)
                       for i, n in enumerate(notes_seq)]
            return Exercise(name="A 小调五声 blues", tempo=t,
                            scope="piano", notes=targets)

        if key == "i_iv_v_chords":
            chords = [(60, 64, 67), (65, 69, 72), (67, 71, 74), (60, 64, 67)]
            targets = []
            for i, chord in enumerate(chords):
                for j, n in enumerate(chord):
                    targets.append(NoteTarget(
                        time=i * beat * 2 + j * 0.01,
                        note=n, velocity=100))
            return Exercise(name="I-IV-V 和弦进行", tempo=t,
                            scope="piano", notes=targets)

        if key == "pad_cc_identification":
            ccs = [102, 103, 104, 105, 106, 107, 108, 109]
            random.seed(42)
            random.shuffle(ccs)
            targets = [NoteTarget(time=i * beat * 2, cc=c, velocity=127)
                       for i, c in enumerate(ccs)]
            return Exercise(name="Pad CC 识别", tempo=t,
                            scope="pad", notes=targets)

        if key == "eighth_note_rhythm":
            targets = []
            for i in range(16):
                ti = i * beat / 2
                targets.append(NoteTarget(time=ti, note=60, velocity=100))
                targets.append(NoteTarget(time=ti, cc=102, velocity=127))
            return Exercise(name="八分音符节奏", tempo=t,
                            scope="both", notes=targets)

        raise ValueError(f"未知内置练习: {key}")


class UserRecording:
    """用户录制消息列表 → Exercise 的转换。"""

    @staticmethod
    def from_messages(messages: list, bpm: float = 120.0,
                      name: str = "用户录制") -> Exercise:
        """messages 是 [(timestamp_float, ParsedMessage), ...] 按时间排序的列表。"""
        if not messages:
            return Exercise(name=name, tempo=bpm, notes=[])

        base_time = messages[0][0]
        targets = []
        for ts, msg in messages:
            t = ts - base_time
            if msg.type == "note_on" and msg.values.get("velocity", 0) > 0:
                targets.append(NoteTarget(
                    time=t, note=msg.values.get("note", 60),
                    velocity=msg.values.get("velocity", 100)))
            elif msg.type == "cc" and msg.values.get("value", 0) > 0:
                targets.append(NoteTarget(
                    time=t, cc=msg.values.get("control", 0),
                    velocity=msg.values.get("value", 127)))

        has_piano = any(t.note is not None for t in targets)
        has_pad = any(t.cc is not None for t in targets)
        if has_piano and has_pad:
            scope = "both"
        elif has_pad:
            scope = "pad"
        else:
            scope = "piano"

        return Exercise(name=name, tempo=bpm, scope=scope, notes=targets)


class MidiFileImport:
    """从 .mid 文件导入。"""

    def load(self, path: str, midi_file_service=None) -> Exercise:
        """用 app.midi_file.load() 读 ParsedMessage 列表 → Exercise。"""
        if midi_file_service is None:
            # 独立路径：用 mido 直接读（测试用）
            import mido
            mid = mido.MidiFile(path)
            # mido 文件直接转 ParsedMessage 比较复杂——先返回空
            # 完整实现需要 tick→second 转换（api.py 里 _MidiFileService 已经做了）
            return Exercise(name=f"MIDI 导入: {Path(path).name}", tempo=120.0,
                            scope="both", notes=[])

        events = midi_file_service.load(path)
        targets = []
        for ev in events:
            t = getattr(ev, "tick_time", 0)
            if ev.type == "note_on" and ev.values.get("velocity", 0) > 0:
                targets.append(NoteTarget(
                    time=t, note=ev.values.get("note", 60),
                    velocity=ev.values.get("velocity", 100)))
            elif ev.type == "cc":
                targets.append(NoteTarget(
                    time=t, cc=ev.values.get("control", 0),
                    velocity=ev.values.get("value", 127)))

        return Exercise(name=f"MIDI 导入: {Path(path).name}", tempo=120.0,
                        scope="both", notes=targets)
