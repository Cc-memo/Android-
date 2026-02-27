"""
私有区符号价格解码：
- 携程部分价格使用私有区字体渲染，UI 树里看不到具体数字，只能看到 U+E*** 一类字符。
- 本文件提供一个简单的「符号 -> 数字」映射与解码函数，供 2.py 在正则失败时作为 fallback 使用。

注意：
- PRICE_CHAR_MAP 里的键需要你根据实际 dump 到的字符手动补充，这里只给出示例写法。
- 当映射表为空或无法完全解码时，decode_price_text 会返回空字符串，不影响现有逻辑。
"""

from __future__ import annotations


# TODO: 按实际 dump 到的字符补充映射关系。
# 例：假设你发现 "\ue001\ue002\ue003" 在界面上显示为 "123"，则可写：
# PRICE_CHAR_MAP = {
#     "\ue001": "1",
#     "\ue002": "2",
#     "\ue003": "3",
# }
PRICE_CHAR_MAP: dict[str, str] = {}


def _is_private_char(ch: str) -> bool:
    """是否在私有区 U+E000–U+F8FF。"""
    if not ch:
        return False
    code = ord(ch)
    return 0xE000 <= code <= 0xF8FF


def decode_price_text(text: str) -> str:
    """
    尝试从一段包含私有区符号的文案里解出价格：
    - 若文本中不存在私有区字符，直接返回空；
    - 对每个私有区字符查 PRICE_CHAR_MAP：
      - 全部可解且组成的数字在 100–9999 之间，则返回 '¥xxx'；
      - 只要有一个字符解不出来，返回空字符串。
    """
    if not text:
        return ""

    # 至少要包含一个私有区字符，否则无需尝试解码
    if not any(_is_private_char(ch) for ch in text):
        return ""

    digits: list[str] = []
    for ch in text:
        if ch in PRICE_CHAR_MAP:
            digits.append(PRICE_CHAR_MAP[ch])
        elif _is_private_char(ch):
            # 是私有区字符但没有映射，暂时视为无法解码
            return ""
        else:
            # 其他字符（¥、空格、汉字、“起”等）忽略
            continue

    if not digits:
        return ""

    num_str = "".join(digits)
    try:
        value = int(num_str)
    except ValueError:
        return ""

    # 简单范围过滤：酒店房价通常不低于 100
    if not (100 <= value <= 9999):
        return ""

    return "¥" + num_str

