"""Practice2 引擎与画布测试。

时间全部走假时钟（monkeypatch time.monotonic），tick 用显式时间推进，
保证判定窗口 / 循环回卷 / 预备拍等用例完全确定。
"""
import sys
import time
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import pytest

from plugins.practice2._engine import (
    C4, MODE_FOLLOW, MODE_LISTEN, MODE_WAIT, Song, SongNote, DriftEngine,
    load_song,
)

T0 = 1000.0


# ============================================================
# 工具
# ============================================================

def note(nid, pitch, start, dur=0.5, channel=0, vel=80):
    return SongNote(id=nid, pitch=pitch, start=start, end=start + dur,
                    velocity=vel, channel=channel)


def song(*notes, bpm=60.0):
    notes = sorted(notes, key=lambda n: (n.start, n.pitch))
    duration = max((n.end for n in notes), default=0.0)
    return Song(notes=list(notes), bpm=bpm, duration=duration)


class FakeClock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """把引擎模块用的 time.monotonic 换成可控假时钟。"""
    c = FakeClock()
    monkeypatch.setattr("time.monotonic", c)
    return c


def make_engine(*notes, bpm=60.0, mode=MODE_FOLLOW, part="all"):
    eng = DriftEngine()
    eng.set_song(song(*notes, bpm=bpm))
    eng.set_mode(mode)
    eng.count_in = False
    eng.cb_sound = lambda n, is_practice: f"h{n.id}"   # 假句柄，让 _voices 登记
    eng.set_part(part)
    return eng


def step(eng, clock, dt):
    """推进一步：先把假钟拨到目标时刻，再 tick。"""
    clock.now += dt
    eng.tick(clock.now)


def voices_of(eng):
    """(note_id, is_practice) 的已发声集合，供发声规则断言。"""
    return {(v["note"].id, v["note"].id in eng._practice_ids)
            for v in eng._voices.values()}


# ============================================================
# 分组
# ============================================================

def test_groups_same_start_chord_is_one_group():
    eng = make_engine(note(0, 60, 0.0, 1.0), note(1, 64, 0.0, 1.0),
                      note(2, 67, 0.0, 1.0), note(3, 60, 1.0, 1.0))
    assert len(eng._groups) == 2
    assert [n.id for n in eng._groups[0]] == [0, 1, 2]
    assert [n.id for n in eng._groups[1]] == [3]


def test_groups_overlapping_merge():
    eng = make_engine(note(0, 60, 0.0, 1.0), note(1, 64, 0.5, 1.0))
    assert len(eng._groups) == 1


def test_groups_touching_are_separate():
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 62, 0.5, 0.5))
    assert len(eng._groups) == 2


# ============================================================
# 等待模式
# ============================================================

def test_wait_mode_clamps_at_group_start(clock):
    eng = make_engine(note(0, 60, 1.0, 3.0), mode=MODE_WAIT)
    eng.play()
    step(eng, clock, 2.0)                       # raw=2.0 → 卡在组起点 1.0
    assert eng.current_position() == pytest.approx(1.0)
    assert eng.expected_pitches() == {60}
    step(eng, clock, 1.5)                       # 再等多久也不动
    assert eng.current_position() == pytest.approx(1.0)
    assert eng.missed == 0                      # 卡住期间不判漏按


def test_wait_mode_releases_after_correct_press(clock):
    eng = make_engine(note(0, 60, 1.0, 3.0), mode=MODE_WAIT)
    eng.play()
    step(eng, clock, 2.0)                       # 卡在 1.0
    assert eng.user_note_on(62) is None         # 错音不放行
    assert len(eng.hit) == 0
    assert eng.user_note_on(60) is not None     # 按对 → 命中并放行
    step(eng, clock, 1.0)                       # 时间继续走
    assert eng.current_position() == pytest.approx(2.0)
    assert eng.expected_pitches() == set()
    assert eng.missed == 0


# ============================================================
# 三模式发声规则
# ============================================================

def test_follow_mode_sounds_practice_and_accomp(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5, channel=0),
                      note(1, 48, 0.0, 0.5, channel=1), part="track:0")
    eng.play()
    step(eng, clock, 0.05)
    assert voices_of(eng) == {(0, True), (1, False)}


def test_wait_mode_keeps_practice_silent(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5, channel=0),
                      note(1, 48, 0.0, 0.5, channel=1),
                      mode=MODE_WAIT, part="track:0")
    eng.play()
    step(eng, clock, 0.05)
    assert voices_of(eng) == {(1, False)}       # 练习声部留给用户弹


def test_listen_mode_sounds_all_but_no_judgment(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5, channel=0),
                      note(1, 48, 0.0, 0.5, channel=1),
                      mode=MODE_LISTEN, part="track:0")
    eng.play()
    step(eng, clock, 0.05)
    assert voices_of(eng) == {(0, True), (1, False)}
    assert eng.user_note_on(60) is None
    assert eng.attempts == 0 and len(eng.hit) == 0
    step(eng, clock, 1.0)
    assert eng.missed == 0                      # 试听不判漏按


# ============================================================
# 判定与统计口径
# ============================================================

def test_miss_when_crossed_without_press(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 48, 5.0, 0.5, channel=1))
    eng.play()
    step(eng, clock, 0.15)                      # 越过 ±100ms 窗口
    assert eng.missed == 1 and len(eng.hit) == 0


def test_hit_when_holding_key_while_crossing(clock):
    """越线时正按着 → 算命中（长按提前压键）。"""
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 48, 5.0, 0.5, channel=1))
    eng.play()
    eng.pressed.add(60)                         # 提前压住键（不经判定入口）
    step(eng, clock, 0.15)
    assert len(eng.hit) == 1 and eng.missed == 0
    assert eng.successful == 1


def test_accuracy_stats(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 48, 5.0, 0.5, channel=1))
    eng.play()
    eng.user_note_on(62)                        # 错按：计 attempts 不计成功
    eng.user_note_on(60)                        # 按对
    hits, missed, acc = eng.stats()
    assert (hits, missed, acc) == (1, 0, "50%")


def test_stats_dash_when_no_attempts():
    eng = make_engine(note(0, 60, 0.0, 0.5))
    assert eng.stats() == (0, 0, "—")


# ============================================================
# A-B 循环
# ============================================================

def test_ab_loop_rounds_reset_stats(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 62, 1.0, 0.5))
    eng.set_loop(True, 0.0, 2.0)
    eng.play()
    eng.user_note_on(60)                        # 第一遍：按对第一个音
    assert eng.attempts == 1 and len(eng.hit) == 1
    step(eng, clock, 0.5)
    step(eng, clock, 2.0)                       # 越过 B 点 → 回卷
    assert eng.loop_count == 1
    assert eng.attempts == 0 and len(eng.hit) == 0 and eng.missed == 0
    assert eng.playing                          # 循环中不结束
    # 第二遍独立计
    eng.user_note_on(62)                        # pos=0，窗口 ±0.1 → 未中
    assert eng.attempts == 1


def test_song_finishes_without_loop(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5))
    finished = []
    eng.cb_finished = lambda: finished.append(1)
    eng.play()
    step(eng, clock, 0.05)
    step(eng, clock, 0.7)
    assert not eng.playing
    assert finished == [1]


# ============================================================
# 预备拍 / 变速
# ============================================================

def test_count_in_clicks_then_play(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 48, 5.0, 0.5, channel=1))
    eng.count_in = True                         # bpm=60 → 预备 2 拍 = 2 秒
    clicks = []
    eng.cb_click = lambda beats_left: clicks.append(beats_left)
    eng.play()
    step(eng, clock, 0.05)
    step(eng, clock, 1.0)
    step(eng, clock, 1.0)
    assert clicks == [2, 1]
    assert eng.current_position() == pytest.approx(0.05)


def test_set_speed_keeps_position_continuous(clock):
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 48, 5.0, 0.5, channel=1))
    eng.play()
    step(eng, clock, 1.0)
    assert eng.current_position() == pytest.approx(1.0)
    eng.set_speed(0.5)
    step(eng, clock, 2.0)
    assert eng.current_position() == pytest.approx(2.0)   # 1.0 + 2.0×0.5


# ============================================================
# 声部过滤
# ============================================================

def test_part_filter_high_low_and_track():
    eng = make_engine(note(0, 48, 0.0, 0.5, channel=0),
                      note(1, 60, 0.0, 0.5, channel=0),
                      note(2, 72, 0.0, 0.5, channel=1))
    eng.set_part("high")
    assert eng._practice_ids == {1, 2}
    eng.set_part("low")
    assert eng._practice_ids == {0}
    eng.set_part("track:0")                     # 排序后通道列表第 0 项 = ch0
    assert eng._practice_ids == {0, 1}
    eng.set_part("track:1")
    assert eng._practice_ids == {2}


# ============================================================
# 延音踏板
# ============================================================

def test_stop_immediately_without_pedal(clock):
    eng = make_engine(note(0, 60, 0.0, 1.0), note(1, 48, 5.0, 0.5, channel=1))
    stops = []
    eng.cb_stop = lambda handle: stops.append(handle)
    eng.play()
    step(eng, clock, 0.05)
    step(eng, clock, 1.5)                       # pos=1.55 越过音符结束
    assert len(stops) == 1


def test_pedal_delays_stop_by_tail(clock):
    eng = make_engine(note(0, 60, 0.0, 1.0), note(1, 48, 5.0, 0.5, channel=1))
    eng.set_pedal(True)
    stops = []
    eng.cb_stop = lambda handle: stops.append(handle)
    eng.play()
    step(eng, clock, 0.05)
    step(eng, clock, 1.5)                       # 越线：踏板按住 → 不停
    assert stops == []
    step(eng, clock, 0.5)                       # 还没到 tail（0.6s）
    assert stops == []
    step(eng, clock, 0.2)                       # 累计 0.7s > tail → 止音
    assert len(stops) == 1


def test_shift_pedal_counts_as_pedal():
    eng = DriftEngine()
    assert not eng._pedal_active()
    eng.set_shift_pedal(True)
    assert eng._pedal_active()
    eng.set_pedal(True)
    eng.set_shift_pedal(False)
    assert eng._pedal_active()


# ============================================================
# load_song（mido 生成 .mid 文件）
# ============================================================

def _write_mid(tmp_path):
    import mido
    mid = mido.MidiFile(ticks_per_beat=480)
    t0 = mido.MidiTrack()
    mid.tracks.append(t0)
    t0.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90), time=0))
    t0.append(mido.Message("note_on", note=60, velocity=100, channel=0, time=0))
    t0.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=480))
    t0.append(mido.Message("note_on", note=64, velocity=100, channel=0, time=480))
    t0.append(mido.Message("note_on", note=64, velocity=0, channel=0, time=480))  # vel0=off
    t1 = mido.MidiTrack()
    mid.tracks.append(t1)
    t1.append(mido.Message("note_on", note=36, velocity=100, channel=9, time=0))
    t1.append(mido.Message("note_off", note=36, velocity=0, channel=9, time=240))
    path = tmp_path / "test.mid"
    mid.save(str(path))
    return path


def test_load_song_parses_notes_and_skips_drum(tmp_path):
    path = _write_mid(tmp_path)
    s = load_song(str(path))
    assert s.bpm == pytest.approx(90.0, abs=0.1)
    assert len(s.notes) == 2                    # 鼓通道（ch9）已跳过
    n0, n1 = s.notes
    assert n0.pitch == 60 and n0.start == pytest.approx(0.0)
    assert n0.end == pytest.approx(60.0 / 90.0, abs=0.01)   # 480 tick = 1 拍
    assert n1.pitch == 64                       # vel0 note_on 正确配对为 off
    assert n1.start == pytest.approx(2 * 60.0 / 90.0, abs=0.01)
    assert [t.name for t in s.tracks] == ["通道 1"]
    assert s.duration == pytest.approx(3 * 60.0 / 90.0, abs=0.01)


# ============================================================
# 画布 smoke（不崩、键位映射、信号）
# ============================================================

def test_canvas_grab_smoke(qtbot):
    from plugins.practice2._canvas import DriftCanvas
    eng = make_engine(note(0, 60, 0.0, 0.5), note(1, 64, 0.0, 0.5, channel=1),
                      note(2, 67, 1.0, 0.5))
    c = DriftCanvas(eng)
    qtbot.addWidget(c)
    c.resize(900, 520)
    eng.pressed.add(60)
    eng.hit_flash[60] = time.monotonic() + 0.5
    eng.miss_flash[62] = time.monotonic() + 0.5
    eng.set_mode(MODE_WAIT)                     # 走待按键描边路径
    pix = c.grab()
    assert not pix.isNull()


def test_canvas_a_key_maps_to_c4(qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from plugins.practice2._canvas import DriftCanvas
    eng = make_engine(note(0, 60, 0.0, 0.5))
    c = DriftCanvas(eng)
    qtbot.addWidget(c)
    QTest.keyPress(c, Qt.Key.Key_A)
    assert 60 in eng.pressed            # A → C4，默认回调已通知引擎
    QTest.keyRelease(c, Qt.Key.Key_A)
    assert 60 not in eng.pressed


def test_canvas_input_goes_through_callbacks(qtbot):
    """宿主注入回调后，画布按下/松开只走回调，引擎登记由回调负责。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from plugins.practice2._canvas import DriftCanvas
    c = DriftCanvas(DriftEngine())
    qtbot.addWidget(c)
    strikes, offs = [], []
    c.cb_strike = lambda pitch, vel: strikes.append(pitch)
    c.cb_note_off = offs.append
    QTest.keyPress(c, Qt.Key.Key_A)
    QTest.keyRelease(c, Qt.Key.Key_A)
    assert strikes == [60] and offs == [60]


def test_canvas_space_emits_toggle(qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from plugins.practice2._canvas import DriftCanvas
    c = DriftCanvas(DriftEngine())
    qtbot.addWidget(c)
    got = []
    c.toggle_play_requested.connect(lambda: got.append(1))
    QTest.keyClick(c, Qt.Key.Key_Space)
    assert got == [1]


def test_canvas_shift_is_temporary_pedal(qtbot):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from plugins.practice2._canvas import DriftCanvas
    eng = DriftEngine()
    c = DriftCanvas(eng)
    qtbot.addWidget(c)
    QTest.keyPress(c, Qt.Key.Key_Shift)
    assert eng.shift_pedal and eng._pedal_active()
    QTest.keyRelease(c, Qt.Key.Key_Shift)
    assert not eng.shift_pedal


# ============================================================
# 插件面板 smoke（假宿主，不碰真实音频）
# ============================================================

class _FakeAudio:
    inited = False
    last_error = "测试环境不开音频"

    def init_if_needed(self):
        return False

    def load(self, path):
        return None

    def play(self, *a, **k):
        return None

    def allocate_pool(self, *a, **k):
        return {}

    def ensure_channels(self, n):
        return 0

    def release_pool(self, *a):
        pass


class _FakeApp:
    def __init__(self, tmp_path):
        import logging
        self.audio = _FakeAudio()
        self.log = logging.getLogger("test.practice2")
        self._root = tmp_path / "data"

    def data_dir(self, name):
        p = self._root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def subscribe(self, *a):
        pass


def test_plugin_panel_and_demo(qtbot, tmp_path):
    from plugins.practice2.plugin import Practice2Plugin
    plug = Practice2Plugin()
    plug.on_activate(_FakeApp(tmp_path))
    panel = plug.create_panel()
    qtbot.addWidget(panel)
    assert panel is not None

    # 选示范曲 → 歌曲与声部下拉被填充
    idx = plug._combo_demo.findText("小星星")
    assert idx > 0
    plug._combo_demo.setCurrentIndex(idx)
    assert plug.engine.song is not None
    assert len(plug.engine.song.notes) > 0
    assert plug._combo_part.count() >= 3

    # 播放/暂停按钮状态随引擎同步
    plug._on_toggle_play()
    assert "暂停" in plug._btn_play.text()
    plug.engine.pause()
    plug._sync_play_button()
    assert "播放" in plug._btn_play.text()


def test_plugin_config_debounced_save(qtbot, tmp_path):
    from plugins.practice2.plugin import Practice2Plugin
    plug = Practice2Plugin()
    plug.on_activate(_FakeApp(tmp_path))
    panel = plug.create_panel()
    qtbot.addWidget(panel)

    idx = plug._combo_mode.findData("wait")
    assert idx >= 0
    plug._combo_mode.setCurrentIndex(idx)       # 触发 _mark_dirty → 400ms 防抖
    import json
    cfg_path = plug._config_path()
    qtbot.wait(600)
    assert cfg_path.exists()
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["mode"] == "wait"


# ============================================================
# 用户音通道管理（松键止音 / 同音重按止旧 / 踏板延音 / 防误杀）
# ============================================================

_SND = object()                                 # 采样对象哨兵（句柄归属校验用）


class _FakeMixerChannel:
    calls = []                                  # [(channel, ms), ...]

    def __init__(self, idx):
        self.idx = idx

    def get_sound(self):
        return _SND                             # 测试里不复用通道，恒等于句柄里的声音

    def fadeout(self, ms):
        _FakeMixerChannel.calls.append((self.idx, ms))


class _FakePygame:
    class mixer:
        Channel = _FakeMixerChannel


class _SeqAudio(_FakeAudio):
    """play 返回递增通道号，用于验证通道登记与止音。"""

    def __init__(self):
        self.next_ch = 100

    def load(self, path):
        return type("H", (), {"sound": _SND})()  # 句柄带 .sound 属性

    def play(self, *a, **k):
        ch = self.next_ch
        self.next_ch += 1
        return ch

    def fadeout_if(self, channel, sound, ms):
        # 模拟归属校验：声音匹配才淡出
        if sound is _SND:
            _FakeMixerChannel.calls.append((channel, ms))


def _make_voice_plug(tmp_path, monkeypatch):
    """构造开了用户音通道管理的插件（pygame 换成假 mixer 记录 fadeout）。"""
    from plugins.practice2 import plugin as _plugin_mod
    from plugins.practice2.plugin import Practice2Plugin
    monkeypatch.setattr(_plugin_mod, "pygame", _FakePygame)
    _FakeMixerChannel.calls.clear()
    plug = Practice2Plugin()
    plug.on_activate(_FakeApp(tmp_path))
    plug.app.audio = _SeqAudio()
    return plug


def test_user_note_off_releases_channel(tmp_path, monkeypatch):
    plug = _make_voice_plug(tmp_path, monkeypatch)

    plug._strike(60, 90)
    ch, snd = plug._user_ch[60]
    assert ch == 100 and snd is _SND
    plug._handle_note_off(60)                   # 无踏板 → 按 tail(0.6s) 淡出
    assert 60 not in plug._user_ch
    assert (100, 600) in _FakeMixerChannel.calls


def test_same_pitch_restrike_fades_old_channel(tmp_path, monkeypatch):
    plug = _make_voice_plug(tmp_path, monkeypatch)

    plug._strike(60, 90)
    plug._strike(60, 90)                        # 同音重按 → 止旧(80ms)起新
    ch, snd = plug._user_ch[60]
    assert ch == 101
    assert (100, 80) in _FakeMixerChannel.calls


def test_pedal_holds_note_until_released(tmp_path, monkeypatch):
    plug = _make_voice_plug(tmp_path, monkeypatch)

    plug.engine.set_pedal(True)
    plug._strike(62, 90)
    plug._handle_note_off(62)                   # 踏板按住 → 不收尾
    assert 62 in plug._user_ch
    assert (100, 600) not in _FakeMixerChannel.calls

    plug.engine.set_pedal(False)                # 松踏板 → 统一收尾
    assert 62 not in plug._user_ch
    assert (100, 600) in _FakeMixerChannel.calls


def test_engine_stop_verifies_sound_owner(tmp_path, monkeypatch):
    """引擎止音前验证声音归属：通道被复用后不误杀新声音。"""
    plug = _make_voice_plug(tmp_path, monkeypatch)
    _FakeMixerChannel.calls.clear()

    plug._on_engine_stop((50, _SND))            # 归属匹配 → 淡出
    assert (50, 150) in _FakeMixerChannel.calls

    _FakeMixerChannel.calls.clear()
    other = object()
    plug._on_engine_stop((50, other))           # 通道已易主 → 不动
    assert not any(c == 50 and ms == 150 for c, ms in _FakeMixerChannel.calls)


def test_fade_channel_falls_back_without_host_support(tmp_path, monkeypatch):
    """旧宿主无 fadeout_if：_fade_channel 退化为直接 pygame 淡出，不崩。"""
    plug = _make_voice_plug(tmp_path, monkeypatch)
    plug._strike(60, 90)
    ch, snd = plug._user_ch[60]
    plug.app.audio.fadeout_if = None            # 模拟旧宿主缺此 API
    _FakeMixerChannel.calls.clear()
    plug._fade_channel(ch, snd, 600)
    assert (ch, 600) in _FakeMixerChannel.calls
