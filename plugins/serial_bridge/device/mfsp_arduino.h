// mfsp_arduino.h — MFSP v1（MidiForge Serial Protocol）设备端参考实现
// header-only，Arduino / ESP32 通用（只依赖 Arduino.h / Stream.h）
//
// 帧格式（与插件端 _codec.py / device/mfsp_vectors.json 同源）：
//   | SYNC 0x7E | LEN | CMD | PAYLOAD(0-32B) | CRC8 |
//                LEN 覆盖 CMD+PAYLOAD    CRC8 多项式 0x07，初值 0x00
//
// 设备→宿主：0x10 note_on / 0x11 note_off / 0x20 cc / 0x30 pitchbend /
//            0x40 heartbeat / 0x50 analog_in / 0x51 digital_in / 0xF0 debug
// 宿主→设备：0x80/0x81/0x82 note_on/note_off/cc / 0x83 analog_out /
//            0x84 digital_out / 0x8F ping（应回一帧 heartbeat）
//
// 注意：MIDI 通道不进协议（上位机 UI 统一配置）；单帧文本 ≤32 字节，
// 长调试信息请分帧发送。

#ifndef MFSP_ARDUINO_H
#define MFSP_ARDUINO_H

#include <Arduino.h>
#include <Stream.h>

namespace mfsp {

const uint8_t SYNC = 0x7E;
const uint8_t PAYLOAD_MAX = 32;

// ---- CRC-8（多项式 0x07，初值 0x00；检查值 "123456789" → 0xF4）----
uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0x00;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}

// ---- 发送（设备→宿主）----
void sendFrame(Stream &s, uint8_t cmd, const uint8_t *payload, uint8_t len) {
  if (len > PAYLOAD_MAX) len = PAYLOAD_MAX;
  s.write(SYNC);
  s.write((uint8_t)(1 + len));           // LEN 覆盖 CMD+PAYLOAD
  s.write(cmd);
  for (uint8_t i = 0; i < len; i++) s.write(payload[i]);
  uint8_t body[1 + PAYLOAD_MAX];         // CRC 覆盖 CMD+PAYLOAD（与 LEN 同段）
  body[0] = cmd;
  if (len) memcpy(body + 1, payload, len);
  s.write(crc8(body, 1 + len));
}

void noteOn(Stream &s, uint8_t note, uint8_t velocity) {
  uint8_t p[2] = {(uint8_t)(note & 0x7F), (uint8_t)(velocity & 0x7F)};
  sendFrame(s, 0x10, p, 2);
}
void noteOff(Stream &s, uint8_t note) {
  uint8_t p = (uint8_t)(note & 0x7F);
  sendFrame(s, 0x11, &p, 1);
}
void cc(Stream &s, uint8_t num, uint8_t value) {
  uint8_t p[2] = {(uint8_t)(num & 0x7F), (uint8_t)(value & 0x7F)};
  sendFrame(s, 0x20, p, 2);
}
void pitchbend(Stream &s, int16_t pitch) {   // -8192..8191，lsb/msb 7bit 组
  uint16_t v = (uint16_t)(constrain(pitch, -8192, 8191) + 8192);
  uint8_t p[2] = {(uint8_t)(v & 0x7F), (uint8_t)((v >> 7) & 0x7F)};
  sendFrame(s, 0x30, p, 2);
}
void heartbeat(Stream &s, uint16_t uptime_s) {
  uint8_t p[2] = {(uint8_t)(uptime_s & 0xFF), (uint8_t)(uptime_s >> 8)};
  sendFrame(s, 0x40, p, 2);
}
void analogIn(Stream &s, uint8_t port, uint16_t value) {  // 0-4095，小端
  uint16_t v = value > 4095 ? 4095 : value;
  uint8_t p[3] = {(uint8_t)(port & 0x0F), (uint8_t)(v & 0xFF),
                  (uint8_t)(v >> 8)};
  sendFrame(s, 0x50, p, 3);
}
void digitalIn(Stream &s, uint8_t port, uint8_t state) {
  uint8_t p[2] = {(uint8_t)(port & 0x0F), (uint8_t)(state ? 1 : 0)};
  sendFrame(s, 0x51, p, 2);
}
void debug(Stream &s, const char *text) {    // UTF-8，≤32 字节
  sendFrame(s, 0xF0, (const uint8_t *)text,
            (uint8_t)min(strlen(text), (size_t)PAYLOAD_MAX));
}

// ---- 接收（宿主→设备）----
enum EventType : uint8_t {
  MFSP_NONE, MFSP_NOTE_ON, MFSP_NOTE_OFF, MFSP_CC,
  MFSP_ANALOG_OUT, MFSP_DIGITAL_OUT, MFSP_PING,
};

struct Event {
  EventType type;
  uint8_t a;       // note / cc 编号 / 端口
  uint8_t b;       // velocity / cc 值 / 开关状态
  uint16_t value;  // analog 12bit 值
};

class Parser {
 public:
  // 每帧 loop() 调一次：消费串口收到的字节，最多返回一条完整事件
  Event poll(Stream &s) {
    Event ev = {MFSP_NONE, 0, 0, 0};
    while (s.available()) {
      uint8_t b = s.read();
      if (_st == SEEK_SYNC) {
        if (b == SYNC) { _idx = 0; _st = READ_LEN; }
      } else if (_st == READ_LEN) {
        if (b == SYNC) continue;                 // 连续 SYNC：重新对齐
        if (b >= 1 && b <= 1 + PAYLOAD_MAX) { _need = b; _st = READ_BODY; }
        else _st = SEEK_SYNC;                    // 非法长度：丢弃
      } else {                                   // READ_BODY
        if (b == SYNC) { _idx = 0; _st = READ_LEN; continue; }  // 帧中途 SYNC：残帧丢弃
        _buf[_idx++] = b;
        if (_idx >= _need + 1) {                 // CMD+PAYLOAD+CRC 收满
          _st = SEEK_SYNC;
          if (crc8(_buf, _need) == _buf[_need])
            return decode(_buf[0], _buf + 1, _need - 1);
          // CRC 错：静默丢弃，等下一个 SYNC
        }
      }
    }
    return ev;
  }

 private:
  static Event decode(uint8_t cmd, const uint8_t *pl, uint8_t len) {
    Event ev = {MFSP_NONE, 0, 0, 0};
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
  uint8_t _buf[1 + PAYLOAD_MAX + 1];   // CMD+PAYLOAD(≤33)+CRC
  uint8_t _need = 0, _idx = 0;
};

}  // namespace mfsp

#endif  // MFSP_ARDUINO_H
