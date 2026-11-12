// mfsp_selftest.ino — MFSP 共享测试向量的设备端核对
//
// 把 device/mfsp_vectors.json 里的向量原样打印/发送，用于核对设备端
// CRC8 / 帧组包与插件端逐字节一致（pytest 端断言见
// tests/test_serial_bridge_mfsp.py::test_vectors_encode / test_vectors_decode）。

#include "mfsp_arduino.h"

// 共享向量：encode 组（宿主→设备）与 decode 组（设备→宿主）
// 与 device/mfsp_vectors.json 同源，改向量请两边同步
// 统一 7 字节行宽（不足补 0），实际长度按 LEN+3 取
const uint8_t VEC_ENCODE[][7] PROGMEM = {
    {0x7E, 0x03, 0x80, 0x3C, 0x64, 0x35, 0x00},  // note_on 60 vel100
    {0x7E, 0x02, 0x81, 0x3C, 0x17, 0x00, 0x00},  // note_off 60
    {0x7E, 0x03, 0x82, 0x07, 0x64, 0x8D, 0x00},  // cc7 val100
    {0x7E, 0x04, 0x83, 0x03, 0x00, 0x08, 0x8E},  // analog_out port3=2048
    {0x7E, 0x03, 0x84, 0x00, 0x01, 0xA7, 0x00},  // digital_out port0=1
    {0x7E, 0x01, 0x8F, 0xA4, 0x00, 0x00, 0x00},  // ping
};

// decode 组帧（设备→宿主），既用于发送也用于打印，宿主侧断言消息内容
const uint8_t VEC_DECODE[][7] PROGMEM = {
    {0x7E, 0x03, 0x10, 0x3C, 0x64, 0x9C, 0x00},  // note_on 60 vel100
    {0x7E, 0x02, 0x11, 0x3C, 0xF6, 0x00, 0x00},  // note_off 60
    {0x7E, 0x03, 0x20, 0x07, 0x64, 0x13, 0x00},  // cc7 val100
    {0x7E, 0x03, 0x30, 0x00, 0x40, 0x26, 0x00},  // pitchbend 居中
    {0x7E, 0x03, 0x40, 0x02, 0x01, 0xAB, 0x00},  // heartbeat uptime 258
    {0x7E, 0x04, 0x50, 0x02, 0xFF, 0x0F, 0xD0},  // analog_in port2=4095
    {0x7E, 0x03, 0x51, 0x07, 0x01, 0x23, 0x00},  // digital_in port7=1
};

const uint8_t VEC_DEBUG[] PROGMEM = {0x7E, 0x06, 0xF0, 0x68,
                                     0x65, 0x6C, 0x6C, 0x6F, 0xAD};  // "hello"

#define MFSP_SEND_OVER_WIRE 0   // 1 = 向量帧直接发上串口（链路核对）

void printHex(const uint8_t *p, uint8_t n) {
  for (uint8_t i = 0; i < n; i++) {
    if (p[i] < 0x10) Serial.print('0');
    Serial.print(p[i], HEX);
    if (i < n - 1) Serial.print(' ');
  }
  Serial.println();
}

void sendVector(const uint8_t *row) {
  uint8_t frame[7];
  memcpy_P(frame, row, 7);
  uint8_t n = frame[1] + 3;     // 完整帧长 = LEN + SYNC + LEN + CRC
  printHex(frame, n);
#if MFSP_SEND_OVER_WIRE
  Serial.write(frame, n);
  Serial.flush();
  delay(50);
#endif
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println(F("== MFSP selftest (device/mfsp_vectors.json) =="));

  for (auto &v : VEC_ENCODE) sendVector(v);   // encode 组
  for (auto &v : VEC_DECODE) sendVector(v);   // decode 组

  uint8_t dbg[9];
  memcpy_P(dbg, VEC_DEBUG, sizeof(VEC_DEBUG));
  printHex(dbg, sizeof(VEC_DEBUG));
#if MFSP_SEND_OVER_WIRE
  Serial.write(dbg, sizeof(VEC_DEBUG));
  Serial.flush();
#endif

  Serial.println(F("== done =="));
}

void loop() {}
