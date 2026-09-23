"""_sysex_lib 纯逻辑单测：hex 编解码 / 校验和 / 厂商表 / 风险分类 / 黑名单。"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from midi import sysex as L


# ============ hex 编解码 ============

def test_hex_str_to_bytes_basic():
    assert L.hex_str_to_bytes("41 10 42 12") == [0x41, 0x10, 0x42, 0x12]
    assert L.hex_str_to_bytes("41,10,42") == [0x41, 0x10, 0x42]
    assert L.hex_str_to_bytes("41\n10\n42") == [0x41, 0x10, 0x42]


def test_hex_str_to_bytes_invalid():
    with pytest.raises(ValueError):
        L.hex_str_to_bytes("41 1")      # 单字符
    with pytest.raises(ValueError):
        L.hex_str_to_bytes("41 ZZ")     # 非 hex
    with pytest.raises(ValueError):
        L.hex_str_to_bytes("41 FF")     # >0x7F


def test_bytes_to_hex_str():
    assert L.bytes_to_hex_str([0x41, 0x10]) == "41 10"
    assert L.bytes_to_hex_str([0x41, 0x10], wrap_f0f7=True) == "F0 41 10 F7"


# ============ 校验和 ============

def test_roland_checksum_known_vector():
    # Roland 文档示例：F0 41 10 42 12 40 00 7F 00 41 F7
    # payload(不含厂商头) = 40 00 7F 00 → sum=191, 191%128=63, 128-63=65=0x41
    assert L._roland_checksum([0x40, 0x00, 0x7F, 0x00]) == 0x41


def test_checksum_register_has_roland():
    assert "roland" in L.CHECKSUM_ALGORITHMS


def test_apply_and_verify_roundtrip():
    p = [0x41, 0x10, 0x42, 0x12, 0x40, 0x00, 0x7F, 0x00, 0x00]
    out = L.apply_checksum(p)
    assert L.verify_checksum(out) is True
    # 篡改最后一个字节后校验失败
    bad = out[:-1] + [out[-1] ^ 1]
    assert L.verify_checksum(bad) is False


def test_apply_checksum_unknown_algo():
    with pytest.raises(ValueError):
        L.apply_checksum([1, 2], algo="nope")


# ============ 厂商表 ============

def test_vendor_of_1_2_3_byte():
    assert L.vendor_of([0x41, 0x10]) == "Roland"
    assert L.vendor_of([0x43, 0x00]) == "Yamaha"
    assert L.vendor_of([0x00, 0x1D, 0x01]) == "Alesis (2字节)"
    assert L.vendor_of([0x00, 0x00, 0x41, 0x10]) == "Roland (3字节扩展)"
    assert L.vendor_of([0x63, 0x10]) is None     # 未知厂商


def test_vendor_id_of():
    assert L.vendor_id_of([0x41, 0x10]) == (0x41,)
    assert L.vendor_id_of([0x00, 0x1D, 0x01]) == (0x00, 0x1D)
    assert L.vendor_id_of([0x00, 0x00, 0x41]) == (0x00, 0x00, 0x41)


# ============ 风险分类 ============

def test_classify_firmware_update_high():
    """固件/程序区写入类消息应判高危。
    Device Inquiry sub-ID2: 0x03=Load/Start, 0x04=Load/Data, 0x05=Load/End 均为写入操作。
    """
    # Device Inquiry Load/Start — 写入设备固件
    level, reason, known = L.classify([0x7E, 0x7F, 0x01, 0x03])
    assert level == L.RISK_HIGH, f"expected HIGH got {level}: {reason}"
    assert "Device Inquiry" in reason and "Load/Start" in reason
    assert known is False
    # Device Inquiry Load/Data 也是写入
    level2, reason2, _ = L.classify([0x7E, 0x7F, 0x01, 0x04, 0x00, 0x41, 0x10])
    assert level2 == L.RISK_HIGH
    # Universal Real-Time firmware update (0x7F 7F 04)
    level3, reason3, _ = L.classify([0x7F, 0x7F, 0x04, 0x01, 0x00])
    assert level3 == L.RISK_HIGH
    assert "固件升级" in reason3


def test_classify_identity_query_ok():
    """GM System Inquiry 请求/回应应判正常。"""
    # GM System Inquiry 请求: F0 7E 7F 06 01 F7
    level, reason, _ = L.classify([0x7E, 0x7F, 0x06, 0x01])
    assert level == L.RISK_OK
    assert "GM System Inquiry" in reason and "请求" in reason
    # GM System Inquiry 回应（截图中 KL 键盘发的 Utoc 回应）
    level2, reason2, _ = L.classify([0x7E, 0x7F, 0x06, 0x00, 0x20, 0x68, 0x00])
    assert level2 == L.RISK_OK
    assert "GM System Inquiry" in reason2 and "回应" in reason2


def test_classify_vendor_known_neutral():
    level, reason, known = L.classify([0x41, 0x10, 0x42, 0x12, 0x40, 0x00, 0x7F])
    assert level == L.RISK_OK
    assert known is True


def test_classify_unknown_vendor_neutral():
    # 未知厂商绝不猜测为高危
    level, reason, known = L.classify([0x63, 0x10, 0x20])
    assert level == L.RISK_OK
    assert known is False


def test_classify_real_time_warn():
    level, _, _ = L.classify([0x7F, 0x01, 0x02])
    assert level == L.RISK_WARN


# ============ 黑名单 ============

def _blacklist():
    return {"enabled": True, "named_addresses": L.DEFAULT_BLACKLIST_ADDRS, "custom": []}


def test_blacklist_flash_hit_block():
    # Roland DT1 写固件区地址 20 00 00
    hit = L.blacklist_hit([0x41, 0x10, 0x12, 0x20, 0x00, 0x00, 0x01, 0x40], _blacklist())
    assert hit is not None
    assert hit["id"] == "roland_flash"
    assert hit["block"] is True


def test_blacklist_system_hit_no_block():
    hit = L.blacklist_hit([0x41, 0x10, 0x12, 0x40, 0x00, 0x00, 0x01, 0x40], _blacklist())
    assert hit is not None
    assert hit["id"] == "roland_system"
    assert hit["block"] is False


def test_blacklist_tone_miss():
    hit = L.blacklist_hit([0x41, 0x10, 0x12, 0x50, 0x00, 0x00, 0x01, 0x40], _blacklist())
    assert hit is None


def test_blacklist_custom_rule():
    bl = _blacklist()
    bl["custom"] = [{
        "id": "my_vendor",
        "manufacturer": 0x43,               # Yamaha
        "addr_offset": 2,
        "addr_prefix": [0x02, 0x00, 0x00],
        "addr_len": 3,
        "reason": "测试自定义固件区",
        "block": True,
    }]
    hit = L.blacklist_hit([0x43, 0x00, 0x12, 0x02, 0x00, 0x00, 0x01], bl)
    assert hit is not None and hit["id"] == "my_vendor"
    assert L.blacklist_hit([0x43, 0x00, 0x12, 0x20, 0x00, 0x00, 0x01], bl) is None


def test_blacklist_disabled():
    bl = _blacklist()
    bl["enabled"] = False
    assert L.blacklist_hit([0x41, 0x10, 0x12, 0x20, 0x00, 0x00, 0x01], bl) is None