# Meituan/parse_meituan_xml.py
"""
从美团酒店详情页 UI 树 XML 中解析房型套餐列表。
兼容 xml/22.xml 格式：节点标签为 android.widget.TextView 等，text 在 text= 属性中。
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional


def _parse_bounds(bounds: str) -> Optional[tuple[int, int, int, int]]:
    """解析 bounds 字符串 "[left,top][right,bottom]" -> (left, top, right, bottom)。"""
    if not bounds:
        return None
    m = re.findall(r"\d+", bounds)
    if len(m) != 4:
        return None
    return tuple(int(x) for x in m)


def _iter_text_nodes(root: ET.Element):
    """遍历所有带 text 或 content-desc 的节点，产出 (top, text, bounds)。"""
    for elem in root.iter():
        text = (elem.attrib.get("text") or elem.attrib.get("content-desc") or "").strip()
        if not text:
            continue
        b = _parse_bounds(elem.attrib.get("bounds", ""))
        top = b[1] if b else 99999
        yield top, text, elem.attrib.get("bounds", "")


# 顶部 Tab/筛选项，不作为房型名（三床房仅为筛选标签；钟点房仅在同行的 Tab 行排除，列表中的钟点房保留）
ROOM_TAB_TEXTS = {"双床房", "大床房", "三床房", "钟点房", "筛选", "预订", "评价", "设施", "周边", "详情", "住就送外卖券", "房间"}
# 非房型名的短文案
BLACKLIST = {
    "代理", "抢", "满", "店内低价", "有房提醒",
    "预计30分钟确认", "住就送·10元闪购券",
    "单份早餐", "双份早餐", "三份早餐",
}
# 不作为房型标题的文案（含这些则跳过，避免点评/设施/推荐附近酒店被当房型）
NOT_ROOM_TITLE_KEYWORDS = (
    "剩余", "仅剩", "已订完", "查看全部", "查看房型", "房间设施好", "房间整洁", "客房WiFi",
    "订房必读", "洗衣房", "超赞房东", "尊敬的客户", "根据《", "生活垃圾",
    "入住时间", "离店时间", "儿童及加床", "传真", "复印", "距您", "驾车", "消费",
    "人气榜", "黄浦区", "静安寺", "地铁口", "经济人气榜", "舒适人气榜",
)


def parse_meituan_rooms_from_xml(xml_str: str) -> list[dict]:
    """
    从美团 UI 树 XML 解析房型套餐列表。
    返回与携程 1.json 一致的字段：房型名称、窗户信息、价格、剩余房间、备注。
    """
    if not xml_str or "<" not in xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return []

    # 收集所有带文字的节点，按 top 排序
    items: list[tuple[int, str, str]] = []
    for top, text, bounds in _iter_text_nodes(root):
        if len(text) > 200:
            continue
        items.append((top, text, bounds))
    items.sort(key=lambda x: (x[0], x[1]))

    # 动态检测 Tab 行位置（"大床房"/"双床房"/"三床房" 所在行），作为房型列表的起始线
    tab_row_top = 0
    tab_texts_found: list[int] = []
    for top, text, _ in items:
        if text in ("大床房", "双床房", "三床房", "钟点房"):
            tab_texts_found.append(top)
    if tab_texts_found:
        from collections import Counter
        top_counts = Counter(tab_texts_found)
        tab_row_top = top_counts.most_common(1)[0][0]

    # 找出房型标题行：含「房」且非 Tab/黑名单
    # Tab 行是 sticky header，房型卡可能滚到 Tab 行上方，所以允许 tab_row_top - 300
    min_room_top = max(tab_row_top - 300, 100) if tab_row_top > 0 else 200
    room_title_indices: list[int] = []
    for i, (top, text, _) in enumerate(items):
        if top < min_room_top:
            continue
        if "房" not in text:
            continue
        if text in ROOM_TAB_TEXTS or text in BLACKLIST:
            continue
        if any(kw in text for kw in NOT_ROOM_TITLE_KEYWORDS):
            continue
        if re.match(r"^\d+张\d", text) or "米大床" in text or "米双床" in text:
            continue
        if "入住" in text or "㎡" in text:
            continue
        # 房型名不宜过长（点评/政策长文）
        if len(text) > 28:
            continue
        room_title_indices.append(i)

    result: list[dict] = []
    for idx, title_i in enumerate(room_title_indices):
        title_top, room_name, _ = items[title_i]
        # 本卡片范围：到下一个房型标题之前，或至少下 400px（保证价格行在下一屏时也能包含）
        if idx + 1 < len(room_title_indices):
            next_i = room_title_indices[idx + 1]
            card_bottom = max(items[next_i][0] - 10, title_top + 400)
        else:
            card_bottom = title_top + 450

        # 本卡片内所有文案（先收集再判价格）；排除 (224) 这类评价数
        window = ""
        price = ""
        remain = ""
        remark_parts: list[str] = []
        card_texts: list[tuple[int, str, str]] = []

        for top, text, bounds in items:
            if top < title_top or top > card_bottom:
                continue
            if text == room_name:
                continue
            if text in BLACKLIST:
                continue
            if re.match(r"^\(\d+\)$", text.strip()):
                continue
            card_texts.append((top, text, bounds))

        # 收集本卡片内所有价格（按 top 排序），支持一卡多套餐：无早餐¥409、1份早餐¥452、2份早餐¥489
        prices_in_card: list[tuple[int, str]] = []
        for top, text, bounds in card_texts:
            if text == "¥":
                for t2, txt, _ in card_texts:
                    if abs(t2 - top) < 80 and txt.isdigit() and 100 <= int(txt) <= 9999:
                        prices_in_card.append((min(top, t2), f"¥{txt}"))
                        break
        if not prices_in_card:
            for top, text, bounds in card_texts:
                if not text.isdigit() or len(text) > 5:
                    continue
                try:
                    v = int(text)
                except ValueError:
                    continue
                if v < 100 or v > 9999 or (200 <= v < 300):
                    continue
                prices_in_card.append((top, f"¥{text}"))
                break
        # 去重：同一价格且 top 接近（同一套餐）只保留一条，其余按 top 排序
        sorted_prices = sorted(prices_in_card, key=lambda x: x[0])
        unique_prices = []
        for top, pk in sorted_prices:
            if unique_prices and unique_prices[-1][1] == pk and (top - unique_prices[-1][0]) < 50:
                continue
            unique_prices.append((top, pk))

        # 若有多条价格，按每条价格拆成独立套餐（每段取该价格附近的 早餐/仅X间/备注）
        if len(unique_prices) <= 1:
            price = unique_prices[0][1] if unique_prices else ""
            for top, text, bounds in card_texts:
                if text == room_name:
                    continue
                if text in BLACKLIST:
                    continue
                if text == "¥" or (text.isdigit() and len(text) <= 5):
                    continue
                if "有窗" in text or "无窗" in text:
                    if not window:
                        window = text
                    continue
                if "仅" in text and "间" in text:
                    if not remain:
                        remain = text
                    continue
                if "已订完" in text or ("订完" in text and ("剩余" in text or "房型" in text)):
                    if not remain:
                        remain = "订完了"
                    continue
                if "无早餐" in text or "不可取消" in text or "预计" in text or "住就送" in text:
                    remark_parts.append(text)
                    continue
                if "早餐" in text or "份早餐" in text:
                    remark_parts.append(text)
                    continue
                if "人入住" in text or "㎡" in text or ("米" in text and "床" in text):
                    remark_parts.append(text)
                    continue
                if len(text) <= 4 and text not in ("代理", "抢", "满"):
                    continue
                remark_parts.append(text)

            remark = " ".join(remark_parts).strip()
            if remain and ("已订完" in remain or "订完" in remain):
                remain = "订完了"
            if not price and not remain and remark and "不可取消" in remark and "无早餐" in remark:
                remain = "订完了"
            if not price and not remain and remark and "不可取消" in remark and ("使用时间" in remark or "可住" in remark):
                remain = "订完了"
            if not price and not remain and not remark:
                continue
            if remark and ("距您查询的酒店" in remark or "如家旗下" in remark or "派柏·云" in remark or "步行350米" in remark):
                continue
            result.append({"房型名称": room_name, "窗户信息": window, "价格": price, "剩余房间": remain, "备注": remark})
        else:
            # 一卡多价：按每个价格划分竖直区间，每区间内收集 早餐/仅X间/备注
            for pidx, (price_top, price_str) in enumerate(unique_prices):
                seg_start = title_top if pidx == 0 else (unique_prices[pidx - 1][0] + price_top) // 2
                seg_end = (unique_prices[pidx + 1][0] + price_top) // 2 if pidx + 1 < len(unique_prices) else card_bottom
                seg_window = ""
                seg_remain = ""
                seg_remarks: list[str] = []
                for top, text, bounds in card_texts:
                    if top < seg_start or top > seg_end:
                        continue
                    if text == room_name or text in BLACKLIST:
                        continue
                    if text == "¥" or (text.isdigit() and len(text) <= 5):
                        continue
                    if "有窗" in text or "无窗" in text:
                        if not seg_window:
                            seg_window = text
                        continue
                    if "仅" in text and "间" in text:
                        if not seg_remain:
                            seg_remain = text
                        continue
                    if "已订完" in text or ("订完" in text and ("剩余" in text or "房型" in text)):
                        if not seg_remain:
                            seg_remain = "订完了"
                        continue
                    if "无早餐" in text or "不可取消" in text or "预计" in text or "住就送" in text or "早餐" in text or "份早餐" in text:
                        seg_remarks.append(text)
                        continue
                    if "人入住" in text or "㎡" in text or ("米" in text and "床" in text):
                        seg_remarks.append(text)
                        continue
                    if len(text) <= 4 and text not in ("代理", "抢", "满"):
                        continue
                    seg_remarks.append(text)
                seg_remark = " ".join(seg_remarks).strip()
                if seg_remain and ("已订完" in seg_remain or "订完" in seg_remain):
                    seg_remain = "订完了"
                if seg_remark and ("距您查询的酒店" in seg_remark or "如家旗下" in seg_remark or "派柏·云" in seg_remark or "步行350米" in seg_remark):
                    continue
                result.append({"房型名称": room_name, "窗户信息": seg_window or window, "价格": price_str, "剩余房间": seg_remain, "备注": seg_remark})

    return result


def extract_meituan_page_info(xml_str: str) -> dict:
    """从美团 UI 树中提取页面信息：酒店名、入住/离店日期、地址（若有）。"""
    out = {"酒店名称": "", "入住日期": "", "离店日期": "", "地址": ""}
    if not xml_str:
        return out
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return out
    items = []
    for top, text, _ in _iter_text_nodes(root):
        if not text or len(text) > 150:
            continue
        items.append((top, text))
    items.sort(key=lambda x: x[0])

    # 顶部区域尝试酒店名：含「酒店」「店」「宾馆」「民宿」且长度适中
    for top, t in items:
        if top > 700:
            break
        t = t.strip()
        if 5 <= len(t) <= 55 and ("酒店" in t or "宾馆" in t or "民宿" in t) and "预订" not in t and "详情" not in t and "送" not in t:
            out["酒店名称"] = t
            break
    # 地址：含区/路/号/弄/街 或 xxx地区·xxx 格式
    for top, t in items:
        if top > 1200:
            break
        t = t.strip()
        if 8 <= len(t) <= 100:
            if ("区" in t or "县" in t) and ("路" in t or "号" in t or "弄" in t or "街" in t):
                out["地址"] = t
                break
            if "地区" in t and "·" in t:
                out["地址"] = t
                break

    # 日期：02月27日 今天 / 02月28日 明天
    date_pattern = re.compile(r"(\d{2})月(\d{2})日")
    for top, t in items:
        if top > 500:
            break
        m = date_pattern.findall(t)
        if len(m) >= 2 and not out["入住日期"]:
            out["入住日期"] = f"2026-{m[0][0]}-{m[0][1]}"
            out["离店日期"] = f"2026-{m[1][0]}-{m[1][1]}"
            break
        if len(m) >= 1 and "今天" in t and not out["入住日期"]:
            out["入住日期"] = f"2026-{m[0][0]}-{m[0][1]}"
        if len(m) >= 1 and "明天" in t and not out["离店日期"]:
            out["离店日期"] = f"2026-{m[0][0]}-{m[0][1]}"
    return out
