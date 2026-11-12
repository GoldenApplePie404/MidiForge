// passthrough_midi_library.ino — 透传协议示例（SerialBridge 默认协议）
//
// 串口上直接跑标准 MIDI 1.0 字节流，MidiForge 串口桥协议下拉选「透传」。
// 依赖：Arduino MIDI Library（FortySevenEffects，库管理器搜 "MIDI Library"）
//
// 示例内容：
//   A0 电位器 → CC1（50Hz 上报）
//   D2 按钮（INPUT_PULLUP）→ note 60 按下/松开
//   MIDI.read() 处理宿主下发的消息（勾「MIDI → 串口」后可反控设备）
//
// 接线：USB 线直连即可；USB-TTL 模块则 TX↔RX 交叉、共地。波特率 115200。

#include <MIDI.h>

MIDI_CREATE_DEFAULT_INSTANCE();

const int PIN_POT = A0;
const int PIN_BTN = 2;
const uint8_t MIDI_CH = 1;      // 设备侧通道；宿主「通道改写」可再统一

void setup() {
  Serial.begin(115200);         // Hairless / MidiForge 串口桥用 115200，非 31250
  MIDI.begin(MIDI_CHANNEL_OMNI);
  pinMode(PIN_BTN, INPUT_PULLUP);
}

void loop() {
  static uint32_t t_adc = 0;
  static bool last_btn = HIGH;

  // A0 → CC1，约 50Hz
  uint32_t now = millis();
  if (now - t_adc >= 20) {
    t_adc = now;
    MIDI.sendControlChange(1, analogRead(PIN_POT) >> 3, MIDI_CH);
  }

  // D2 → note 60（按钮按下为 LOW）
  bool btn = digitalRead(PIN_BTN);
  if (btn != last_btn) {
    last_btn = btn;
    if (btn == LOW) MIDI.sendNoteOn(60, 100, MIDI_CH);
    else            MIDI.sendNoteOff(60, 0, MIDI_CH);
  }

  // 宿主方向：读到 note/cc 可驱动 LED / 蜂鸣器等
  if (MIDI.read()) {
    if (MIDI.getType() == midi::NoteOn && MIDI.getData1() == 60) {
      // digitalWrite(LED_BUILTIN, MIDI.getData2() > 0 ? HIGH : LOW);
    }
  }
}
