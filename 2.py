"""
从「元素列表」解析房型套餐，规则与 1.py 一致，输出 2.json。

支持两种输入（任选其一）：
1) Appium Inspector 导出的 XML：在 Inspector 里打开携程房型页 → Source 标签 → 
   「Download XML」保存为 source.xml，然后运行：python 2.py source.xml
2) 元素 JSON：python 2.py elements.json（格式见下方）

约定：一个房型下可有多个套餐，每个套餐框（ViewGroup）对应 房型列表 里的一条。
例如 source.xml = 1 个房型 + 2 个套餐 → 2 条；source2.xml = 1 个房型 + 3 个套餐 → 3 条。

用法：
  python 2.py [source.xml 或 elements.json]
  不传参数时默认读取当前目录下 source.xml，若无则 elements.json
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from price_mapping import decode_price_text


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """解析 bounds 字符串 "[left,top][right,bottom]" -> (left, top, right, bottom)。"""
    if not bounds:
        return None
    m = re.findall(r"\d+", bounds)
    if len(m) != 4:
        return None
    return tuple(int(x) for x in m)


def _elem_kv_list_to_dict(elem_list: list) -> dict:
    """将 [{"key":"text","value":"xxx","name":"text"}, ...] 转为 {"text":"xxx", ...}。"""
    d = {}
    for item in elem_list:
        if isinstance(item, dict):
            k = item.get("key") or item.get("name")
            v = item.get("value")
            if k is not None:
                d[str(k)] = v
    return d


def load_elements_from_xml(path: str) -> list[dict]:
    """从 Appium Inspector 导出的 XML（Source → Download XML）加载元素，转为 list[dict]。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    root = ET.fromstring(raw)
    out = []
    for elem in root.iter():
        a = elem.attrib
        text = (a.get("text") or a.get("name") or a.get("content-desc") or "").strip()
        bounds = (a.get("bounds") or "").strip()
        cls = (a.get("class") or "").strip()
        if not bounds and not text:
            continue
        out.append({
            "text": text, "bounds": bounds, "class": cls,
            "content-desc": a.get("content-desc") or "",
            "resource-id": a.get("resource-id") or "",
        })
    return out


def load_elements(path: str) -> list[dict]:
    """从 JSON 文件加载元素列表。支持：元素数组、或 {"elements": [...]}、或单个元素（key-value 数组）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, dict):
        if "elements" in data:
            data = data["elements"]
        else:
            data = [data]
    if not isinstance(data, list):
        data = [data]
    # 若顶层是「多个带 key/value 的对象」且无子数组，则视为一个元素（key-value 数组）
    if data and all(isinstance(x, dict) and "key" in x and "value" in x for x in data):
        return [_elem_kv_list_to_dict(data)]
    out = []
    for raw in data:
        if isinstance(raw, list):
            d = _elem_kv_list_to_dict(raw)
        elif isinstance(raw, dict):
            d = raw if (raw.get("bounds") or raw.get("text")) else _elem_kv_list_to_dict([raw])
        else:
            d = {}
        if d:
            out.append(d)
    return out


def _normalize_remarks(remarks: str, max_len: int = 150) -> str:
    if not remarks:
        return ""
    gift_phrase = "赠·人民广场地铁站至酒店接送"
    while gift_phrase in remarks and remarks.count(gift_phrase) > 1:
        idx = remarks.find(gift_phrase)
        end = remarks.find(" 赠·", idx + 1)
        if end > idx:
            remarks = remarks[:idx] + remarks[end:].strip()
        else:
            break
    parts = remarks.split()
    seen = set()
    out = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    s = " ".join(out)
    if len(s) > max_len:
        s = s[: max_len - 3].rstrip() + "..."
    return s


def _bounds_inside(inner_b: tuple, outer: tuple) -> bool:
    """inner_b 是否在 outer (left,top,right,bottom) 内（允许少许溢出）。"""
    if not inner_b or not outer or len(inner_b) != 4 or len(outer) != 4:
        return False
    il, it, ir, ib = inner_b
    ol, ot, or_, ob = outer
    margin = 20
    return il >= ol - margin and ir <= or_ + margin and it >= ot - margin and ib <= ob + margin


def _bounds_overlap_x(b1: tuple, b2: tuple) -> bool:
    if not b1 or not b2 or len(b1) != 4 or len(b2) != 4:
        return True
    return not (b1[2] <= b2[0] or b1[0] >= b2[2])


def parse_rooms_from_elements(elements: list[dict]) -> list[dict]:
    """从元素列表解析房型与套餐。用「房型名称」+「套餐框 ViewGroup」：只收集框内文案。"""
    blacklist_exact = {"洗衣房", "订房优惠", "房型", "有房提醒", "订房必读", "查看房型"}
    blacklist_contains = [
        "销量No.", "本店大床房销量", "点评", "评论", "服务与设施",
        "来上海旅游", "生活垃圾管理条例", "退房时间", "所有房型不可加床",
    ]
    gift_keywords = ["赠·", "赠送", "礼遇", "礼品", "票券"]
    room_keywords = ["房", "单人间", "大床", "双床", "三人间", "家庭房"]
    remark_keywords = [
        "早餐", "早", "取消", "可退", "不可退", "不可取消", "免费取消",
        "礼", "预付", "现付", "在线付", "到店付",
        # 钟点房相关：将可住时段/连住X小时也视为套餐备注的组成部分
        "可住时段", "连住", "小时",
    ]
    breakfast_cancel_pay_keywords = [
        "无早餐", "含早", "份早餐", "早餐券", "取消", "可退", "不可退",
        "不可取消", "免费取消", "预付", "现付", "在线付", "到店付",
    ]
    tab_only_names = {"双床房", "大床房", "三床房", "单人间"}

    # 1) 分类：房型名称（TextView+房型关键词）、套餐框（ViewGroup）、所有带文案节点
    # 若为携程 XML（存在 htl_x_dtl_rmlist 等 id），优先只把 rmCard 当套餐框；
    # 真机 dump 可能无 rmCard，则用全部 ViewGroup；但明确排除 nearbyRec（推荐酒店）区域的卡片。
    has_ctrip_ids = any(
        "htl_x_dtl" in (str(d.get("resource-id") or "") + str(d.get("content-desc") or ""))
        for d in elements
    )
    title_nodes = []
    view_groups = []
    view_groups_fallback = []  # 所有 ViewGroup，用于 has_ctrip 但无 rmCard 时回退
    text_nodes = []
    for d in elements:
        text = (d.get("text") or d.get("content-desc") or "").strip()
        bounds = d.get("bounds") or ""
        b = _parse_bounds(bounds) if bounds else None
        cls = (d.get("class") or "").lower()
        is_view_group = "viewgroup" in cls or "view group" in cls
        rid = (d.get("resource-id") or "").strip()
        cdesc = (d.get("content-desc") or "").strip()
        rid_cdesc = (rid + " " + cdesc).lower()
        is_rm_card = "rmcard" in rid_cdesc and "mbrmcard" not in rid_cdesc
        # 推荐酒店卡片常带 htl_x_dtl_nearbyRec_htlCard_exposure 等 id/content-desc，明确排除，不算作本酒店套餐
        is_nearby_card = "nearbyrec" in rid_cdesc
        if b is None:
            continue
        top = b[1]
        node = {"text": text, "bounds_parsed": b, "top": top}
        if is_view_group:
            if is_nearby_card:
                # 推荐酒店区域的卡片，不作为当前酒店房型的套餐框
                continue
            if has_ctrip_ids:
                if is_rm_card:
                    view_groups.append(node)
                view_groups_fallback.append(node)
            else:
                view_groups.append(node)
        if text:
            text_nodes.append(node)
            has_room_keyword = any(k in text for k in room_keywords)
            if has_room_keyword and ("textview" in cls or "text" in cls or not is_view_group):
                if "点评" in text or "评论" in text or "预订" in text:
                    continue
                if text in blacklist_exact or any(k in text for k in blacklist_contains):
                    continue
                # 仅过滤“纯床型描述”，避免把包含“房型名 + 床型说明”的标题误杀
                bed_spec_only = bool(
                    re.fullmatch(r"\d+张\d(?:\.\d+)?米(?:大床|双床)", text)
                    or re.fullmatch(r"\d+张(?:大床|双床)", text)
                )
                if bed_spec_only:
                    continue
                if text in tab_only_names and "「" not in text:
                    continue
                # 赠品长句（含退房/接送/管家）不作为房型名
                if ("赠·" in text or "赠送" in text) and ("接送" in text or "管家" in text or "延迟退房" in text or "退房至" in text):
                    continue
                title_nodes.append(node)

    # 真机 uiautomator dump 常有 htl_x_dtl 但无 rmCard，导致 view_groups 为空，用全部 ViewGroup 回退
    if has_ctrip_ids and len(view_groups) == 0 and len(view_groups_fallback) > 0:
        view_groups = view_groups_fallback
    title_nodes.sort(key=lambda x: x["top"])
    view_groups.sort(key=lambda x: x["top"])

    # 规律：同一房型下可有多个套餐框（ViewGroup），竖直堆叠；归属优先级：1) 与套餐框竖直重叠的房型 2) 紧贴其上(tight_gap) 3) 略下( max_gap)
    tight_gap = 220   # 紧贴其上：房型 bottom 在 套餐 top 下方约一卡内（避免 source4「订完」卡被归到更下房型）
    max_gap = 600     # 套餐在房型名上方时的放宽（如 source3）
    vg_to_title_idx = []
    for vg in view_groups:
        v_top = vg["bounds_parsed"][1]
        v_bottom = vg["bounds_parsed"][3]
        best_title = None
        best_priority = -1  # 0=重叠 1=tight 2=loose
        best_bottom = -1
        for ti, tn in enumerate(title_nodes):
            t_top = tn["bounds_parsed"][1]
            t_bottom = tn["bounds_parsed"][3]
            if not _bounds_overlap_x(tn["bounds_parsed"], vg["bounds_parsed"]):
                continue
            if t_top >= v_bottom + 80:
                continue
            overlaps = t_top < v_bottom and t_bottom > v_top
            in_tight = not overlaps and t_bottom <= v_top + tight_gap
            in_loose = not overlaps and t_bottom <= v_top + max_gap
            if overlaps:
                prio = 0
            elif in_tight:
                prio = 1
            elif in_loose:
                prio = 2
            else:
                continue
            if best_title is None or prio < best_priority or (prio == best_priority and t_bottom > best_bottom):
                best_title = (ti, tn)
                best_priority = prio
                best_bottom = t_bottom
        if best_title is not None:
            vg_to_title_idx.append((vg, best_title[1]))

    # 按 (title_top, vg_top) 排序，保证先输出上一房型再下一房型、同一房型内按套餐框顺序
    def _key_vg_title(item):
        vg, tn = item
        return (tn["top"], vg["top"])

    vg_to_title_idx.sort(key=_key_vg_title)

    room_items = []
    for vg, tn in vg_to_title_idx:
        title = re.sub(r"[\u200b-\u200d\ufeff\u202a-\u202e\u2060\u00ad\u034f\ue004-\ue0ff]", "", tn["text"]).strip()
        card = vg["bounds_parsed"]
        card_left, card_top, card_right, card_bottom = card
        # 放宽收集范围：右扩以包含「预订/价格」按钮区，上下多扩以包含「仅剩X间」等常在卡片底/顶的节点
        margin_right = 100
        margin_v = 45
        extended_card = (
            card_left,
            max(0, card_top - margin_v),
            card_right + margin_right,
            card_bottom + margin_v,
        )

        # 区分「严格在卡内」与「仅在扩展区」，价格优先用卡内，避免下一张卡价格串入（如大床房误用优选单人间的 437）
        texts_with_in_card = []
        for other in text_nodes:
            ob = other["bounds_parsed"]
            ot = other["text"]
            if not ot or ot == title:
                continue
            if _bounds_inside(ob, card):
                texts_with_in_card.append((ot, True))
            elif _bounds_inside(ob, extended_card) and _bounds_overlap_x(ob, card):
                texts_with_in_card.append((ot, False))
        seen = {}
        for t, in_card in texts_with_in_card:
            if t not in seen:
                seen[t] = in_card
            else:
                seen[t] = seen[t] or in_card
        uniq_texts = list(seen.keys())
        in_card_texts = {t for t, inc in seen.items() if inc}

        window = ""
        remain = ""
        for t in uniq_texts:
            if ("有窗" in t or "无窗" in t) and not window:
                window = t
            if ("仅剩" in t or "间" in t) and not remain and "仅" in t:
                remain = t
        # 订完/售罄等：无“仅剩X间”时若卡片内有任一“已订完”类表述则写入剩余房间
        sold_out_keywords = ("订完", "售罄", "暂无", "无房", "满房", "已满")
        if not remain and any(any(k in t for k in sold_out_keywords) for t in uniq_texts):
            remain = "已订完"

        combined = " ".join(uniq_texts) if uniq_texts else ""

        # 价格：优先用「严格在卡内」节点的价格，避免扩展区带入下一张卡价格（如大床房误用 437）
        price_candidates_in_card: list[int] = []
        price_candidates_extended: list[int] = []
        price = ""
        remark_parts = []
        for t in uniq_texts:
            if "¥" in t:
                for m in re.finditer(r"¥\s*(\d+)[\d.]*", t):
                    num_str = m.group(1)
                    try:
                        v = int(num_str)
                        if 100 <= v <= 9999:
                            if t in in_card_texts:
                                price_candidates_in_card.append(v)
                            else:
                                price_candidates_extended.append(v)
                    except ValueError:
                        continue
                # 仍然保留原先「同一句里价格外部分并入备注」的逻辑（以第一个匹配为准）
                m_first = re.search(r"¥\s*\d+[\d.]*", t)
                if m_first:
                    rest = (t[: m_first.start()] + t[m_first.end() :]).strip()
                    if rest and any(k in rest for k in remark_keywords):
                        remark_parts.append(rest)
                continue
            if any(k in t for k in remark_keywords):
                remark_parts.append(t)

        # 优先取卡内价格的最小值，无卡内价再用扩展区（避免串格）
        if price_candidates_in_card:
            price = f"¥{min(price_candidates_in_card)}"
        elif price_candidates_extended:
            price = f"¥{min(price_candidates_extended)}"

        # 价格 fallback 1：在合并文案里匹配（携程常把 ¥ 与数字拆成多节点或放在扩展区）
        if not price and combined:
            for m in re.finditer(r"¥\s*(\d+)[\d.]*", combined):
                try:
                    v = int(m.group(1))
                    if 100 <= v <= 9999:
                        price_candidates_extended.append(v)
                except ValueError:
                    continue
            if price_candidates_extended:
                price = f"¥{min(price_candidates_extended)}"

        # 价格 fallback 2：尝试用私有区符号解码价格（需要在 price_mapping.PRICE_CHAR_MAP 中维护映射）
        if not price:
            for t in uniq_texts:
                if "¥" in t or any(0xE000 <= ord(ch) <= 0xF8FF for ch in t):
                    decoded = decode_price_text(t)
                    if decoded:
                        m_dec = re.search(r"¥\s*(\d+)", decoded)
                        if m_dec:
                            try:
                                v = int(m_dec.group(1))
                                if 100 <= v <= 9999:
                                    price = f"¥{v}"
                                    break
                            except ValueError:
                                pass
                    break

        remarks = " ".join(remark_parts).strip()
        if remain == "已订完" and "订完" not in remarks:
            remarks = ("已订完" if not remarks else remarks.rstrip() + " 已订完").strip()

        room_items.append({
            "房型名称": title,
            "窗户信息": window,
            "价格": price,
            "剩余房间": remain,
            "备注": remarks,
            "_bounds": (card_left, card_top, card_right, card_bottom),  # 供 1.py 截图 OCR 补价格用
        })

    # 同房型内回填：若前一条有价格而本条为空，则沿用（同一房型多套餐常共用展示）
    for i in range(1, len(room_items)):
        prev, cur = room_items[i - 1], room_items[i]
        if (prev.get("房型名称") or "").strip() != (cur.get("房型名称") or "").strip():
            continue
        # 仅回填价格，不再回填剩余房间，避免「仅剩4间」在同房型所有套餐上都一样
        if not (cur.get("价格") or "").strip() and (prev.get("价格") or "").strip():
            cur["价格"] = prev["价格"]

    # 赠品合并
    merged = []
    for item in room_items:
        name = item["房型名称"]
        if any(k in name for k in gift_keywords) and merged:
            prev = merged[-1]
            extra = name.strip()
            if extra:
                prev["备注"] = (prev["备注"] or "") + (" " + extra if prev["备注"] else extra)
            if not prev.get("价格") and item.get("价格"):
                prev["价格"] = item["价格"]
            if not prev.get("剩余房间") and item.get("剩余房间"):
                prev["剩余房间"] = item["剩余房间"]
            continue
        merged.append(item)

    filtered = []
    for item in merged:
        name = (item.get("房型名称") or "").strip()
        name = re.sub(r"[\u200b-\u200d\ufeff\u202a-\u202e\u2060\u00ad\u034f\ue004-\ue0ff]", "", name).strip()
        if not name or len(name) > 60:
            continue
        if any(bad in name for bad in blacklist_contains) or name in blacklist_exact:
            continue
        if name in tab_only_names and "「" not in name:
            continue
        bed_spec_only = bool(
            re.fullmatch(r"\d+张\d(?:\.\d+)?米(?:大床|双床)", name)
            or re.fullmatch(r"\d+张(?:大床|双床)", name)
        )
        if bed_spec_only:
            continue
        price = (item.get("价格") or "").strip()
        remarks = _normalize_remarks((item.get("备注") or "").strip())
        if not price and len(remarks) > 80 and not any(k in remarks for k in breakfast_cancel_pay_keywords):
            continue
        remain_val = (item.get("剩余房间") or "").strip()
        has_info = bool(price) or (
            remarks and len(remarks) <= 120 and any(k in remarks for k in breakfast_cancel_pay_keywords)
        ) or "订完" in remain_val or "订完" in remarks
        if not has_info:
            continue
        item["房型名称"] = name
        item["备注"] = remarks
        filtered.append(item)

    return filtered


def _contract_room_item(item: dict) -> dict:
    """收缩单条房型套餐展示信息：房型名取主名，钟点房时长写入备注，再截断长度。"""
    raw_name = (item.get("房型名称") or "").strip()

    # 从标题中抽取钟点房时长信息，例如「连住3小时」「连住4小时」
    extra_time = ""
    m_time = re.search(r"连住\d+小时", raw_name)
    if m_time:
        extra_time = m_time.group(0)

    # 收缩房型名到主名（去掉「…」内部文案）
    name = raw_name
    if "「" in name:
        name = name.split("「")[0].strip()
    if len(name) > 24:
        name = name[:24].rstrip() + "…"

    # 合并钟点时长到备注
    remark = (item.get("备注") or "").strip()
    if extra_time and extra_time not in remark:
        remark = (extra_time + " " + remark).strip() if remark else extra_time

    # 备注最大长度控制
    if len(remark) > 80:
        remark = remark[:77].rstrip() + "..."

    return {
        "房型名称": name,
        "窗户信息": (item.get("窗户信息") or "").strip(),
        "价格": (item.get("价格") or "").strip(),
        "剩余房间": (item.get("剩余房间") or "").strip(),
        "备注": remark,
    }


def build_output(room_list: list[dict]) -> dict:
    now = datetime.now()
    search_time = now.strftime("%Y-%m-%d %H:%M:%S")
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    def _main_name(n):
        return (n or "").split("「")[0].strip()
    room_names_with_price = {_main_name(r.get("房型名称")) for r in room_list if (r.get("价格") or "").strip()}
    short_policy_only = ("含早餐 免费取消", "免费取消", "不可取消", "无早餐 不可取消")

    def _is_likely_wrong(item):
        name = _main_name(item.get("房型名称"))
        price = (item.get("价格") or "").strip()
        remark = (item.get("备注") or "").strip()
        if price or name not in room_names_with_price:
            return False
        if len(remark) > 20:
            return False
        return remark in short_policy_only or any(p in remark for p in short_policy_only)

    filtered_rooms = [r for r in room_list if not _is_likely_wrong(r)]
    contracted = [_contract_room_item(r) for r in filtered_rooms]
    return {
        "搜索时间": search_time,
        "入住日期": today,
        "离店日期": tomorrow,
        "地址": "",
        "酒店名称": "",
        "房型总数": len(contracted),
        "房型列表": contracted,
    }


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = os.path.join(base, "source.xml")
        if not os.path.isfile(path):
            path = os.path.join(base, "elements.json")
    if not os.path.isfile(path):
        print(f"文件不存在: {path}")
        print("用法: python 2.py [source.xml 或 elements.json]")
        print("  source.xml = Appium Inspector → Source 标签 → Download XML 保存")
        print("  elements.json = 元素数组，每项为 key-value 数组")
        sys.exit(1)

    if path.lower().endswith(".xml"):
        elements = load_elements_from_xml(path)
    else:
        elements = load_elements(path)
    print(f"已加载 {len(elements)} 个元素")
    rooms = parse_rooms_from_elements(elements)
    data = build_output(rooms)
    out_path = os.path.join(base, "2.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"解析出 {len(rooms)} 个房型套餐，已写入: {out_path}")


if __name__ == "__main__":
    main()
