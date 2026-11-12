// mfsp_minimal_single.ino — MFSP 单文件版示例（ESP32 / Arduino 通用）
//
// 协议实现（CRC8 / 收发帧 / 解析器）内联进本文件：新建同名文件夹，
// 只放这一个 .ino 即可编译上传，不用管 header 文件。
// 协议参考实现另见 ../mfsp_arduino.h（header-only，selftest 用）。
//
// 示例内容：
//   A0 电位器（map 到 0-4095）→ analog_in 端口 0，约 50Hz
//     —— MidiForge 入向映射表「模拟 端口0 → CC」即可当 MIDI 旋钮
//   GPIO4 按键（接 GND，INPUT_PULLUP）→ digital_in 端口 0
//     —— 入向映射「数字 端口0 → 音符」即可触发打击垫
//   digital_out 端口 0 → 板载 LED（GPIO2，宿主出向映射「音符→数字」）
//   1s 心跳（uptime 秒数，面板状态行显示心跳在线）；收到 ping 回心跳
//
// 上传：开发板选 ESP32 Dev Module，端口选板子的 COM 口（CP210x）
// 注意：MIDI 通道不进协议，宿主面板「通道改写」统一配置（默认 ch0）

#include <Arduino.h>

// ============ MFSP v1 协议实现（内联自 mfsp_arduino.h）============
// 帧格式：| SYNC 0x7E | LEN | CMD | PAYLOAD(0-32B) | CRC8 |
//          LEN 覆盖 CMD+PAYLOAD    CRC8 多项式 0x07，初值 0x00

const uint8_t MFSP_SYNC = 0x7E;
const uint8_t MFSP_PAYLOAD_MAX = 32;

uint8_t mfsp_crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0x00;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}

void mfsp_sendFrame(Stream &s, uint8_t cmd, const uint8_t *payload, uint8_t len) {
  if (len > MFSP_PAYLOAD_MAX) len = MFSP_PAYLOAD_MAX;
  s.write(MFSP_SYNC);
  s.write((uint8_t)(1 + len));
  s.write(cmd);
  for (uint8_t i = 0; i < len; i++) s.write(payload[i]);
  uint8_t body[1 + MFSP_PAYLOAD_MAX];
  body[0] = cmd;
  if (len) memcpy(body + 1, payload, len);
  s.write(mfsp_crc8(body, 1 + len));
}

void mfsp_noteOn(Stream &s, uint8_t note, uint8_t velocity) {
  uint8_t p[2] = {(uint8_t)(note & 0x7F), (uint8_t)(velocity & 0x7F)};
  mfsp_sendFrame(s, 0x10, p, 2);
}
void mfsp_noteOff(Stream &s, uint8_t note) {
  uint8_t p = (uint8_t)(note & 0x7F);
  mfsp_sendFrame(s, 0x11, &p, 1);
}
void mfsp_cc(Stream &s, uint8_t num, uint8_t value) {
  uint8_t p[2] = {(uint8_t)(num & 0x7F), (uint8_t)(value & 0x7F)};
  mfsp_sendFrame(s, 0x20, p, 2);
}
void mfsp_heartbeat(Stream &s, uint16_t uptime_s) {
  uint8_t p[2] = {(uint8_t)(uptime_s & 0xFF), (uint8_t)(uptime_s >> 8)};
  mfsp_sendFrame(s, 0x40, p, 2);
}
void mfsp_analogIn(Stream &s, uint8_t port, uint16_t value) {  // 0-4095 小端
  uint16_t v = value > 4095 ? 4095 : value;
  uint8_t p[3] = {(uint8_t)(port & 0x0F), (uint8_t)(v & 0xFF), (uint8_t)(v >> 8)};
  mfsp_sendFrame(s, 0x50, p, 3);
}
void mfsp_digitalIn(Stream &s, uint8_t port, uint8_t state) {
  uint8_t p[2] = {(uint8_t)(port & 0x0F), (uint8_t)(state ? 1 : 0)};
  mfsp_sendFrame(s, 0x51, p, 2);
}
void mfsp_debug(Stream &s, const char *text) {  // UTF-8，≤32 字节
  mfsp_sendFrame(s, 0xF0, (const uint8_t *)text,
                 (uint8_t)min(strlen(text), (size_t)MFSP_PAYLOAD_MAX));
}

// 宿主→设备事件
enum MfspEventType : uint8_t {
  MFSP_NONE, MFSP_NOTE_ON, MFSP_NOTE_OFF, MFSP_CC,
  MFSP_ANALOG_OUT, MFSP_DIGITAL_OUT, MFSP_PING,
};

struct MfspEvent {
  MfspEventType type;
  uint8_t a;       // note / cc 编号 / 端口
  uint8_t b;       // velocity / cc 值 / 开关状态
  uint16_t value;  // analog 12bit 值
};

class MfspParser {
 public:
  // 每帧 loop() 调一次：消费收到的字节，最多返回一条完整事件
  MfspEvent poll(Stream &s) {
    MfspEvent ev = {MFSP_NONE, 0, 0, 0};
    while (s.available()) {
      uint8_t b = s.read();
      if (_st == SEEK_SYNC) {
        if (b == MFSP_SYNC) { _idx = 0; _st = READ_LEN; }
      } else if (_st == READ_LEN) {
        if (b == MFSP_SYNC) continue;
        if (b >= 1 && b <= 1 + MFSP_PAYLOAD_MAX) { _need = b; _st = READ_BODY; }
        else _st = SEEK_SYNC;
      } else {                                     // READ_BODY
        if (b == MFSP_SYNC) { _idx = 0; _st = READ_LEN; continue; }
        _buf[_idx++] = b;
        if (_idx >= _need + 1) {                   // CMD+PAYLOAD+CRC 收满
          _st = SEEK_SYNC;
          if (mfsp_crc8(_buf, _need) == _buf[_need])
            return decode(_buf[0], _buf + 1, _need - 1);
        }
      }
    }
    return ev;
  }

 private:
  static MfspEvent decode(uint8_t cmd, const uint8_t *pl, uint8_t len) {
    MfspEvent ev = {MFSP_NONE, 0, 0, 0};
    switch (cmd) {
      case 0x80: if (len == 2) { ev.type = MFSP_NOTE_ON;  ev.a = pl[0]; ev.b = pl[1]; } break;
      case 0x81: if (len == 1) { ev.type = MFSP_NOTE_OFF; ev.a = pl[0]; } break;
      case 0x82: if (len == 2) { ev.type = MFSP_CC;       ev.a = pl[0]; ev.b = pl[1]; } break;
      case 0x83: if (len == 3) {
        ev.type = MFSP_ANALOG_OUT; ev.a = pl[0] & 0x0F;
        ev.value = min((uint16_t)(pl[1] | ((uint16_t)pl[2] << 8)), (uint16_t)4095);
      } break;
      case 0x84: if (len == 2) { ev.type = MFSP_DIGITAL_OUT; ev.a = pl[0] & 0x0F; ev.b = pl[1] ? 1 : 0; } break;
      case 0x8F: if (len == 0) { ev.type = MFSP_PING; } break;
      default: break;
    }
    return ev;
  }

  enum State : uint8_t { SEEK_SYNC, READ_LEN, READ_BODY };
  State _st = SEEK_SYNC;
  uint8_t _buf[1 + MFSP_PAYLOAD_MAX + 1];
  uint8_t _need = 0, _idx = 0;
};

// ============================ 示例主体 ============================

// ESP32 引脚适配：GPIO4 按键（GPIO2 是板载 LED，GPIO0 是 BOOT 键）
const int PIN_POT = A0;         // ESP32 = GPIO36，接电位器中间脚
const int PIN_BTN = 4;          // 按键接 GPIO4 ↔ GND
const int PIN_LED = 2;  // ESP32 DevKit 板载 LED（旧板包无 LED_BUILTIN 宏，写死 GPIO2）

MfspParser parser;
uint16_t uptime = 0;
uint32_t t_hb = 0, t_adc = 0;
bool last_btn = HIGH;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  mfsp_debug(Serial, "mfsp ready");
}

void loop() {
  uint32_t now = millis();

  if (now - t_hb >= 1000) {                 // 1s 心跳
    t_hb = now;
    mfsp_heartbeat(Serial, uptime++);
  }
  if (now - t_adc >= 20) {                  // A0 → analog_in(0)，50Hz
    t_adc = now;
    mfsp_analogIn(Serial, 0, analogRead(PIN_POT));  // ESP32 ADC 原生 0-4095
  }

  bool btn = digitalRead(PIN_BTN);          // GPIO4 → digital_in(0)
  if (btn != last_btn) {
    last_btn = btn;
    mfsp_digitalIn(Serial, 0, btn == LOW ? 1 : 0);
  }

  MfspEvent ev = parser.poll(Serial);       // 宿主 → 设备
  switch (ev.type) {
    case MFSP_DIGITAL_OUT:
      if (ev.a == 0) digitalWrite(PIN_LED, ev.b);
      break;
    case MFSP_ANALOG_OUT:
      // GPIO2 走 LEDC 支持 PWM：宿主 CC(0-127) 经映射 <<5 成 0-4064
      analogWrite(PIN_LED, map((long)ev.value, 0, 4095, 0, 255));
      break;
    case MFSP_PING:
      mfsp_heartbeat(Serial, uptime);       // ping → 回心跳
      break;
    default:
      break;
  }
}
