"""SysEx 纯逻辑库：hex 编解码 / 校验和 / 厂商表 / 风险分类 / 黑名单判定。

无 Qt / mido 端口依赖，可独立单测。
约定：payload = 不含 F0/F7 的数据字节列表（mido sysex 约定）。
"""

from typing import Callable, Dict, List, Optional, Tuple

# ============================================================
# 风险等级
# ============================================================
RISK_OK = 0
RISK_WARN = 1
RISK_HIGH = 2

RANK_TEXT = {RISK_OK: "正常", RISK_WARN: "警告", RISK_HIGH: "高危"}
RANK_LEVEL = {RISK_OK: "ok", RISK_WARN: "warn", RISK_HIGH: "high"}


# ============================================================
# Hex 编解码
# ============================================================

def hex_str_to_bytes(s: str) -> List[int]:
    """hex 字符串（空格/逗号/换行分隔）→ 字节列表。非法输入抛 ValueError。"""
    tokens = [
        t for t in s.replace(",", " ").replace("\n", " ").replace("\r", " ").split(" ")
        if t.strip()
    ]
    if not tokens:
        raise ValueError("内容为空")
    out = []
    for t in tokens:
        t = t.strip()
        if len(t) != 2 or any(c not in "0123456789abcdefABCDEF" for c in t):
            raise ValueError(f"非法 hex: {t!r}（应为两位十六进制，如 41）")
        out.append(int(t, 16))
    if any(b > 127 for b in out):
        raise ValueError("包含 >0x7F 的字节，MIDI 数据字节范围是 0x00-0x7F")
    return out


def bytes_to_hex_str(data: List[int], wrap_f0f7: bool = False) -> str:
    """字节列表 → "41 10 42" 空格分隔的 hex 串。wrap_f0f7=True 时补 F0/F7。"""
    if wrap_f0f7:
        data = [0xF0] + list(data) + [0xF7]
    return " ".join(f"{b:02X}" for b in data)


# ============================================================
# 校验和注册表
# ============================================================

CHECKSUM_ALGORITHMS: Dict[str, Callable[[List[int]], int]] = {}


def register(name: str):
    def deco(fn):
        CHECKSUM_ALGORITHMS[name] = fn
        return fn
    return deco


@register("roland")
def _roland_checksum(data: List[int]) -> int:
    """Roland 通用校验和：checksum = (128 - (sum(data) % 128)) % 128。"""
    return (128 - (sum(data) % 128)) % 128


def apply_checksum(payload: List[int], algo: str = "roland") -> List[int]:
    """把 payload 的最后一个字节替换为算法算出的校验和。"""
    if algo not in CHECKSUM_ALGORITHMS:
        raise ValueError(f"未知校验算法: {algo}")
    if not payload:
        raise ValueError("payload 为空，无法应用校验和")
    out = list(payload)
    out[-1] = CHECKSUM_ALGORITHMS[algo](payload[:-1])
    return out


def verify_checksum(payload: List[int], algo: str = "roland") -> bool:
    """校验 payload 最后一个字节是否等于算法值（需 payload 至少 2 字节）。"""
    if algo not in CHECKSUM_ALGORITHMS:
        raise ValueError(f"未知校验算法: {algo}")
    if len(payload) < 2:
        return False
    return payload[-1] == CHECKSUM_ALGORITHMS[algo](payload[:-1])


# ============================================================
# 厂商表
# ============================================================

# key 为 tuple（兼容 1/2/3 字节厂商 ID）
VENDOR_MAP: Dict[Tuple[int, ...], str] = {
    (0x40,): "Kawai",
    (0x41,): "Roland",
    (0x42,): "Korg",
    (0x43,): "Yamaha",
    (0x44,): "Casio",
    (0x45,): "Akai",
    (0x46,): "MOTU",
    (0x4A,): "Alesis",
    (0x4C,): "Zoom",
    (0x00, 0x1D): "Alesis (2字节)",
    (0x00, 0x20, 0x33): "Behringer",
    (0x00, 0x00, 0x41): "Roland (3字节扩展)",
}


def vendor_of(payload: List[int]) -> Optional[str]:
    """从 payload 开头解析厂商 ID（1/2/3 字节），返回厂商名或 None。"""
    if not payload:
        return None
    # 3 字节扩展 ID：0x00 0x00 xx
    if len(payload) >= 3 and payload[0] == 0x00 and payload[1] == 0x00:
        return VENDOR_MAP.get((payload[0], payload[1], payload[2]))
    # 2 字节扩展 ID：0x00 xx
    if len(payload) >= 2 and payload[0] == 0x00:
        return VENDOR_MAP.get((payload[0], payload[1]))
    # 1 字节
    return VENDOR_MAP.get((payload[0],))


def vendor_id_of(payload: List[int]) -> Optional[Tuple[int, ...]]:
    """返回匹配到的厂商 ID 元组（1/2/3 字节），用于黑名单匹配。"""
    if not payload:
        return None
    if len(payload) >= 3 and payload[0] == 0x00 and payload[1] == 0x00:
        return (payload[0], payload[1], payload[2])
    if len(payload) >= 2 and payload[0] == 0x00:
        return (payload[0], payload[1])
    return (payload[0],)


# ============================================================
# 风险分类
# ============================================================

# 通用 SysEx（首字节 0x7E=非实时，0x7F=实时）前缀规则（按序匹配，长前缀优先）
# 匹配基于 payload 前缀（不含 F0/F7），设备号 0x7F = ALL DEVICES
# 参考 MIDI 1.0 Detailed Specification v4.2 Universal SysEx 分配表
GENERIC_RULES: List[Tuple[Tuple[int, ...], int, str]] = [
    # ============================================================
    # 0x7E Universal Non-Real-Time System Exclusive
    # F0 7E <dev> <sub-ID1> <sub-ID2> ... F7
    # ============================================================

    # ---- sub-ID1=0x00 General Information（中性，只读查询）----
    ((0x7E, 0x7F, 0x00, 0x01), RISK_OK, "Device Identity Request"),
    ((0x7E, 0x7F, 0x00, 0x02), RISK_OK, "GM System Name Query/Response"),
    ((0x7E, 0x7F, 0x00, 0x03), RISK_OK, "GM System Inquiry (通用)"),
    ((0x7E, 0x7F, 0x00, 0x04), RISK_OK, "General MIDI System 查询"),
    ((0x7E, 0x00, 0x00),       RISK_OK, "通用信息查询（设备特定）"),

    # ---- sub-ID1=0x01 Device Inquiry — 固件/程序区 高危 ----
    # Request/Response: 中性查询
    ((0x7E, 0x7F, 0x01, 0x01), RISK_OK,   "Device Inquiry Request (查询)"),
    ((0x7E, 0x7F, 0x01, 0x02), RISK_OK,   "Device Inquiry Response (回应)"),
    # Load 系列 (0x03-0x05): 写入设备，最危险
    ((0x7E, 0x7F, 0x01, 0x03), RISK_HIGH, "Device Inquiry Load/Start (固件写入)"),
    ((0x7E, 0x7F, 0x01, 0x04), RISK_HIGH, "Device Inquiry Load/Data (固件写入)"),
    ((0x7E, 0x7F, 0x01, 0x05), RISK_HIGH, "Device Inquiry Load/End (固件写入)"),
    # Dump 系列 (0x06-0x08): 只读从设备取出，偏中性
    ((0x7E, 0x7F, 0x01, 0x06), RISK_OK,   "Device Inquiry Dump Request"),
    ((0x7E, 0x7F, 0x01, 0x07), RISK_OK,   "Device Inquiry Dump/Data"),
    ((0x7E, 0x7F, 0x01, 0x08), RISK_OK,   "Device Inquiry Dump/End"),
    # 1 字节兜底（未知 Device Inquiry 子命令）
    ((0x7E, 0x7F, 0x01),       RISK_WARN, "Device Inquiry 未识别子命令"),

    # ---- sub-ID1=0x02 Sample Dump Standard — 采样转储 警告 ----
    ((0x7E, 0x7F, 0x02),       RISK_WARN, "Sample Dump Standard (采样转储)"),

    # ---- sub-ID1=0x04 MIDI Time Code — 中性 ----
    ((0x7E, 0x7F, 0x04),       RISK_OK, "MIDI Time Code"),

    # ---- sub-ID1=0x05 File Dump — 警告 ----
    ((0x7E, 0x7F, 0x05),       RISK_WARN, "File Dump (文件转储)"),

    # ---- sub-ID1=0x06 General Information Bulk Dump ----
    # GM System Inquiry: sub-ID2=0x01 请求, 0x00 回应（最常见）
    ((0x7E, 0x7F, 0x06, 0x01), RISK_OK, "GM System Inquiry 请求"),
    ((0x7E, 0x7F, 0x06, 0x00), RISK_OK, "GM System Inquiry 回应"),
    ((0x7E, 0x7F, 0x06, 0x02), RISK_WARN, "File Bulk Dump"),
    ((0x7E, 0x7F, 0x06),       RISK_WARN, "General Information Bulk Dump"),

    # ---- sub-ID1=0x08 Tuning Standard — 调音相关 中性 ----
    ((0x7E, 0x7F, 0x08, 0x01), RISK_OK, "Bulk Dump Tuning Request"),
    ((0x7E, 0x7F, 0x08, 0x02), RISK_OK, "Bulk Dump Tuning Reply"),
    ((0x7E, 0x7F, 0x08),       RISK_OK, "Tuning Standard"),

    # ---- sub-ID1=0x09 GM Tuning Dump / Scale/Octave Tuning ----
    ((0x7E, 0x7F, 0x09),       RISK_OK, "GM Tuning Dump"),

    # ---- sub-ID1=0x0A General MIDI 2 System ----
    ((0x7E, 0x7F, 0x0A),       RISK_OK, "General MIDI 2 System"),

    # ---- 兜底 ----
    ((0x7E, 0x00),             RISK_WARN, "未知通用 SysEx (设备特定)"),
    ((0x7E, 0x7F),             RISK_WARN, "未知通用 SysEx (ALL DEVICES)"),

    # ============================================================
    # 0x7F Universal Real-Time System Exclusive
    # F0 7F <dev> <sub-ID1> <sub-ID2> ... F7
    # ============================================================
    ((0x7F, 0x7F, 0x04),       RISK_HIGH, "Universal Real-Time 固件升级 (0x7F 7F 04)"),
    ((0x7F, 0x7F),             RISK_WARN, "Universal Real-Time SysEx (ALL DEVICES)"),
    ((0x7F,),                  RISK_WARN, "Universal Real-Time SysEx"),
]

# Roland 专用规则（payload 结构: 41 dev cmd ...）
_ROLAND_RULES: List[Tuple[Tuple[int, ...], int, str]] = [
    ((0x41,), RISK_OK, "Roland 系统消息"),          # 兜底：41 开头中性
]


def classify(payload: List[int]) -> Tuple[int, str, bool]:
    """风险分类。返回 (level, reason, vendor_known)。

    规则：通用 SysEx 按 GENERIC_RULES 前缀表判定；
    厂商特有先查黑名单（在 plugin 层做，这里只做基础分类），61 开头等
    未知厂商 → OK 中性（绝不猜测为高危）。
    """
    if not payload:
        return RISK_OK, "空消息", False

    # 通用 SysEx（0x7E / 0x7F 开头）
    if payload[0] in (0x7E, 0x7F):
        for prefix, level, reason in GENERIC_RULES:
            if list(payload[:len(prefix)]) == list(prefix):
                return level, reason, False
        return RISK_WARN, "未识别的通用 SysEx", False

    # 厂商 ID 解析
    vid = vendor_id_of(payload)
    vendor = vendor_of(payload)
    if vid is None:
        return RISK_OK, "未知厂商·未知命令（中性）", False
    if vendor is None:
        return RISK_OK, f"未知厂商 (ID {bytes(vid).hex(' ').upper()})·中性", False

    # Roland 专用规则
    if vid == (0x41,):
        for prefix, level, reason in _ROLAND_RULES:
            if list(payload[:len(prefix)]) == list(prefix):
                return level, reason, True
        return RISK_OK, "Roland·未知命令（中性）", True

    # 其它已知厂商：未匹配 → OK 中性
    return RISK_OK, f"{vendor}·未知命令（中性）", True


def summarize(payload: List[int]) -> str:
    """解析摘要（给接收/发送区显示）。"""
    level, reason, _ = classify(payload)
    vendor = vendor_of(payload)
    head = f"{vendor} " if vendor else ""
    return f"{head}{reason}"


# ============================================================
# 黑名单
# ============================================================

# 默认黑名单（named_addresses 的基底，config.json 里可覆盖）
DEFAULT_BLACKLIST_ADDRS = [
    {
        "id": "roland_flash",
        "manufacturer": 0x41,
        "addr_offset": 2,          # manuf + dev_id + cmd 之后的 3 字节地址
        "addr_prefix": [0x20, 0x00, 0x00],
        "addr_len": 3,
        "reason": "Roland 固件/程序区",
        "block": True,
    },
    {
        "id": "roland_system",
        "manufacturer": 0x41,
        "addr_offset": 2,
        "addr_prefix": [0x40, 0x00, 0x00],
        "addr_len": 3,
        "reason": "Roland 系统参数区（写风险中）",
        "block": False,
    },
]


def blacklist_hit(payload: List[int], blacklist: dict) -> Optional[dict]:
    """按厂商 ID + 地址前缀匹配黑名单条目。返回命中的条目 dict 或 None。

    payload 假定结构: [manuf(1-3字节), dev_id, cmd, addr...(addr_len 字节), ...]。
    地址段从 payload 里 厂商ID之后 + addr_offset 个字节 开始取 addr_len 字节。
    条目用 addr_prefix（与地址段前 len(prefix) 字节等长）匹配。
    """
    if not payload or not blacklist.get("enabled", True):
        return None
    rules = list(blacklist.get("named_addresses", [])) + list(blacklist.get("custom", []))
    if not rules:
        return None

    vid = vendor_id_of(payload)
    if vid is None:
        return None
    manuf = vid[0]

    for rule in rules:
        if rule.get("manufacturer") != manuf:
            continue
        prefix = rule.get("addr_prefix") or []
        alen = rule.get("addr_len") or len(prefix)
        aoff = rule.get("addr_offset", 2)
        addr = payload[len(vid) + aoff:len(vid) + aoff + alen]
        if len(addr) < len(prefix):
            continue
        if list(addr[:len(prefix)]) == list(prefix):
            return rule
    return None