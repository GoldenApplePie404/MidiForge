"""Practice2 传输引擎 — 纯逻辑、无 Qt，可单测。

时间模型（逆向 pianodrift.com/app.js 得到，语义一致）：

    position = anchor_song + (now - anchor_wall) × speed      # 锚点映射
    等待模式:  position = min(position, 当前未完成音符组的 start)
    播放终点:  position = min(position, 循环开启 ? B 点 : 曲长)

其余核心语义：
    - 分组：时间上重叠的练习音符为一组（和弦可逐个按），组内全中才放行
    - 发声：练习声部仅在"跟随/试听"模式自动出声，伴奏声部始终出声
    - 判定：按键在 ±100ms 窗口内命中练习音符；音符越线时按住也算中
    - 统计：正确 = 按中音符数，漏掉 = 越线未按数，准确率 = 成功按键/总按键
    - 延音：CC64 按下时松键不立即止音，延迟 tail 秒（墙钟时间）
    - 循环：到 B 回 A，重置调度指针与统计计数（累计不清零）
    - 预备：起播前回退两拍（2×60/bpm 秒），每拍发一次 click 事件

引擎不直接碰音频/Qt：通过回调 cb_sound/cb_stop/cb_click/cb_finished 与宿主交互，
线程安全——tick/seek 在主线程，note_on/note_off 来自 MIDI 事件线程，内部加锁。
"""

import bisect
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

# ---------------- 常量 ----------------

NOTE_MIN = 21      # A0
NOTE_MAX = 108     # C8
C4 = 60
DRUM_CHANNEL = 9   # 0 基索引；MIDI 习惯称"通道 10"，鼓轨跳过

MODE_FOLLOW = "follow"    # 跟随演奏：曲子照常响，跟着弹
MODE_WAIT = "wait"        # 等待按对：弹对才继续
MODE_LISTEN = "listen"    # 试听所选声部：纯听，不判定

LOOKAHEAD = 0.20     # 调度提前量（墙钟秒）
HIT_WINDOW = 0.10    # 按键判定窗口 ±100ms
SPEED_MIN, SPEED_MAX = 0.25, 1.5


# ---------------- 数据模型 ----------------

@dataclass
class SongNote:
    id: int
    pitch: int
    start: float
    end: float
    velocity: int = 80
    channel: int = 0
    color_idx: int = 0     # 按通道分配的调色板索引（画布用）


@dataclass
class TrackInfo:
    name: str
    count: int

@dataclass
class Song:
    notes: List[SongNote] = field(default_factory=list)   # 按 start 升序
    tracks: List[TrackInfo] = field(default_factory=list)
    bpm: float = 120.0
    duration: float = 0.0


# ---------------- MIDI 文件 → Song ----------------

def load_song(path: str) -> Song:
    """解析 .mid → Song。复用宿主 core/midi_file 服务做事件解析，
    另外用 mido 直接读一次文件首 tempo（load() 会丢掉 set_tempo）。"""
    from core.midi_file import _MidiFileService

    events = _MidiFileService().load(path)   # 已按 tick_time 排序
    bpm = _read_bpm(path)

    # 按 (channel, pitch) 配对 note_on / note_off；跳过鼓通道
    stacks: Dict[Tuple[int, int], List] = {}
    notes: List[SongNote] = []
    nid = 0
    for ev in events:
        if ev.type not in ("note_on", "note_off") or ev.channel is None:
            continue
        ch = int(ev.channel)
        if ch == DRUM_CHANNEL:
            continue
        pitch = ev.values.get("note")
        if pitch is None:
            continue
        t = getattr(ev, "tick_time", 0.0)
        vel = ev.values.get("velocity", 0)
        key = (ch, pitch)
        if ev.type == "note_on" and vel > 0:
            stacks.setdefault(key, []).append((t, vel))
        else:
            if stacks.get(key):
                start, v = stacks[key].pop(0)
                notes.append(SongNote(id=nid, pitch=pitch, start=start,
                                      end=max(t, start + 0.05), velocity=v,
                                      channel=ch))
                nid += 1

    notes.sort(key=lambda n: (n.start, n.pitch))

    # 轨道信息 = 通道（不含鼓）；名字用通道号，MIDI 文件里同名轨很常见
    counts: Dict[int, int] = {}
    for n in notes:
        counts[n.channel] = counts.get(n.channel, 0) + 1
    palette = _palette_indices(sorted(counts))
    for n in notes:
        n.color_idx = palette[n.channel]
    tracks = [TrackInfo(f"通道 {ch + 1}", counts[ch]) for ch in sorted(counts)]

    duration = max((n.end for n in notes), default=0.0)
    return Song(notes=notes, tracks=tracks, bpm=bpm, duration=duration)


def _read_bpm(path: str) -> float:
    try:
        import mido
        mid = mido.MidiFile(path)
        for track in mid.tracks:
            for msg in track:
                if msg.type == "set_tempo":
                    return float(min(400.0, max(20.0, mido.tempo2bpm(msg.tempo))))
    except Exception:
        pass
    return 120.0


def _palette_indices(channels: List[int]) -> Dict[int, int]:
    return {ch: i for i, ch in enumerate(channels)}


# ---------------- 引擎 ----------------

class DriftEngine:
    """传输/判定/调度引擎。UI 与音频全部通过回调和只读状态交互。"""

    def __init__(self):
        self.song: Optional[Song] = None
        self.mode: str = MODE_FOLLOW
        self.part: str = "all"           # all / high / low / track:{ch}
        self.speed: float = 1.0
        self.count_in: bool = True
        self.pedal_tail: float = 0.6     # 延音尾（秒）
        self.loop_enabled: bool = False
        self.loop_a: float = 0.0
        self.loop_b: float = 0.0

        # ---- 回调（宿主注入）----
        self.cb_sound: Callable[[SongNote, bool], object] = lambda *a: None
        self.cb_stop: Callable[[object], None] = lambda *a: None
        self.cb_click: Callable[[int], None] = lambda *a: None   # 参数=剩余拍数
        self.cb_finished: Callable[[], None] = lambda: None
        self.cb_pedal: Callable[[bool], None] = lambda *a: None  # 参数=踏板是否按下

        # ---- 内部状态 ----
        self._position: float = 0.0
        self._playing: bool = False
        self._anchor_song: float = 0.0
        self._anchor_wall: float = 0.0
        self._lock = threading.RLock()

        self._practice_ids: Set[int] = set()
        self._practice_sorted: List[SongNote] = []      # 按 start 升序，漏按扫描用
        self._track_channels: List[int] = []            # 排序后的通道，track:N 匹配用
        self._groups: List[List[SongNote]] = []
        self._group_starts: List[float] = []
        self._group_cursor: int = 0
        self._audio_cursor: int = 0
        self._miss_cursor: int = 0

        self.hit: Set[int] = set()
        self.missed: int = 0
        self.attempts: int = 0
        self.successful: int = 0
        self.loop_count: int = 0

        self.pressed: Set[int] = set()                  # 用户当前按住的音高
        self._recent_press: Dict[int, float] = {}       # 音高 → 最近按键时的曲内时间
        self.pedal_down: bool = False                   # CC64 物理踏板
        self.shift_pedal: bool = False                  # Shift 临时踏板

        self._voices: Dict[int, dict] = {}              # note_id → {handle, stop_wall}
        self._sounded: Set[int] = set()                 # 本轮已调度过的音符
        self._last_click_beat: int = 0

        # 画布用的视觉状态（帧内只读）
        self.hit_flash: Dict[int, float] = {}           # pitch → 闪灯截止(墙钟)
        self.miss_flash: Dict[int, float] = {}          # pitch → 闪灯截止(墙钟)

    # ---- 声部过滤 / 分组 ----

    def _is_practice(self, n: SongNote) -> bool:
        p = self.part
        if p == "all":
            return True
        if p == "high":
            return n.pitch >= C4
        if p == "low":
            return n.pitch < C4
        if p.startswith("track:"):
            # track:N = 歌曲轨道列表的第 N 项（按通道排序），不是裸通道号
            try:
                idx = int(p.split(":", 1)[1])
            except ValueError:
                return True
            chans = self._track_channels
            return idx < len(chans) and n.channel == chans[idx]
        return True

    def _rebuild_practice(self) -> None:
        """part/mode 变化后重建练习声部与分组，统计清零。"""
        self.hit.clear()
        self.missed = self.attempts = self.successful = 0
        self._miss_cursor = 0
        self._groups = []
        self._group_starts = []
        if not self.song:
            return
        self._track_channels = sorted({m.channel for m in self.song.notes})
        practice = [n for n in self.song.notes if self._is_practice(n)]
        self._practice_ids = {n.id for n in practice}
        self._practice_sorted = practice
        # 分组：与当前组时间上重叠的音符归同组（和弦可逐个按）
        cur: List[SongNote] = []
        cur_end = -1.0
        for n in practice:
            if cur and n.start >= cur_end:
                self._groups.append(cur)
                cur = []
                cur_end = -1.0
            cur.append(n)
            cur_end = max(cur_end, n.end)
        if cur:
            self._groups.append(cur)
        self._group_starts = [g[0].start for g in self._groups]
        pos = max(0.0, self._position)
        self._group_cursor = max(0, bisect.bisect_right(self._group_starts, pos) - 1)
        self._advance_groups()

    def _advance_groups(self) -> None:
        while (self._group_cursor < len(self._groups)
               and all(n.id in self.hit for n in self._groups[self._group_cursor])):
            self._group_cursor += 1

    def expected_pitches(self) -> Set[int]:
        """画布高亮用：等待模式下当前组还没按对的音高。"""
        with self._lock:
            if self.mode != MODE_WAIT or not self.song:
                return set()
            if self._group_cursor >= len(self._groups):
                return set()
            return {n.pitch for n in self._groups[self._group_cursor]
                    if n.id not in self.hit}

    # ---- 歌曲与设置 ----

    def set_song(self, song: Song) -> None:
        with self._lock:
            self._stop_all_voices()
            self.song = song
            self._position = 0.0
            self.loop_count = 0
            self._sounded.clear()
            self._rebuild_practice()

    def set_part(self, part: str) -> None:
        with self._lock:
            if part == self.part:
                return
            self.part = part
            self._rebuild_practice()

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def set_speed(self, speed: float) -> None:
        """变速不打断播放：重新锚定，保证 position 连续。"""
        with self._lock:
            if self._playing:
                self._position = self._raw_position(time.monotonic())
                self._anchor_wall = time.monotonic()
                self._anchor_song = self._position
            self.speed = max(SPEED_MIN, min(SPEED_MAX, speed))

    def set_loop(self, enabled: bool, a: float, b: float) -> None:
        with self._lock:
            self.loop_enabled = enabled
            self.loop_a = max(0.0, a)
            self.loop_b = max(self.loop_a + 0.05, b)

    def set_pedal_tail(self, tail: float) -> None:
        self.pedal_tail = max(0.0, min(3.0, tail))

    def set_pedal(self, down: bool) -> None:
        """CC64 物理踏板。"""
        self.pedal_down = bool(down)
        if self.cb_pedal:
            self.cb_pedal(self.pedal_down)

    def set_shift_pedal(self, down: bool) -> None:
        self.shift_pedal = bool(down)
        if self.cb_pedal:
            self.cb_pedal(self.shift_pedal)

    # ---- 传输 ----

    def _end_position(self) -> float:
        if not self.song:
            return 0.0
        return min(self.loop_b, self.song.duration) if self.loop_enabled else self.song.duration

    def _start_position(self) -> float:
        return self.loop_a if self.loop_enabled else 0.0

    def _raw_position(self, now: float) -> float:
        return self._anchor_song + (now - self._anchor_wall) * self.speed

    def current_position(self) -> float:
        with self._lock:
            if not self._playing or not self.song:
                return self._position
            pos = self._raw_position(time.monotonic())
            if self.mode == MODE_WAIT and self._group_cursor < len(self._groups):
                pos = min(pos, self._groups[self._group_cursor][0].start)
            return min(pos, self._end_position())

    @property
    def playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        with self._lock:
            if not self.song or self._playing:
                return
            start = self._start_position()
            if self._position >= self.song.duration - 0.01:
                self._position = start        # 播完了 → 从头再来
            if self.count_in:
                self._position = start - 2 * 60.0 / max(self.song.bpm, 20.0)
            elif self._position < start:
                self._position = start
            self._reseed(self._position)
            self._anchor_song = self._position
            self._anchor_wall = time.monotonic()
            self._playing = True
            self._last_click_beat = int(self._position // self._beat_len()) - 1

    def pause(self) -> None:
        with self._lock:
            if self._playing:
                self._position = self._raw_position(time.monotonic())
            self._playing = False
            self._stop_all_voices()

    def seek(self, pos: float, count_in: bool = False) -> None:
        with self._lock:
            if not self.song:
                return
            was_playing = self._playing
            self._stop_all_voices()
            pos = max(-2 * 60.0 / max(self.song.bpm, 20.0),
                      min(pos, self.song.duration))
            if count_in and was_playing:
                pos -= 2 * 60.0 / max(self.song.bpm, 20.0)
            self._position = pos
            self._sounded.clear()
            self._reseed(pos)
            self._rebuild_practice_cursor_only()
            if was_playing:
                self._anchor_song = self._position
                self._anchor_wall = time.monotonic()
            else:
                self._playing = False

    def _rebuild_practice_cursor_only(self) -> None:
        """seek 后只重定位组游标，不动统计。"""
        if not self._groups:
            return
        pos = max(0.0, self._position)
        self._group_cursor = max(0, bisect.bisect_right(self._group_starts, pos) - 1)
        self._advance_groups()

    def restart(self) -> None:
        with self._lock:
            self.hit.clear()
            self.missed = self.attempts = self.successful = 0
            self.loop_count = 0
            self._miss_cursor = 0
            self._playing = False
            self._stop_all_voices()
            self._position = self._start_position()
            self._sounded.clear()
            self._rebuild_practice_cursor_only()

    def _reseed(self, pos: float) -> None:
        """(重)起播时：越过当前位置的长音立即补声，调度指针定位到 pos。"""
        self._audio_cursor = self._lower_bound_note(pos)
        if not self.song:
            return
        now = time.monotonic()
        for n in self.song.notes:
            if n.start < pos < n.end:
                self._schedule(n, now, pos)

    def _lower_bound_note(self, pos: float) -> int:
        if not self.song:
            return 0
        starts = [n.start for n in self.song.notes]
        return bisect.bisect_left(starts, pos - 0.001)

    def _beat_len(self) -> float:
        return 60.0 / max(self.song.bpm, 20.0) if self.song else 0.5

    # ---- 主循环 tick（主线程每帧调用）----

    def tick(self, now: float) -> None:
        """推进传输、调度发声、判定漏按、处理止音与循环。"""
        with self._lock:
            if not self.song or not self._playing:
                return
            pos = self._raw_position(now)

            # --- 循环回卷（每一遍作为独立的练习轮次，统计重新计）---
            if self.loop_enabled and pos >= self._end_position():
                self.loop_count += 1
                pos = self._start_position()
                self._stop_all_voices()
                self._sounded.clear()
                self.hit.clear()
                self.missed = self.attempts = self.successful = 0
                self._miss_cursor = 0
                self._reseed(pos)
                self._rebuild_practice_cursor_only()
                self._anchor_song = pos
                self._anchor_wall = now

            # --- 等待模式：卡在当前未完成组的起点 ---
            if self.mode == MODE_WAIT and self._group_cursor < len(self._groups):
                pos = min(pos, self._groups[self._group_cursor][0].start)

            end = self._end_position()
            if pos >= end:
                pos = end
                self._position = end
                self._playing = False
                self._stop_all_voices()
                self.cb_finished()
                return
            self._position = pos
            self._anchor_song = pos
            self._anchor_wall = now

            # --- 预备拍 click ---
            if pos < 0:
                beat = int(pos // self._beat_len())
                if beat > self._last_click_beat:
                    self._last_click_beat = beat
                    self.cb_click(int(round(-pos / self._beat_len())))

            # --- 调度发声 ---
            horizon = pos + LOOKAHEAD * self.speed
            notes = self.song.notes
            i = self._audio_cursor
            while i < len(notes) and notes[i].start <= horizon:
                n = notes[i]
                if n.end > pos and n.id not in self._sounded:
                    self._schedule(n, now, pos)
                i += 1
            self._audio_cursor = i

            # --- 止音 / 延音 ---
            for nid in list(self._voices):
                v = self._voices[nid]
                n = v["note"]
                if v["stop_wall"] is None and pos >= n.end:
                    if self._pedal_active():
                        v["stop_wall"] = now + self.pedal_tail   # 延音：松键不立即止
                    else:
                        self.cb_stop(v["handle"])
                        del self._voices[nid]
                if v.get("stop_wall") is not None and now >= v["stop_wall"]:
                    self.cb_stop(v["handle"])
                    del self._voices[nid]

            # --- 漏按判定（等待模式 position 被卡住，不会误判）---
            if self.mode != MODE_LISTEN:
                while self._miss_cursor < len(self._practice_sorted):
                    n = self._practice_sorted[self._miss_cursor]
                    if n.id in self.hit:
                        self._miss_cursor += 1
                        continue
                    if n.start + HIT_WINDOW < pos:
                        if n.pitch in self.pressed:
                            # 越线时正按着 → 命中（长按提前压键的玩法）
                            self.hit.add(n.id)
                            self.successful += 1
                            self.hit_flash[n.pitch] = now + 0.4
                        else:
                            self.missed += 1
                            self.miss_flash[n.pitch] = now + 0.35
                        self._miss_cursor += 1
                    else:
                        break

    def _pedal_active(self) -> bool:
        return self.pedal_down or self.shift_pedal

    def _schedule(self, n: SongNote, now: float, pos: float) -> None:
        """把一个音符交给宿主发声（是否发声由模式决定，与 Practice2 一致）。"""
        self._sounded.add(n.id)
        is_practice = n.id in self._practice_ids
        if is_practice:
            if self.mode == MODE_WAIT:
                return        # 等待模式：练习声部留给用户弹
        if n.end <= pos:
            return
        handle = self.cb_sound(n, is_practice)
        if handle is None:
            return
        self._voices[n.id] = {"handle": handle, "note": n, "stop_wall": None}

    def _stop_all_voices(self) -> None:
        for v in self._voices.values():
            self.cb_stop(v["handle"])
        self._voices.clear()

    def stop_all_sound(self) -> None:
        """Esc 一类的"全部静音"入口。"""
        with self._lock:
            self._stop_all_voices()

    # ---- 用户输入（MIDI 线程调用）----

    def user_note_on(self, pitch: int, velocity: int = 95) -> Optional[SongNote]:
        """用户按键：更新按压状态 + 判定。返回命中的音符（未中返回 None）。"""
        with self._lock:
            now = time.monotonic()
            self.pressed.add(pitch)
            pos = self.current_position()
            self._recent_press[pitch] = pos
            if self.mode == MODE_LISTEN or not self.song:
                return None
            self.attempts += 1

            target = self._find_target(pitch, pos)
            if target is not None:
                self.hit.add(target.id)
                self.successful += 1
                self.hit_flash[target.pitch] = now + 0.4
                self._advance_groups()
            return target

    def _find_target(self, pitch: int, pos: float) -> Optional[SongNote]:
        """找这次按键该命中的音符：优先当前等待组，其次时间窗内的未命中音符。"""
        if self.mode == MODE_WAIT and self._group_cursor < len(self._groups):
            pending = [n for n in self._groups[self._group_cursor]
                       if n.id not in self.hit and n.pitch == pitch]
            if pending:
                return min(pending, key=lambda n: n.start)
            return None
        # 跟随模式：窗口内最早的同音高未命中音符
        best = None
        for n in self.song.notes:
            if n.id not in self._practice_ids or n.id in self.hit:
                continue
            if n.pitch != pitch:
                continue
            if n.start - HIT_WINDOW <= pos <= n.start + HIT_WINDOW:
                if best is None or n.start < best.start:
                    best = n
        return best

    def user_note_off(self, pitch: int) -> None:
        with self._lock:
            self.pressed.discard(pitch)

    # ---- 统计 ----

    def stats(self) -> Tuple[int, int, str]:
        """返回 (正确数, 漏掉数, 准确率文本)。"""
        acc = f"{round(self.successful / self.attempts * 100)}%" if self.attempts else "—"
        return len(self.hit), self.missed, acc
