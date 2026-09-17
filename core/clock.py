"""统一时钟服务 — QTimer 驱动，支持 BPM / beat / bar tick 和 Tap Tempo。

=== 为什么宿主维护 clock ===
    Looper 和 Arpeggiator 需要共同的时间基准。如果每个插件自己 sleep，
    它们会逐渐漂移——**宿主维护一个 clock，所有插件订阅同一个 beat 信号**。

=== 插件使用 ===
    class MyPlugin(Plugin):
        def on_activate(self, app):
            clock = app.clock
            clock.bpm = 120
            # 订阅 beat 和 bar
            app.subscribe("clock.beat", self._on_beat)
            app.subscribe("clock.bar", self._on_bar)
            # 或者手动 tap 打拍子
            clock.tap()  # 算 BPM（默认 5 次求平均）

        def _on_beat(self, beat_index):
            # beat_index: 当前第几拍（1-4 循环）
            looper.record_step(beat_index)
"""

import time
from typing import List, Optional


class _Clock:
    """宿主维护的 BPM 时钟。用 QTimer 驱动（宿主在 AppContext.start() 后启动）。

    宿主需要先调用:
        app.clock.start()   # 启动后会按 BPM 发 "clock.tick" / "clock.beat" / "clock.bar"
    停止:
        app.clock.stop()
    """

    def __init__(self, bus=None, default_bpm: float = 120.0):
        self._bus = bus
        self._bpm = default_bpm
        self._beat = 0          # 当前第几拍 (0-based, 显示时 +1)
        self._bar = 0           # 当前第几小节
        self._running = False
        self._tick_count = 0    # 每拍切 4 tick（16th note）

        # Tap tempo
        self._tap_times: List[float] = []

        # 宿主注入 QTimer（MidiForge 用 QTimer；headless 模式用 threading.Timer）
        self._timer = None
        self._interval_ms = 0
        self._last_bar_subscribed = False  # 避免每次 tick 都 publish bar

    # ---- BPM ----

    @property
    def bpm(self) -> float:
        return self._bpm

    @bpm.setter
    def bpm(self, value: float) -> None:
        self._bpm = max(30.0, min(300.0, value))
        self._update_interval()

    # ---- 状态 ----

    @property
    def beat(self) -> int:
        """当前第几拍（0-based, 范围 0-3）。"""
        return self._beat

    @property
    def bar(self) -> int:
        """当前第几小节（0-based）。"""
        return self._bar

    # ---- Tap Tempo ----

    def tap(self) -> Optional[float]:
        """Tap tempo：记录一次敲击时间戳，返回当前估计 BPM（需要 >= 2 次）。
        超过 2 秒不 tap 自动重置。"""
        now = time.monotonic()
        # 清除过期 tap
        self._tap_times = [t for t in self._tap_times if now - t < 2.0]
        self._tap_times.append(now)

        if len(self._tap_times) >= 2:
            intervals = [self._tap_times[i] - self._tap_times[i - 1]
                         for i in range(1, len(self._tap_times))]
            avg = sum(intervals) / len(intervals)
            bpm = 60.0 / avg
            # 限制合理范围
            self.bpm = max(30.0, min(300.0, bpm))
            return self.bpm
        return None

    def reset_tap(self) -> None:
        self._tap_times.clear()

    # ---- 宿主内部：启动/停止 ----

    def _start(self, timer_factory=None, interval_ms_callback=None) -> None:
        """宿主调用。timer_factory 是 QTimer 创建函数（QApplication 已跑后才能用）。"""
        if self._running:
            return
        self._running = True
        self._update_interval()
        if timer_factory:
            # GUI 模式: 用 QTimer
            self._timer = timer_factory()
            self._timer.timeout.connect(self._tick_callback)
            if interval_ms_callback:
                self._timer.start(interval_ms_callback())
            else:
                self._timer.start(self._interval_ms)
        # headless 模式: 宿主循环里手动调 _tick_callback

    def _stop(self) -> None:
        self._running = False
        if self._timer:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None

    def _update_interval(self) -> None:
        # 每拍切 4 tick（16th note 精度）
        beat_ms = 60000.0 / self._bpm
        self._interval_ms = int(beat_ms / 4)

    # ---- tick 回调 ----

    def _tick_callback(self) -> None:
        """宿主定时器每 interval_ms 调一次。"""
        if not self._running or not self._bus:
            return

        self._tick_count += 1
        tick_per_beat = 4

        # 每 tick 发一次 "clock.tick"（16th note 精度）
        self._bus.publish("clock.tick", {
            "tick": self._tick_count,
            "beat": self._beat + 1,
            "bar": self._bar + 1,
            "bpm": self._bpm,
        })

        if self._tick_count % tick_per_beat == 0:
            # 新的一拍
            self._beat += 1
            if self._beat >= 4:
                self._beat = 0
                self._bar += 1
                self._bus.publish("clock.bar", {
                    "bar": self._bar + 1,
                    "bpm": self._bpm,
                })
            self._bus.publish("clock.beat", {
                "beat": self._beat + 1,
                "bar": self._bar + 1,
                "bpm": self._bpm,
            })
