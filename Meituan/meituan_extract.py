"""
meituan_extract.py：美团酒店房型套餐提取并汇总为 1.json。

仿照 3.py + 1.py 流程，针对美团 App：
- 可从本地 XML 文件（如 xml/22.xml）解析，或从已连接设备 dump UI 后解析；
- --device 时会自动下滑多屏、合并去重后输出；
- 输出与 1.json 同结构的 JSON 到 Meituan/1.json。

用法：
  python meituan_extract.py                    # 使用默认 XML 路径 ../xml/22.xml
  python meituan_extract.py ../xml/22.xml      # 指定 XML 文件
  python meituan_extract.py --device           # 从设备多屏下滑抓取并解析（需 uiautomator2，请先手动切到「预订」Tab 并确保首屏可见第一个房型）
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

# 本脚本所在目录即 Meituan/
MEITUAN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MEITUAN_DIR)
if MEITUAN_DIR not in sys.path:
    sys.path.insert(0, MEITUAN_DIR)


def _date_md_to_iso(md: str, ref: datetime) -> str:
    """把 "2月6日" 或 "02月06日" 转为 YYYY-MM-DD。"""
    m = re.match(r"(\d{1,2})月(\d{1,2})日?", md)
    if not m:
        return md
    try:
        month, day = int(m.group(1)), int(m.group(2))
        y = ref.year
        d = ref.replace(month=month, day=day)
        return d.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return md


def build_output_json(room_list: list[dict], page_info: dict | None = None) -> dict:
    """构造与 1.json 同结构的最终 JSON。"""
    info = page_info or {}
    now = datetime.now()
    search_time = now.strftime("%Y-%m-%d %H:%M:%S")
    check_in = info.get("入住日期") or ""
    check_out = info.get("离店日期") or ""
    if not check_in or not check_out:
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        if not check_in:
            check_in = today
        if not check_out:
            check_out = tomorrow
    if check_in and re.match(r"^\d{1,2}月\d{1,2}日", str(check_in)):
        check_in = _date_md_to_iso(check_in, now)
    if check_out and re.match(r"^\d{1,2}月\d{1,2}日", str(check_out)):
        check_out = _date_md_to_iso(check_out, now)

    # 每条套餐收缩：备注截短；并过滤全空条目（无价格、无剩余、无备注）
    contracted = []
    for r in room_list:
        if not (r.get("价格") or "").strip() and not (r.get("剩余房间") or "").strip() and not (r.get("备注") or "").strip():
            continue
        if "仅剩" in (r.get("房型名称") or ""):
            continue
        name = (r.get("房型名称") or "").strip()
        if len(name) > 24:
            name = name[:24].rstrip() + "…"
        remark = (r.get("备注") or "").strip()
        if len(remark) > 80:
            remark = remark[:77].rstrip() + "..."
        contracted.append({
            "房型名称": name,
            "窗户信息": (r.get("窗户信息") or "").strip(),
            "价格": (r.get("价格") or "").strip(),
            "剩余房间": (r.get("剩余房间") or "").strip(),
            "备注": remark,
        })

    # 同房型+同备注（前60字）视为同一套餐，只保留一条，优先保留有价格的（避免多屏/解析重复导致的无价重复条）
    remark_key_len = 20
    by_key: dict[tuple[str, str], dict] = {}
    for c in contracted:
        rk = (c["房型名称"], (c["备注"] or "")[:remark_key_len])
        if rk not in by_key:
            by_key[rk] = c
        else:
            cur = by_key[rk]
            if (cur.get("价格") or "").strip() and not (c.get("价格") or "").strip():
                continue
            if not (cur.get("价格") or "").strip() and (c.get("价格") or "").strip():
                by_key[rk] = c
            else:
                if (c.get("价格") or "").strip():
                    by_key[rk] = c
    contracted = list(by_key.values())

    return {
        "搜索时间": search_time,
        "入住日期": check_in,
        "离店日期": check_out,
        "地址": info.get("地址") or "",
        "酒店名称": info.get("酒店名称") or "",
        "房型总数": len(contracted),
        "房型列表": contracted,
    }


def get_xml_from_file(path: str) -> str:
    """从本地文件读取 XML 内容。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def get_xml_from_device(device_id: str | None = None) -> str:
    """从已连接设备 dump 当前 UI 树（需 uiautomator2）。"""
    try:
        import uiautomator2 as u2
        d = u2.connect(device_id) if device_id else u2.connect()
        xml = d.dump_hierarchy()
        return xml or ""
    except Exception as e:
        print(f"从设备获取 UI 树失败: {e}")
        return ""


def _merge_remark_key(remark: str) -> str:
    """合并用备注键：取前20字，覆盖面积+床型，且能区分无早餐/1份早餐/2份早餐（第20字不同）。"""
    return ((remark or "").strip())[:20]


def _room_dedupe_key(r: dict) -> tuple:
    """去重键：有价格用 (房型名称, 价格)，无价格用 (房型名称, 价格, 备注)。"""
    name = (r.get("房型名称") or "").strip()
    price = (r.get("价格") or "").strip()
    pk = ""
    if price and "¥" in price:
        m = re.search(r"¥\s*(\d+)", price)
        if m:
            pk = "¥" + m.group(1)
    if pk:
        return (name, pk)
    return (name, pk, (r.get("备注") or "").strip())


def _parse_bounds(bounds: str) -> tuple[int, int, int, int] | None:
    """解析 bounds 字符串 "[left,top][right,bottom]" -> (left, top, right, bottom)。"""
    if not bounds:
        return None
    m = re.findall(r"\d+", bounds)
    if len(m) != 4:
        return None
    return (int(m[0]), int(m[1]), int(m[2]), int(m[3]))


def _find_expand_room_buttons(xml_str: str) -> list[tuple[int, int]]:
    """
    在 UI 树 XML 中查找两类折叠展开入口，返回可点击中心点列表：
    1）「查看全部X个房型」— 展开更多房型；
    2）「剩余X个房型已订完」— 展开/展示已订完的房型信息。
    用可点击的父节点 bounds 点击（TextView 本身多为 clickable=false）。
    """
    import xml.etree.ElementTree as ET
    if not xml_str or "<" not in xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return []
    # 建父节点映射，便于向上找 clickable 的节点
    parent_map: dict[int, object] = {}
    for p in root.iter():
        for c in p:
            parent_map[id(c)] = p

    centers: list[tuple[int, int]] = []
    seen_centers: set[tuple[int, int]] = set()

    for elem in root.iter():
        text = (elem.attrib.get("text") or elem.attrib.get("content-desc") or "").strip()
        if not text:
            continue
        is_expand = ("查看全部" in text and "房型" in text) or ("剩余" in text and "房型" in text)
        if not is_expand:
            continue
        # 优先用 clickable="true" 的父节点 bounds，否则用当前节点
        node = elem
        b = _parse_bounds(elem.attrib.get("bounds", ""))
        while b and id(node) in parent_map:
            parent = parent_map[id(node)]
            attrib = getattr(parent, "attrib", {})
            if attrib.get("clickable") == "true":
                p_bounds = _parse_bounds(attrib.get("bounds", ""))
                if p_bounds:
                    b = p_bounds
                    break
            node = parent
        if not b:
            continue
        l, t, r, bot = b
        if t < 200 or t > 2600:
            continue
        cx = (l + r) // 2
        cy = (t + bot) // 2
        if (cx, cy) in seen_centers:
            continue
        seen_centers.add((cx, cy))
        centers.append((cx, cy))
    return centers


def _find_collapsed_chevron_buttons(xml_str: str, skip_rooms: set | None = None) -> list[tuple[int, int, str]]:
    """
    基于位置关系判断房型卡片折叠/展开状态，只返回折叠卡片的点击坐标。
    返回 [(x, y, room_name), ...] 按 Y 从上到下排序。
    skip_rooms: 已尝试点击过但无效的房型名集合，跳过不再点。
    """
    import xml.etree.ElementTree as ET
    if not xml_str or "<" not in xml_str:
        return []
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return []
    if skip_rooms is None:
        skip_rooms = set()

    parent_map: dict[int, object] = {}
    for p in root.iter():
        for c in p:
            parent_map[id(c)] = p

    # ── 1. 收集所有文本节点 ──
    text_nodes: list[tuple[str, tuple[int, int, int, int]]] = []
    for elem in root.iter():
        a = getattr(elem, "attrib", {})
        t = (a.get("text") or a.get("content-desc") or "").strip()
        if t:
            b = _parse_bounds(a.get("bounds", ""))
            if b:
                text_nodes.append((t, b))

    # ── 2. 找所有「房型名」位置 ──
    ROOM_KW = ("双床", "大床", "单人", "特惠", "商务", "豪华", "标准", "家庭", "亲子", "套房", "精品")
    room_names: list[tuple[str, int]] = []
    for t, b in text_nodes:
        if "房" in t and any(k in t for k in ROOM_KW):
            room_names.append((t, (b[1] + b[3]) // 2))
    room_names.sort(key=lambda x: x[1])

    # ── 3. 找所有 49x49 左右的右侧小图标（折叠箭头特征） ──
    arrows: list[tuple[object, tuple[int, int, int, int]]] = []
    for elem in root.iter():
        a = getattr(elem, "attrib", {})
        cls = a.get("class") or ""
        if "ImageView" not in (elem.tag or "") and "ImageView" not in cls:
            continue
        b = _parse_bounds(a.get("bounds", ""))
        if not b:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        # 折叠箭头尺寸约 49x49，放宽到 35-65
        if w < 35 or w > 65 or h < 35 or h > 65:
            continue
        # 必须在屏幕右侧（x right > 950）
        if b[2] < 950:
            continue
        ay = (b[1] + b[3]) // 2
        # 排除状态栏和底部导航栏
        if ay < 150 or ay > 2350:
            continue
        # 排除「筛选」旁的箭头
        near_filter = False
        for t, tb in text_nodes:
            if "筛选" in t and abs((tb[1] + tb[3]) // 2 - ay) < 100:
                near_filter = True
                break
        if near_filter:
            continue
        arrows.append((elem, b))

    # ── 4. 对每个箭头判断折叠/展开 ──
    EXPANDED_KW = ("无早餐", "份早餐", "不可取消", "可取消")
    results: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()

    for elem, ab in arrows:
        ay = (ab[1] + ab[3]) // 2

        # 只处理屏幕中间区域（250 < y < 2000）的箭头
        # 顶部/底部的卡片可能展开的子项已滚出屏幕，无法可靠判断状态
        if ay < 250 or ay > 2000:
            continue

        # 找同行房型名（Y 差 ≤ 100px）
        matched_room = None
        matched_room_y = None
        for rname, ry in room_names:
            if abs(ry - ay) <= 100:
                matched_room = rname
                matched_room_y = ry
                break
        if matched_room is None:
            continue
        if matched_room in skip_rooms:
            continue

        # 检查该房型是否「已订完」且不可展开 → 看箭头同行有没有「已订完」文字
        is_sold_out = False
        for t, tb in text_nodes:
            if "已订完" in t and abs((tb[1] + tb[3]) // 2 - ay) <= 60:
                is_sold_out = True
                break

        # 当前卡片的下边界 = 下一个房型名的 Y
        next_room_y = 9999
        for rname, ry in room_names:
            if ry > matched_room_y + 120:
                next_room_y = ry
                break

        # 在「当前房型名」到「下一个房型名」之间，检查是否有已展开标记
        is_expanded = False
        for t, tb in text_nodes:
            ty = (tb[1] + tb[3]) // 2
            if matched_room_y + 30 < ty < next_room_y - 30:
                if any(kw in t for kw in EXPANDED_KW):
                    is_expanded = True
                    break
        if is_expanded:
            continue

        # 找可点击父节点
        node = elem
        click_b = ab
        while id(node) in parent_map:
            parent = parent_map[id(node)]
            pa = getattr(parent, "attrib", {})
            if pa.get("clickable") == "true":
                pb = _parse_bounds(pa.get("bounds", ""))
                if pb:
                    click_b = pb
                    break
            node = parent

        cx, cy = (click_b[0] + click_b[2]) // 2, (click_b[1] + click_b[3]) // 2
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))
        tag = "已订完" if is_sold_out else "折叠"
        results.append((cx, cy, matched_room))
        print(f"    [chevron] {tag}「{matched_room}」→ ({cx},{cy})")

    # 按 Y 从上到下排序
    results.sort(key=lambda r: r[1])
    return results


def _expand_folded_rooms(d, device_id: str | None, max_rounds: int = 5) -> None:
    """采集前多次 dump 并点击「查看全部X个房型」「剩余X个房型已订完」，展开折叠房型。"""
    for round_no in range(max_rounds):
        if round_no == 0:
            time.sleep(1.8)
        try:
            xml = d.dump_hierarchy()
        except Exception as e:
            print(f"  展开折叠: 第{round_no+1}轮 dump 异常: {e}")
            break
        if not xml or "<" not in xml:
            print(f"  展开折叠: 第{round_no+1}轮 未获取到 UI 树")
            break
        buttons = _find_expand_room_buttons(xml)
        print(f"  展开折叠: 第{round_no+1}轮 找到 {len(buttons)} 处")
        if not buttons:
            break
        for x, y in buttons[:5]:
            try:
                d.click(x, y)
                time.sleep(0.8)
            except Exception as e:
                print(f"    点击 ({x},{y}) 异常: {e}")
        time.sleep(0.5)


def collect_all_rooms_from_device(
    device_id: str | None = None,
    max_swipes: int = 40,
    swipe_sleep: float = 1.0,
    scroll_to_top: bool = False,
):
    """
    从设备反复 dump + 下滑，收集多屏房型套餐并去重。
    返回 (all_rooms, page_info)。
    """
    from parse_meituan_xml import parse_meituan_rooms_from_xml, extract_meituan_page_info

    try:
        import uiautomator2 as u2
    except ImportError:
        print("需要安装 uiautomator2: pip install uiautomator2")
        return [], {}

    try:
        d = u2.connect(device_id) if device_id else u2.connect()
    except Exception as e:
        print(f"连接设备失败: {e}")
        return [], {}

    all_rooms: list[dict] = []
    seen_keys: set = set()
    page_info: dict = {}
    last_xml_hash = None
    same_hash_count = 0

    print("  请确保：1) 当前在「预订」Tab  2) 屏幕上已能看到第一个房型卡（如有必要请先手动滑到房型列表顶部）。")
    if scroll_to_top:
        try:
            for _ in range(2):
                d.swipe(500, 800, 500, 1800, duration=0.4)
                time.sleep(0.3)
            time.sleep(1.2)
        except Exception as e:
            print(f"  滚回顶部异常（继续采集）: {e}")
    time.sleep(1.0)

    for i in range(max_swipes):
        if i == 0:
            time.sleep(1.5)
        try:
            xml = d.dump_hierarchy()
        except Exception as e:
            if i == 0:
                print(f"dump 失败: {e}")
            break
        if not xml or "<" not in xml:
            if i == 0:
                print("未获取到 UI 树，请确认当前在美团酒店房型列表页。")
            break

        # 效仿 3.py：每屏先查找并点击「查看全部X个房型」「剩余X个房型已订完」，以及房型卡片右侧折叠箭头（碰到的折叠需展开）
        # 保存每屏 XML 便于调试
        debug_screen = os.path.join(MEITUAN_DIR, f"debug_screen_{i+1}.xml")
        try:
            with open(debug_screen, "w", encoding="utf-8", errors="replace") as f:
                f.write(xml[:120000] if len(xml) > 120000 else xml)
        except Exception:
            pass
        expand_buttons = _find_expand_room_buttons(xml)
        chevron_buttons = _find_collapsed_chevron_buttons(xml)
        # chevron_buttons 是 [(x,y,name),...], expand_buttons 是 [(x,y),...]
        chevron_coords = [(x, y) for x, y, _ in chevron_buttons]
        all_expand = expand_buttons + chevron_coords
        if all_expand:
            print(f"  第{i+1}屏: 找到 {len(expand_buttons)} 处折叠文案、{len(chevron_buttons)} 处折叠箭头，逐个点击…")
            # 从上往下逐个点击，每次点完重新dump获取新坐标
            tried_rooms: set[str] = set()
            for click_round in range(10):
                new_chevrons = _find_collapsed_chevron_buttons(xml, skip_rooms=tried_rooms)
                new_expands = _find_expand_room_buttons(xml)
                if not new_chevrons and not new_expands:
                    break
                # 优先点「查看全部」类文案按钮
                if new_expands:
                    x, y = new_expands[0]
                    print(f"    round {click_round+1}: 文案展开 ({x},{y})")
                else:
                    # 点最上面的折叠箭头（已按 Y 从上到下排序）
                    x, y, rname = new_chevrons[0]
                    tried_rooms.add(rname)
                    print(f"    round {click_round+1}: 箭头「{rname}」({x},{y})")
                try:
                    d.click(x, y)
                except Exception:
                    pass
                time.sleep(0.8)
                try:
                    xml = d.dump_hierarchy()
                except Exception:
                    break
                if not xml or "<" not in xml:
                    break
            if not xml or "<" not in xml:
                break
        elif i == 0 and not all_expand:
            debug_path = os.path.join(MEITUAN_DIR, "debug_first_dump.xml")
            try:
                with open(debug_path, "w", encoding="utf-8", errors="replace") as f:
                    f.write(xml[:120000] if len(xml) > 120000 else xml)
                print(f"  提示: 首屏未找到展开按钮，已保存 UI 树到 {debug_path}；若当前页确有「查看全部X个房型」可发该文件排查。")
            except Exception:
                pass

        if i == 0:
            page_info = extract_meituan_page_info(xml)

        rooms = parse_meituan_rooms_from_xml(xml)
        xml_hash = hash(xml)

        for r in rooms:
            key = _room_dedupe_key(r)
            name = (r.get("房型名称") or "").strip()
            remark = (r.get("备注") or "").strip()
            merge_key = (name, _merge_remark_key(remark))
            found_idx = None
            for j, ex in enumerate(all_rooms):
                if (ex.get("房型名称") or "").strip() == name and _merge_remark_key(ex.get("备注") or "") == merge_key[1]:
                    found_idx = j
                    break
            if found_idx is not None:
                ex = all_rooms[found_idx]
                ex_price = (ex.get("价格") or "").strip()
                r_price = (r.get("价格") or "").strip()
                if not ex_price and r_price:
                    old_key = _room_dedupe_key(ex)
                    seen_keys.discard(old_key)
                    all_rooms[found_idx] = dict(r)
                    seen_keys.add(key)
                elif ex_price and not r_price:
                    continue
                else:
                    continue
            else:
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_rooms.append(dict(r))

        print(f"  第{i+1}屏: 本屏识别 {len(rooms)} 条, 累计 {len(all_rooms)} 条")

        if xml_hash == last_xml_hash:
            same_hash_count += 1
            if same_hash_count >= 2:
                print("  连续 2 屏 UI 相同，已滑到底，结束采集。")
                break
        else:
            same_hash_count = 0
        last_xml_hash = xml_hash

        try:
            d.swipe(500, 1700, 500, 700, duration=0.5)
        except Exception as e:
            print(f"  滑动异常: {e}")
            break
        time.sleep(swipe_sleep)

    return all_rooms, page_info


def run_from_xml_file(xml_path: str, out_json_path: str | None = None) -> str:
    """
    从指定 XML 文件解析套餐并写入 JSON。
    返回生成的 JSON 文件路径。
    """
    if not os.path.isfile(xml_path):
        raise FileNotFoundError(f"XML 文件不存在: {xml_path}")
    xml_str = get_xml_from_file(xml_path)
    return run_from_xml_string(xml_str, out_json_path or os.path.join(MEITUAN_DIR, "1.json"))


def run_from_xml_string(xml_str: str, out_json_path: str | None = None) -> str:
    """从 XML 字符串解析套餐并写入 JSON。返回生成的 JSON 文件路径。"""
    from parse_meituan_xml import parse_meituan_rooms_from_xml, extract_meituan_page_info

    rooms = parse_meituan_rooms_from_xml(xml_str)
    page_info = extract_meituan_page_info(xml_str)
    data = build_output_json(rooms, page_info)
    out_path = out_json_path or os.path.join(MEITUAN_DIR, "1.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return out_path


def main():
    use_device = "--device" in sys.argv or "-d" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--device", "-d")]

    if use_device:
        all_rooms, page_info = collect_all_rooms_from_device()
        if not all_rooms and not page_info:
            print("未采集到任何数据，请确保：1) 已安装 uiautomator2  2) 设备已连接  3) 当前在美团酒店房型列表页。")
            sys.exit(1)
        data = build_output_json(all_rooms, page_info)
        out_path = os.path.join(MEITUAN_DIR, "1.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已从设备多屏采集并生成: {out_path}（共 {len(all_rooms)} 条套餐）")
        return

    # 默认使用项目下的 xml/22.xml
    xml_path = os.path.join(PROJECT_ROOT, "xml", "22.xml")
    if args:
        xml_path = os.path.abspath(args[0])
    out_path = run_from_xml_file(xml_path)
    print(f"已从 {xml_path} 解析套餐并生成: {out_path}")


if __name__ == "__main__":
    main()
