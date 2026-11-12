# MidiForge

一个 Windows 平台的双向 MIDI 监控、信号绑定与虚拟端口调试工具。

- 实时收发与解析 MIDI 消息，支持 note / CC / pitch bend / aftertouch / sysex 等
- 自动检测 loopMIDI / Virtual MIDI 虚拟端口
- 键盘 ↔ MIDI 信号绑定，JSON 持久化，每条可单独启用/停用
- 霓虹暗色主题，16 通道矩阵 + 消息日志 + 发送测试面板
- 事件总线驱动，支持 Python 插件和 headless SDK
- 8 个内置插件：打击垫、实时示波器、录制回放、钢琴帘练习、下落音符练习、实时合成器、SysEx 抓包、串口桥
- 发送 SysEx 带安全网关（黑名单硬拦截 + 高危告警）

> 更细的资料在 `docs/`：[API 参考](docs/API参考.md)（数据模型 / 事件总线 / 插件 API 全表）、[MIDI 协议详解](docs/MIDI协议详解.md)。

---

## 快速开始

### 前置条件

- **Python 3.11 或更高**（3.11 / 3.12 / 3.13 都行）
- 下载地址：https://www.python.org/downloads/windows/
- ⚠️ 安装时**务必勾选 "Add Python to PATH"**

### 方式一：一键启动（推荐）

双击 `start.bat`，首次运行会自动创建虚拟环境并安装依赖。

### 方式二：手动启动

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 安装依赖
.venv\Scripts\pip install -r requirements.txt

# 3. 运行
.venv\Scripts\python app.py
```

### 依赖

宿主主依赖（`requirements.txt`）：

| 包 | 版本 | 用途 |
|---|---|---|
| mido | ≥1.3.2 | MIDI 消息封装与解析 |
| python-rtmidi | ≥1.5.8 | 跨平台 MIDI I/O |
| PyQt6 | ≥6.7.0 | GUI 框架 |
| keyboard | ≥0.13.5 | 全局键盘监听 |
| pygame | ≥2.5 | pad_sentry 插件音频播放 |
| pytest / pytest-qt | — | 开发依赖，测试框架 |

插件附加依赖 —— 只影响对应插件，缺了它该插件会加载失败（面板里显示错误），宿主和其他插件照常工作：

| 包 | 用于 | 安装 |
|---|---|---|
| PyQt6-WebEngine | practice 插件（乐谱渲染） | `pip install PyQt6-WebEngine` |
| numpy | synth（DSP 内核）、piano_drift（音色合成） | `pip install numpy` |
| sounddevice | synth 插件（音频输出） | `pip install -r plugins/synth/requirements.txt` |
| pyserial | serial_bridge 插件（串口 ↔ MIDI 桥接） | `pip install -r plugins/serial_bridge/requirements.txt` |

---

## 使用指南

> 在以下的测试中，我使用的是我的 MIDI 键盘进行测试的，型号为KeyLab Essential 61 mk3

### 连接 MIDI 设备

1. 打开工具，顶部"输入设备"下拉选你的键盘或虚拟端口输入
2. "输出设备"下拉选需要接收消息的端口（如 loopMIDI 输出端口）
3. 右侧状态栏会高亮显示自动检测到的虚拟端口名称

> Windows 上 rtmidi 会给端口名追加索引号（如 `CYD-MIDI 0` / `CYD-MIDI 1`），工具会自动识别同 base 名的输入输出对作为虚拟端口。

### 实时监控

- **通道矩阵**：16 通道按 MIDI 1–16 对应，收到消息的通道会青色脉冲高亮
- **消息日志**：显示时间戳、来源、通道、消息类型、数值、原始 hex 字节、中文描述
- 点击"清空"按钮重置日志

### 发送测试

面板底部的发送测试区可快速发一条 MIDI 消息到输出端口：
- 类型下拉：`note_on` / `note_off` / `control_change` / `pitchwheel` / `program_change` / `poly_aftertouch` / `channel_aftertouch`
- 通道：0–15（日志中显示为 1–16）
- 参数根据类型自动切换（note + velocity / control + value / pitch 等）
- 点击青色"发送"按钮

### 信号绑定

绑定面板管理 MIDI / 键盘 → 自定义信号的映射关系：

| 列 | 说明 |
|---|---|
| 启用 | 开关。取消勾选后这条绑定彻底停止工作（不参与匹配、不执行动作），整行内容变灰 |
| 选 | 批量删除用的行选择框 |
| 信号名 | 自定义标签（如 `kick` / `solo_on`） |
| 匹配源 | MIDI：事件类型 + 通道 + 参数过滤；键盘：按键名。多个源之间是「或」关系 |
| 触发后动作 | `virtual_midi` 转发到虚拟端口 / `key_out` 模拟按键，可同时存在 |

- **新增 / 编辑 / 删除**：行级操作，双击某行也可直接编辑
- **启用 / 停用**：第一列开关，**即时生效、即时落盘**，不用重启。用来临时关掉某几个键的监听（比如只留播放/暂停，其余全停）
- **导入 / 导出 JSON**：方便跨设备同步
- **批量删除**：勾选「选」列的复选框后点删除按钮

配置文件保存在 `config/bindings.json`（首次运行自动创建空模板，损坏文件自动备份）。

### 插件管理

插件可插拔，改动**即时生效**，无需重启宿主：

- **标签右键**：在"插件"区域右键某标签 → 「停用此插件 / 启用此插件」。停用后标签变为灰显占位页（标题带 `[已停用]`），右键可重新启用
- **管理对话框**：菜单栏「插件 → 管理插件…」，列表含插件名 / 版本 / 状态 / 启用开关，勾选即时启停
- **持久化**：停用列表保存在 `config/plugins.json`，下次启动自动跳过这些插件的激活
- 重新启用时会**重建插件实例**（重新 `on_activate` + 重建面板），插件自身的配置（`data/config.json`）也会重新加载；加载失败的插件开关置灰不可启用

### 音频设置

菜单栏「设置 → 音频设置…」，可自定义音频输出，应用后**即时生效**：

- **输出设备**：枚举系统全部输出设备（声卡/虚拟声卡如 VB-Audio Cable），默认「系统默认」
- **驱动后端**：自动（推荐，失败自动降级）/ WASAPI / WinMM / DirectSound
- **采样率**：44100 / 48000 Hz；**缓冲区**：256–2048（越小延迟越低，越大越稳定）
- 应用时重建音频引擎，已加载的采样（打击垫、练习插件）**自动重新加载**，无需重启宿主
- 切换失败自动回退到原设置并提示原因；配置保存在 `config/audio.json`，下次启动直接生效

### 内置插件

插件放在 `plugins/` 下，宿主启动时自动发现。单个插件加载失败会被隔离成一条错误提示，不影响宿主和其他插件。

| 插件 | 作用 | 版本 | 完成度   |
|---|---|---|-----|
| pad_sentry | 2×4 打击垫可视化 + 采样发声 | v1.3 | 完成度最高，可以说基本完成了开发，可直接使用 |
| synth | 实时减法合成器（2 OSC + ADSR + 低通 + LFO） | v0.1 | 开发中，存在很多bug   |
| oscilloscope | 实时示波器：CC 热力图 / Pitch Bend 波形 / 触后曲线 | v0.1 | 完成度较高，有待完善             |
| recorder | 录制 MIDI 输入 → 保存 `.mid` → 回放 | v0.1 | 框架已搭好，功能待完善   |
| practice | 61 键钢琴帘 + 五线谱/简谱 + 半自动练习 | v0.2 | 完成度较高，仍有细节待完善   |
| piano_drift | 88 键下落音符练习：三模式 / 分声部 / A-B 循环 | v2.0 | 核心功能完整，细节待打磨   |
| sysex_editor | 只读 SysEx 抓包器（接收 / 分类 / 复制） | v0.2 | 开发中，但重心已不在此；仅作测试用途   |
| serial_bridge | 串口 ↔ MIDI 双向桥：自制硬件（Arduino/ESP32）经 UART 进入 MIDI 世界 | v0.2 | 透传 + MFSP 双协议、虚拟端口双向映射表可用 |

#### PadSentry 打击垫插件

2×4 可视化打击垫，监听 MIDI CC 102–109（对应多数 61 键键盘打击垫），踩 pad 时格子发光 + 播放鼓组采样。

**用法：**

1. 在顶部「输入设备」里打开你的键盘
2. 踩键盘上的打击垫（默认 CC 102–109），对应格子发光并播出采样；踩延音踏板同样会打底鼓
3. 音量分两级：底部滑杆是总音量，某个 pad 太响就右键它单独调

**音频特性：**

- 每 pad 独占 4 条 pygame channel，round-robin 轮换播放，**连打 / 同时踩永不丢音**
- 支持 FL Studio Edison 导出的 **Ogg Vorbis 嵌 WAV 格式**（format tag 0x674F），自动解包转 PCM
- 用户原始采样安全：解包前自动备份到 `samples_backup/` 目录，gitignore 排除不上传

**可调项（v1.3）：**

- 右下角总音量滑杆；**每个 pad 还能单独调音量**（右键 → "调整音量…"），最终音量 = 总音量 × pad 音量
- 右键 → "更换采样…" 换成自己的 wav，"恢复默认采样" 回到内置鼓组；刷新失败会自动回滚并提示原因
- 每个 pad 可配 `alt_cc` 作为备选触发 CC，典型用法是延音踏板（CC64）；`pedal_kick` 开关决定踏板是否打底鼓
- 状态栏显示采样就绪数与自选采样数量

**自定义采样：**

把 wav 丢进 `plugins/pad_sentry/samples/`，改 `plugins/pad_sentry/config.json` 的 `wav` 字段指向新文件名即可。

#### Synth 合成器插件

面板内嵌一个实时减法合成器，MIDI 键盘直接驱动，**只接收键盘发声、不对外转发**。全部代码自包含在 `plugins/synth/` 内。

**用法：**

1. 在顶部打开键盘输入，切到这个 Tab 直接弹就有声音（第一次按键会自动开音频流）
2. 音频设备默认自动挑一个能开流的；想指定就改「音频输出」下拉，或点「启动音频」手动开
3. 调音：旋钮支持拖拽 / 滚轮调节，按住 Shift 微调，双击复位；波形与 LFO 目标用下拉切换
4. 想把某个推杆绑到参数上：点该参数旁的「L」按钮，再推一下那个推杆即可（不会误绑延音踏板 CC64）
5. 「Panic」立刻释放所有声部；「恢复默认」一键回到初始参数

**面板内容：**

- 输出波形：实时显示当前发声波形，下方一行是声部数 / 峰值 / 丢帧 / 延音状态
- OSC1 / OSC2：波形、电平、失谐、八度、半音
- ENVELOPE：ADSR 图示 + 四个旋钮，转动旋钮图示跟着变
- FILTER：Cutoff / 谐振 / 包络量 / 键盘跟踪；LFO：波形、速率、深度、目标（Cutoff、音高、音量）
- MASTER：主音量、复音数（1–16）、弯音范围

「通道」下拉可以只让某个通道的键盘发声（默认全部）。音频设备空闲 10 秒自动释放，参数改动自动落盘到 `plugins/synth/config.json`（运行时生成，不入库）。

> 注意：合成器插件当前版本还有一些尚未修复的bug

#### Oscilloscope实时示波器插件

把 MIDI 输入实时画成图，用来看推杆推了多少、弯音和触后的走向，以及哪个键正被按住。只读显示，不发送任何消息。

**用法：**

1. 打开输入设备后切到这个 Tab，边弹边看
2. 想定格观察就勾「暂停」，画面冻结后可慢慢读；取消勾选恢复实时
3. 「窗口」下拉决定波形回看的时长（2s / 5s / 10s / 20s）
4. 「清空波形」把热力图和曲线一起清零（勾一下即清，自动弹回未勾选）

**画面分三块：**

- **Note 活动**：黑白琴键一带，按下的键整列点亮，力度越大越亮
- **CC 控制器热力图**：横轴是 CC 号、纵轴是通道，数值越大越亮
- **波形**：下方两条曲线，青色是 Pitch Bend、粉色是 Aftertouch

#### Recorder MIDI 录制 / 回放插件

录下 MIDI 输入、存成标准 `.mid`，或者把录到的事件直接回放出去。

**用法：**

1. 打开输入设备，点「● 录制」（按钮变「■ 停止」），期间收到的消息实时进列表
2. 再点一次停止录制，计时器停在总时长
3. 「保存 .mid」存文件（默认名 `recording.mid`，用「BPM」框里的值写 tempo）
4. 「加载 .mid」把文件读回列表，「▶ 回放」按「速度」百分比发到当前输出端口（100% 为原速）
5. 换一批材料就点「清空」

**说明：**

- 列表每行显示 时间 / 类型 / Channel / Details
- 录制会跳过虚拟回声与 SysEx（SysEx 交给抓包器）
- v1 的回放一旦开始不能中途打断，要等它放完；「速度」可调 1–400%

#### Practice钢琴帘练习插件

竖向钢琴帘 + 乐谱的练习器：音符往下落，跟着弹，实时判定打分。

**用法：**

1. 在顶部打开键盘输入，再选一个内置练习（「C大调音阶」「小星星」），或点「导入 MIDI」加载自己的曲子
2. 选「模式」：**自动下落**（音符自己往下走，跟着弹）/ **半自动识谱**（弹对当前音符才继续推进）
3. 选「谱面」：五线谱 / 简谱；拖「BPM」（60–220）与「飘速」调到合适难度
4. 点「开始」；中途可「暂停 / 继续」，想重来点「重置」
5. 底部状态栏显示提示与判定分数

**说明：**

- 上方是乐谱（VexFlow 渲染），下方是 61 键钢琴帘（C2–C7），中间的分隔条可以上下拖
- 练习时乐谱会跟着进度滚动并高亮当前小节
- 依赖 `PyQt6-WebEngine` 渲染乐谱，没装的话插件会加载失败

#### PianoDrift 下落音符练习插件

横版下落音符练习器（旧 practice 插件的 2.0）：音符像音游一样落到 88 键键盘上，落线的瞬间按下对应的键。三种练习模式覆盖从跟弹到识谱，伴奏声部会自动跟着响。

**用法：**

1. 点「打开 MIDI…」加载自己的曲子，或从「示范曲…」选内置小曲（小星星 / C 大调音阶）
2. 选「模式」：**跟随演奏**（曲子照常响，跟着弹）/ **等待按对**（弹对当前一组音才继续走，适合识谱）/ **试听所选声部**（纯听，不判定）
3. 选「声部」：整首乐曲 / 只练高音（C4 及以上）/ 只练低音 / 某个轨道；没选中的部分当伴奏，播放时照常出声
4. 要磨某段乐句就勾「A-B 循环」，用「A=此处 / B=此处」圈出区间，再调「速度」（25%–150%）慢练
5. 点「▶ 播放」，两拍预备倒计时后开始；右上角实时显示正确 / 漏掉 / 准确率，循环时每一遍独立计分

**弹奏方式：**

- MIDI 键盘直弹；没有键盘时用电脑键盘也行：`a w s e d f t g y h u j k o l p ;` 对应 C4–E5
- 按住 Shift 是临时延音踏板，延音踏板（CC64）同样生效；松键后的余音时长由「延音」滑杆控制
- 空格 = 播放 / 暂停，Esc = 全部静音，点顶部进度条任意位置可跳转，点琴键也能出声

**说明：**

- 88 键全覆盖（A0–C8），不同轨道的音符用不同颜色，伴奏声部半透明
- 内置三角钢琴 / 电钢琴 / 八音盒三种音色，第一次弹到某个键才合成该音的采样（毫秒级，无感）
- 「等待按对」模式下画面会停住，待按键的琴键带绿色描边，弹对才继续

#### SysEx 抓包器插件

只读地把设备发来的 SysEx 消息抓下来，适合逆向厂商私有消息。**不提供发送入口**，主动发 SysEx 走 SDK 的 [`send_sysex()` 安全网关](#sysex-安全网关)。

**用法：**

1. 打开输入设备后切到这个 Tab，收到的 SysEx 自动进「接收抓包」列表
2. 表格列：时间 / 摘要 / 风险 / 字节数 / hex
3. 选中一行点「复制 hex」（双击行同效），或「导出 CSV」存档（默认名 `sysex_capture_日期时间.csv`）
4. 想暂停抓取勾「暂停」；「清空」清掉列表

**说明：**

- 「风险」列来自平台的安全网关分级，高危厂商消息会标出来
- 列表最多保留最近 500 条（`capture_max`，配置在 `plugins/sysex_editor/data/config.json`）
- 该插件仅作测试用途，后续开发重心不在这里

#### SerialBridge 串口桥插件

自制硬件往往只有一个 UART 串口，这个插件就是它和 MIDI 之间的桥。Arduino、ESP32、STM32 做的开发板合成器、DIY 控制器，插上 USB 就能参与 MidiForge 的所有活动。协议有两种，面板下拉即可切换：

- **透传**。串口上跑标准 MIDI 1.0 字节流（默认 115200 波特），与 Hairless MIDI-Serial Bridge 兼容。设备端用 [Arduino MIDI Library](https://github.com/FortySevenEffects/arduino_midi_library) 或 espMIDI 这类现成库就行，不需要自定义协议
- **MFSP 帧协议**。每帧带 CRC8 校验，1 秒一次心跳用来判断设备是否在线，另有 debug 文本通道和虚拟端口（analog/digital 各 0-15）。设备端引入插件自带的 `plugins/serial_bridge/device/mfsp_arduino.h`（header-only）即可

**用法：**

1. 先装依赖 `pip install -r plugins/serial_bridge/requirements.txt`，设备 USB 插好后切到插件 Tab
2. 选串口（热插拔后点「刷新」）、选波特率（默认 115200）、选协议，然后点「连接」
3. 勾上「串口 → MIDI」，设备发来的音符和 CC 会走统一分发，示波器可视化、录制器录 `.mid`、绑定触发、synth 发声都不需要再配置
4. 勾上「MIDI → 串口」则反过来，由宿主驱动设备，比如让设备的 LED 跟着 CC 变化

**MFSP 虚拟端口与双向映射表：**

- 固件只认端口号（analog 0-15，值 0-4095；digital 0-15，开关量），完全不涉及 MIDI 概念。翻译工作在电脑端做：入向映射表把端口转成 CC/音符，出向映射表把 CC/音符转成 analog/digital 输出。想换映射改表格就行，固件不用动
- MFSP 下状态行显示设备心跳，3 秒没有心跳就判定离线；设备的 debug 文本会以 `»` 前缀出现在监视区
- 映射换算规则：入向 analog（0-4095）转 CC 按 12bit 压到 7bit，digital 按边缘触发；出向 CC（0-127）转 analog 左移 5 位（相当于乘 32），CC≥64 转 digital 为开，note 力度转 analog 电平

**实战示例：ESP32 推杆调光板载 LED（已真机验证）**

1. 用 Arduino IDE 打开 `plugins/serial_bridge/device/examples/mfsp_minimal_single.ino`。协议实现已经内联，单文件就能编译，开发板选 ESP32 Dev Module，然后 Upload
2. 串口桥面板选板子的 COM 口、115200、协议 MFSP，点连接。状态行应显示「在线 · 心跳: 正常」，监视区会出现 `» mfsp ready` 和每秒一帧的 `7E 04 40 …` 心跳
3. 勾「MIDI → 串口」，在出向映射表加一行「启用 ✓ · CC · 编号 73 · → 模拟 · 端口 0」。73 请换成你键盘推杆实际的 CC 号，推一下推杆，在主界面日志里就能看到
4. 再推推杆，监视区应出现 `← 7E 04 83 …`（analog_out 帧），ESP32 板载 LED 亮度随之线性变化，拉到底熄灭，推到底最亮
5. 同一份固件也能反过来用：A0 接电位器，入向映射「模拟 端口0 → CC」，就是一个 DIY MIDI 旋钮；GPIO4 接按键（接 GND），映射「数字 端口0 → 音符」，就是一个 DIY 打击垫

**无硬件自检：** 串口下拉选 `loop://`，连接后勾双向，弹宿主键盘。监视区应同时出现 `←` 出站字节和 `→` 回环进站；MFSP 下还能验证心跳/ping 闭环，全程不需要真实设备。

**故障排查：**

- **插上板子没有新 COM 口**：经典 ESP32 走板载 USB 转串芯片，装 CP210x（Silicon Labs）或 CH340 驱动；S2/S3/C3 原生 USB-CDC 免驱，显示为「USB 串行设备」
- **连接瞬间监视区有乱码/解码错误**：属正常现象，那是 ESP32 的启动日志（ROM bootloader 输出），重同步状态机会自动找回帧头
- **连接失败/端口被占用**：Arduino IDE 的串口监视器、其他串口工具会独占 COM 口，先关掉再连
- **推杆推了但监视区没有 `←` 帧**：依次检查「MIDI → 串口」是否勾选、出向映射编号是否与推杆 CC 号一致、相关插件是否已启用
- **怀疑固件端协议坏了**：烧 `device/examples/mfsp_selftest.ino` 跑设备端逐字节自检（与 pytest 共用同一份 `mfsp_vectors.json` 测试向量）

**说明：**

- 「通道改写」可以把设备发来的消息统一改写到指定 MIDI 通道（默认保持原样；MFSP 没有通道概念，默认 ch0）。DIY 设备通道乱飞的时候，用它对绑定会方便很多
- 十六进制监视区保留最近 200 行收发字节（`→` 表示串口进，`←` 表示串口出）。串口断流残留的垃圾字节会计入「解码错误」，随后自动重对齐
- 连接期间掉线会自动重连（1 秒起步，指数退避到 8 秒）；配置（含映射表）自动保存到 `plugins/serial_bridge/config.json`
- 有防回环保护：设备自己发出的消息、宿主虚拟端口的回声，都不会再转发回串口
- 设备端对接材料（接线、帧格式表、Arduino 示例、共享测试向量）都在 `plugins/serial_bridge/device/README.md`
- 真机验证看两个现象就够：设备发 `note_on`，pad_sentry 应该出声；宿主弹键盘，监视区应出现 `←` 字节

<br />

***

## SDK —— Headless 模式

不启动 GUI，直接用 Python 订阅 MIDI 事件：

```python
import api

app = api.create_app()

@api.on("midi.message")
def on_msg(parsed):
    print(f"[{parsed.type}] ch={parsed.channel} values={parsed.values} hex={parsed.raw_hex}")

app.engine.open_input("CYD-MIDI 0")
app.start()
# ... 持续监听
app.stop()
```

运行示例：

```bash
python examples/sdk_demo.py "CYD-MIDI 0"
```

### 事件总线 Topic

| Topic | 载荷 | 触发时机 |
|---|---|---|
| `midi.all` | `ParsedMessage` | 收到并解析后的每条 MIDI（`midi.message` 是它的别名） |
| `midi.note_on` / `midi.note_off` / `midi.cc` / `midi.pitch_bend` / `midi.aftertouch` / `midi.sysex` | `ParsedMessage` | 按消息类型细分的 topic，只关心某一类时订这个 |
| `midi.signal` | `{"signals": set, "parsed": ParsedMessage}` 或 `{"signals": set, "key": str}` | Matcher 匹配到绑定时 |
| `midi.sent` | `{"type", "channel", "values"}`；SysEx 另带 `level` / `risk` / `blacklist_hit` | 主动发送一条消息后 |
| `key.pressed` | `str` — 归一化按键名 | 键盘监听回调 |
| `binding.changed` | `{"action": "add"/"remove", "signal"}` 或 `None` | 绑定增删改后 |
| `binding.action` | `{"signals": [...], "actions": [...]}` | 命中绑定的动作执行后 |

### AppContext 聚合对象

`api.create_app(bindings_path=...)` 返回的 `AppContext`（不传 `bindings_path` 时用默认 `config/bindings.json`）：

**核心成员**

- `engine: MidiEngine` — MIDI 端口引擎
- `bus: EventBus` — 事件总线
- `config: BindingConfig` — 绑定配置
- `matcher: Matcher` — 匹配器

**常用方法**

- `send_midi(type, channel, **kw)` / `send_sysex(data, safe=True)` — 发送消息，SysEx 走安全网关
- `open_input(name)` / `close_inputs()` — 打开、关闭输入端口
- `start()` / `stop()` — 起停后台轮询线程
- `subscribe(topic, cb)` — 订阅事件；模块级 `@api.on(topic)` 装饰器是它的语法糖
- `execute_actions(signals)` — 手动执行一组信号对应的动作（跳过停用的绑定）
- `save_bindings()` — 把绑定配置落盘
- `data_dir(plugin_name)` — 取插件私有数据目录（自动创建）

**子 API**

| 子 API | 用途 |
|---|---|
| `state` | 运行时快照：`pressed_notes()` / `is_pressed()` / `latest_message()` / `recent_messages()` |
| `log` | 日志：`debug` / `info` / `warning` / `error` / `critical` |
| `bindings` | 绑定运行时读写：`get_all()` / `find_by_signal()` / `add()` / `remove()` / `trigger_signal()` |
| `audio` | pygame 采样播放：`load()` / `play()` / `stop_all()` / `set_master_volume()` / `allocate_pool()` |
| `clock` | 节拍时钟：`bpm` / `beat` / `bar` / `tap()` |
| `midi_file` | MIDI 文件：`load()` / `save()` / `play()` |

### SysEx 安全网关

`app.send_sysex(payload)` 默认开三道检查，防止误发厂商私有消息把设备写坏：

1. **黑名单硬拦截** —— 命中危险地址直接抛 `RuntimeError`，一个字节都不发
2. **高危告警** —— 高风险操作照发，但记 warning 日志，并在 `midi.sent` 里带上 `level` / `risk` / `blacklist_hit`
3. **显式绕过** —— `send_sysex(payload, safe=False)` 跳过全部检查，只留给固件刷写这类明确知道风险的场景

配套的 `sysex_editor` 插件是**只读**抓包器（接收 / 分类 / 复制），不提供发送入口——主动发消息统一走上面的网关。

---

## 插件开发

在 `plugins/` 目录下新建目录和 `plugin.py`，继承 `core.plugin.Plugin`：

```python
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget
from core.plugin import Plugin
from ui import style_qss as QSS


class NoteSentry(Plugin):
    name = "note_sentry"

    def on_activate(self, app):
        self.app = app
        app.on("midi.message", self.on_message)

    def on_message(self, parsed):
        if hasattr(self, "label"):
            self.label.setText(f"最后消息: {parsed.type} ch{parsed.channel + 1}")

    def create_panel(self):
        box = QWidget()
        v = QVBoxLayout(box)
        self.label = QLabel("等待 MIDI 消息…")
        v.addWidget(self.label)
        return box

    def on_deactivate(self):
        pass
```

插件宿主会自动发现、隔离加载失败的插件、在 UI 插件 Tab 中展示。

可参考 `examples/example_plugin/` 中的完整示例。基类上还有几个描述性字段，填了会显示得更清楚：

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | `""` | 插件名（也是数据目录名） |
| `version` | `"0.1"` | 版本号 |
| `status` | `"stable"` | `stable` / `dev` / `deprecated`，后两者在 Tab 标题上显示 `[开发中]` / `[已废弃]` |

`create_panel()` 返回 `None` 表示这是个无界面的纯后台插件。

---

## 项目结构

```
midi/
├── app.py                        # GUI 入口
├── api.py                        # SDK：AppContext + 事件总线装饰器
├── start.bat           # Windows 一键启动
├── requirements.txt              # 宿主主依赖
├── requirements-dev.txt          # 开发依赖（含 pytest + pytest-qt）
├── fl.json                       # 作者的 FL Studio 键位绑定（定制配置，见下）
│
├── midi/                         # MIDI 底层
│   ├── parser.py                 # ParsedMessage + mido → 统一字典解析
│   ├── engine.py                 # 端口枚举 / 收发引擎
│   ├── virtual_port.py           # 虚拟端口检测
│   ├── sysex.py                  # SysEx 安全网关（黑名单 / 风险分级）
│   └── keyboard_input.py         # 键盘名归一化 + 全局监听
│
├── core/                         # 业务逻辑
│   ├── events.py                 # EventBus（线程安全）
│   ├── bindings.py               # Binding / BindingSource / BindingConfig
│   ├── matcher.py                # MIDI + 键盘 → 信号匹配
│   ├── audio.py                  # 采样播放服务（pygame，多后端降级）
│   ├── clock.py                  # BPM / 节拍时钟
│   ├── midi_file.py              # MIDI 文件读写与回放
│   ├── plugin.py                 # Plugin 抽象类
│   └── plugin_loader.py          # 插件自动发现与宿主
│
├── ui/                           # PyQt6 界面组件
│   ├── main_window.py            # 主窗口组装
│   ├── port_panel.py             # 端口选择 + 虚拟端口状态
│   ├── channel_matrix.py         # 16 通道脉冲矩阵
│   ├── log_view.py               # 实时日志表格
│   ├── send_panel.py             # 发送测试面板
│   ├── bindings_view.py          # 绑定编辑器
│   ├── plugin_tabs.py            # 插件面板 Tab 容器
│   └── style_qss.py              # 霓虹暗色主题
│
├── plugins/                      # 内置插件（一个目录一个插件）
│   ├── pad_sentry/               # 打击垫
│   ├── synth/                    # 实时合成器
│   ├── oscilloscope/             # 实时示波器
│   ├── recorder/                 # 录制 / 回放
│   ├── practice/                 # 钢琴帘练习
│   ├── piano_drift/              # 88 键下落音符练习
│   └── sysex_editor/             # SysEx 抓包器
│
├── examples/
│   ├── sdk_demo.py               # Headless SDK 用法
│   └── example_plugin/           # 插件模板
│
├── docs/                         # API 参考、MIDI 协议详解
└── tests/                        # pytest 测试
```

### 架构概览

```
 ┌─────────┐  端口枚举  ┌────────────┐  mido 回调   ┌──────────┐
 │ Port UI │──────────▶│ MidiEngine │──────────────▶│  parser  │
 └─────────┘           └──────┬─────┘               └────┬─────┘
                               │ send_message()            │ ParsedMessage
                               ▼                           ▼
                          ┌──────────┐              ┌────────────┐
                          │  mido     │              │  EventBus  │
                          └──────────┘              └──────┬─────┘
                                                           │ publish topics
                                                   ┌───────┴──────┐
                                                   ▼              ▼
                                              ┌──────────┐  ┌──────────┐
                                              │ Matcher  │  │ Plugin   │
                                              │ +Binding │  │ Host     │
                                              └────┬─────┘  └────┬─────┘
                                                   │ signal       │ create_panel()
                                                   ▼              ▼
                                              ┌────────────────────┐
                                              │     UI / 插件面板    │
                                              └────────────────────┘
```

数据流：**MIDI / 键盘 → EventBus → Matcher / Plugin → UI 更新**。Engine 只负责 I/O 和原始消息，业务逻辑和 UI 通过 EventBus 解耦。

---

## 配置

### 绑定文件

`config/bindings.json`，格式：

```json
{
  "version": 1,
  "bindings": [
    {
      "signal": "kick",
      "sources": [
        { "type": "midi", "channel": 9, "event": "note_on", "note": 36 }
      ],
      "virtual_midi": {
        "output_port": "CYD-MIDI 1",
        "channel": 0
      }
    }
  ]
}
```

`enabled` 字段只在**停用**时出现（`"enabled": false`）。没有该字段即为启用，所以旧配置文件无需改动。停用的绑定不参与匹配、也不执行动作，可在界面「信号绑定」列表里用第一列的开关随时切换。

### 项目自带的定制化配置

以下文件属于**针对特定硬件/软件的个性化定制**，作为项目随仓库保存，不属于 MidiForge 通用配置：

- `fl.json` — 针对作者的 **KL Essential 61 mk3 MIDI 键盘 + FL Studio 20** 整理的播放键位绑定（play/stop/rec/cycle/next bar 等）。换用其他 DAW 或键盘后需自行重建。
- `plugins/pad_sentry/config.json` — 打击垫插件的 pad → 采样映射（8 个 pad 的 CC 号、显示名、wav 文件名）。

### 虚拟端口检测策略

按优先级尝试：
1. 端口名包含 `loopMIDI` / `Virtual` 关键字（标准 loopMIDI / VirtualMIDISynth）
2. 同 base 名的输入/输出端口对（通用兼容，适配用户自定义的 CYD-MIDI、MIDI-PORT 等）

Windows rtmidi 自动附加的索引号后缀会被自动剔除再比较。

---

