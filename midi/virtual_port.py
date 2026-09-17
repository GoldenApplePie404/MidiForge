import re
from typing import List, Optional

LOOPMIDI_URL = "https://www.tobias-erichsen.de/software/loopmidi.html"


def _base(name: str) -> str:
    """去掉 rtmidi 在 Windows 上附加的末尾索引号。"""
    return re.sub(r"\s+\d+$", "", name)


def detect_virtual_out_port(output_names: List[str], input_names: Optional[List[str]] = None) -> Optional[str]:
    """识别虚拟 MIDI 端口。

    优先匹配 loopMIDI / Virtual 关键字；
    否则查找在输入输出两侧都出现的同名（base 名）端口。
    """
    for name in output_names:
        lower = name.lower()
        if "loopmidi" in lower or "virtual" in lower:
            return name

    if input_names:
        out_bases = {_base(n) for n in output_names}
        in_bases = {_base(n) for n in input_names}
        common = out_bases & in_bases
        if common:
            # 返回第一个匹配的输出端口
            for name in output_names:
                if _base(name) in common:
                    return name

    return None


def setup_guide() -> str:
    return f"未检测到虚拟 MIDI 端口。请安装 loopMIDI（{LOOPMIDI_URL}）并创建至少一个虚拟端口，然后重启本程序。"