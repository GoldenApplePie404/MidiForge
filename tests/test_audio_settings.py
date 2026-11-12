"""音频设备设置测试：fake mixer 下验证 desired 应用、restart/回退、配置持久化。

不碰真实音频设备：monkeypatch core.audio 的 pygame 为 fake，
init 只在设备名匹配时成功，用于模拟"指定设备不在线"等场景。
"""
import json
import os
import sys
import types

import pytest

from core.audio import _AudioService
from core.audio import _PYGAME_AVAILABLE


# ============================================================
# fake mixer
# ============================================================

class _FakeMixer:
    """init(devicename) 行为可控：fail_device 指定时该设备初始化失败。"""

    def __init__(self):
        self.calls = []            # 每次 init 的参数
        self.fail_device = None    # 该设备名 init 必失败
        self.fail_all = False
        self.inited = False
        self.num_channels = 0

    def init(self, freq=44100, size=-16, channels=2, buffer=512,
             devicename=None, **kw):
        drv = os.environ.get("SDL_AUDIODRIVER", "")
        if drv and drv not in ("winmm", "directsound", "wasapi"):
            self.inited = False
            raise RuntimeError(f"no such driver: {drv}")
        if self.fail_all or (self.fail_device is not None
                             and devicename == self.fail_device):
            self.inited = False
            raise RuntimeError(f"no such device: {devicename}")
        self.calls.append({"freq": freq, "buffer": buffer, "device": devicename})
        self.inited = True
        self.num_channels = 8

    def quit(self):
        self.inited = False

    def set_num_channels(self, n):
        self.num_channels = n

    def get_num_channels(self):
        return self.num_channels

    def stop(self):
        pass

    def get_init(self):
        return (44100, -16, 2)

    class _Ch:
        def get_busy(self):
            return False

        def get_sound(self):
            return None

    def Channel(self, idx):
        return self._Ch()

    def Sound(self, path):
        return object()


@pytest.fixture
def mixer(monkeypatch):
    fake = _FakeMixer()
    fake_pygame = types.SimpleNamespace(mixer=fake)
    monkeypatch.setattr("core.audio.pygame", fake_pygame)
    monkeypatch.setattr("core.audio._PYGAME_AVAILABLE", True)
    return fake


@pytest.fixture
def clean_env():
    """_try_init 会改 SDL_AUDIODRIVER，测试后恢复，防止污染同进程的其他测试。"""
    saved = os.environ.pop("SDL_AUDIODRIVER", None)
    yield
    if saved is None:
        os.environ.pop("SDL_AUDIODRIVER", None)
    else:
        os.environ["SDL_AUDIODRIVER"] = saved


# ============================================================
# desired 配置应用
# ============================================================

def test_desired_applied_on_init(mixer, clean_env):
    svc = _AudioService(channels=4, desired={
        "driver": "winmm", "sample_rate": 48000, "buffer": 512})
    assert svc.init_if_needed()
    assert mixer.calls[-1] == {"freq": 48000, "buffer": 512, "device": None}
    assert os.environ.get("SDL_AUDIODRIVER") == "winmm"
    assert svc.last_error == ""


def test_desired_device_passed_to_mixer(mixer, clean_env):
    svc = _AudioService(channels=4, desired={"device": "CABLE Input"})
    assert svc.init_if_needed()
    assert mixer.calls[-1]["device"] == "CABLE Input"


def test_desired_driver_failed_falls_back(mixer, clean_env):
    """指定驱动失败 → 自动回退降级链（winmm 可用），原因已记录。"""
    mixer.fail_device = None
    svc = _AudioService(channels=4, desired={"driver": "no_such_drv"})
    # fake 只在设备名不匹配时失败——驱动失败用 fail_all 模拟不了精细场景，
    # 这里直接让 no_such_drv 之外的驱动成功：fake 对驱动无感知，天然成功。
    assert svc.init_if_needed()
    assert svc._driver == ""            # 回退链第一档 = SDL 默认驱动


def test_desired_device_failure_full_fallback(mixer, clean_env):
    """指定设备是失败根源 → 降级链（带设备）失败 → 全默认兜底成功。"""
    mixer.fail_device = "GHOST DEVICE"
    svc = _AudioService(channels=4, desired={"device": "GHOST DEVICE"})
    assert svc.init_if_needed()
    assert mixer.calls[-1]["device"] is None   # 全默认兜底
    assert svc.inited


# ============================================================
# 设备枚举
# ============================================================

def test_list_output_devices_ok(monkeypatch, mixer):
    fake_mod = types.ModuleType("pygame._sdl2.audio")
    fake_mod.get_audio_device_names = lambda capture: ["A", "B"]
    monkeypatch.setitem(sys.modules, "pygame._sdl2.audio", fake_mod)
    assert _AudioService().list_output_devices() == ["A", "B"]


@pytest.mark.skipif(not _PYGAME_AVAILABLE, reason="pygame 不可用")
def test_list_output_devices_tolerant(monkeypatch, mixer):
    """_sdl2 子模块缺失（ImportError）时返回空列表，不抛异常。"""
    monkeypatch.setitem(sys.modules, "pygame._sdl2.audio", None)  # None → import 即抛
    assert _AudioService().list_output_devices() == []


def test_list_output_devices_no_pygame(monkeypatch):
    monkeypatch.setattr("core.audio._PYGAME_AVAILABLE", False)
    assert _AudioService().list_output_devices() == []


# ============================================================
# restart
# ============================================================

def test_restart_applies_and_notifies(mixer, clean_env):
    svc = _AudioService(channels=4)
    assert svc.init_if_needed()
    notified = []
    svc.on_restarted = lambda: notified.append(1)

    ok, err = svc.restart(device="CABLE Input", sample_rate=48000, buffer=256)
    assert ok and err == ""
    assert notified == [1]
    assert mixer.calls[-1] == {"freq": 48000, "buffer": 256,
                               "device": "CABLE Input"}
    cfg = svc.current_config()
    assert (cfg["device"], cfg["sample_rate"], cfg["buffer"]) == \
        ("CABLE Input", 48000, 256)


def test_restart_explicit_none_resets_device(mixer, clean_env):
    """显式传 None = 恢复系统默认（未传的 key 不动）。"""
    svc = _AudioService(channels=4)
    svc.init_if_needed()
    svc.restart(device="CABLE Input")
    assert svc.current_config()["device"] == "CABLE Input"

    ok, _ = svc.restart(device=None)
    assert ok
    assert mixer.calls[-1]["device"] is None
    assert svc.current_config()["device"] is None


def test_restart_fallback_keeps_audio_alive(mixer, clean_env):
    """新参数失败 → 按旧参数回退成功；mixer 回到可用状态并通知重建。"""
    svc = _AudioService(channels=4)
    svc.init_if_needed()
    notified = []
    svc.on_restarted = lambda: notified.append(1)

    mixer.fail_device = "GHOST DEVICE"
    ok, err = svc.restart(device="GHOST DEVICE")
    assert not ok and "GHOST DEVICE" in err
    assert mixer.inited                          # 回退后音频活着
    assert svc.current_config()["device"] is None   # 旧 desired
    assert notified == [1]                       # 句柄已失效，仍需通知


def test_restart_total_failure(mixer, clean_env):
    """新旧参数全部失败 → 返回 False，mixer 保持未初始化。"""
    svc = _AudioService(channels=4)
    svc.init_if_needed()
    mixer.fail_all = True
    ok, err = svc.restart(sample_rate=48000)
    assert not ok and err
    assert not mixer.inited
    assert not svc.inited


def test_restart_without_pygame(monkeypatch):
    monkeypatch.setattr("core.audio._PYGAME_AVAILABLE", False)
    ok, err = _AudioService().restart(device="X")
    assert not ok and err == "pygame 不可用"


# ============================================================
# AppContext 配置读写（绕过 __post_init__ 的轻量单测）
# ============================================================

def _bare_app(tmp_path):
    from api import AppContext
    app = AppContext.__new__(AppContext)     # 跳过 post_init：只测 IO 方法
    app.bindings_path = tmp_path / "config" / "bindings.json"
    app._plugin_dir = tmp_path
    return app


def test_audio_config_roundtrip(tmp_path):
    app = _bare_app(tmp_path)
    assert app.load_audio_config() == {"device": None, "driver": None,
                                       "sample_rate": None, "buffer": None}
    cfg = {"device": "CABLE Input", "driver": "winmm",
           "sample_rate": 48000, "buffer": 512}
    app.save_audio_config(cfg)
    assert app.load_audio_config() == cfg
    assert (tmp_path / "config" / "audio.json").exists()


def test_audio_config_corrupted(tmp_path):
    app = _bare_app(tmp_path)
    path = tmp_path / "config" / "audio.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken json", encoding="utf-8")
    assert app.load_audio_config()["device"] is None     # 损坏 → 全默认


def test_audio_config_extra_keys_ignored(tmp_path):
    app = _bare_app(tmp_path)
    app.save_audio_config({"device": "A", "hacker": True, "driver": None,
                           "sample_rate": None, "buffer": None})
    loaded = app.load_audio_config()
    assert "hacker" not in loaded and loaded["device"] == "A"
