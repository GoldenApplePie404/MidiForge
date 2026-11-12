# 设备端对接指南（serial_bridge/device/）

让 Arduino / ESP32 / STM32 等只有 UART 串口的板子接入 MidiForge 串口桥。
所有内容都是**参考实现**：透传协议用现成库即可，MFSP 想自己实现也只需要
照 [mfsp_vectors.json](mfsp_vectors.json) 对齐字节。

## 两种协议怎么选

| | 透传（默认） | MFSP |
|---|---|---|
| 串口上跑什么 | 标准 MIDI 1.0 字节流 | MFSP 帧协议（见下） |
| 设备端成本 | 零自定义协议：[Arduino MIDI Library](https://github.com/FortySevenEffects/arduino_midi_library) / espMIDI 现成库 | 引入 `mfsp_arduino.h`（header-only，~150 行） |
| 校验 / 心跳 | 无（靠 MIDI 字节流自同步） | CRC8 每帧校验 + 1s 心跳在线判定 |
| 传感器 / 执行器 | 自己塞进 CC/note 语义 | 虚拟端口原样上报，映射在电脑端配 |
| 调试输出 | 无 | debug 文本通道（宿主监视区显示） |
| 示例 | [examples/passthrough_midi_library.ino](examples/passthrough_midi_library.ino) | [examples/mfsp_minimal_single.ino](examples/mfsp_minimal_single.ino)（协议实现内联的单文件版，开箱即编译；ESP32 引脚已适配：A0 电位器 / GPIO4 按键 / GPIO2 LED 调光） |

只有旋钮按钮当 MIDI 控制器用 → 透传最省事；要接传感器/执行器、
要看设备运行状态、要 CRC 可靠性 → 用 MFSP。

## 接线与串口设置

- USB 线直连（Leonardo/Micro 除外，见下），或 USB-TTL 模块：模块 TX → 板 RX、
  模块 RX → 板 TX（交叉）、GND 共地
- 波特率 **115200**（两边一致即可，示例统一 115200）
- MidiForge 侧：插件面板选串口 → 波特率 → 协议 → 连接
- 5V / 3.3V 电平不要跨压直连 UART

## MFSP 帧协议 v1

```
| SYNC 0x7E | LEN | CMD | PAYLOAD(0-32B) | CRC8 |
             LEN 覆盖 CMD+PAYLOAD    CRC8: 多项式 0x07，初值 0x00
```

CRC-8 标准检查值：`crc8("123456789") == 0xF4`（[mfsp_arduino.h](mfsp_arduino.h)
与插件端 `_codec.py` 同一实现）。

**设备→宿主：**

| CMD | 载荷 | 语义 |
|---|---|---|
| 0x10 | note, velocity | 音符开 |
| 0x11 | note | 音符关 |
| 0x20 | cc, value | CC |
| 0x30 | lsb, msb | 弯音（14bit = lsb \| msb<<7，-8192..8191） |
| 0x40 | uptime_lo, uptime_hi | 心跳（秒），宿主 3s 没收到显示「心跳超时」 |
| 0x50 | port, val_lo, val_hi | 模拟虚拟端口 0-15，值 0-4095 小端 |
| 0x51 | port, state | 数字虚拟端口 0-15，state 0/1 |
| 0xF0 | UTF-8 文本 ≤32B | 调试文本（宿主监视区 `»` 前缀显示） |

**宿主→设备：**

| CMD | 载荷 | 语义 |
|---|---|---|
| 0x80 / 0x81 / 0x82 | 同上 note/cc | 音符开 / 关 / CC |
| 0x83 | port, val_lo, val_hi | 模拟输出（LED 亮度 / 舵机…），0-4095 小端 |
| 0x84 | port, state | 数字输出 |
| 0x8F | （空） | ping → 设备应回一帧 0x40 心跳 |

约定：
- **MIDI 通道不进协议**：宿主面板「通道改写」统一配置（默认 ch0），固件不用关心
- 心跳建议 1s 一帧；收到 ping 回一帧心跳
- 单帧负载 ≤32 字节，长文本分帧发

## 虚拟端口：固件只认端口，语义在电脑端配

设备端只报「analog 端口 N 读数 0-4095」「digital 端口 N 开关」，
**不出现任何 MIDI 概念**。翻译在宿主面板「双向映射表」完成：

- 入向（`map_in`）：`analog→CC`（0-4095 右移 5 成 0-127，连续流）、
  `analog→音符`（≥2048 视为按下，边缘触发）、`digital→音符/CC`（变化才发）
- 出向（`map_out`）：`CC→analog`（0-127 左移 5）、`CC→digital`（≥64 为 1）、
  `音符→digital`（on/off）、`音符→analog`（力度作电平）。命中映射的消息
  只发映射帧，未命中的 note/cc 走 0x80/0x81/0x82 标准帧直通

换映射改电脑端表格即可，**固件零改动**——这是相对商业桥接器的核心差异。

## 验证

1. **无板自检**：`mfsp_selftest.ino` 烧录后串口监视器 115200 打开，逐行
   十六进制与 [mfsp_vectors.json](mfsp_vectors.json) 对照（pytest 端同一组
   向量在 `tests/test_serial_bridge_mfsp.py` 断言，两边同源）
2. **虚拟链路**：MidiForge 面板串口选 `loop://`（pyserial 虚拟回环）可验证
   宿主侧收发闭环，不需要硬件
3. **真机联调清单**：
   - 设备发 `0x10 note_on` → pad_sentry 出声、示波器可见、录制器录到
   - 旋钮 → analog_in + 入向映射 → CC 连续变化
   - 按钮接 digital_in + 入向映射 → 音符开关
   - 勾「MIDI → 串口」+ 出向映射 → 宿主弹键盘，设备 LED / 执行器动作
   - 状态行「心跳: 正常」；拔掉串口线 → 自动重连、心跳转「超时」
