"""
3.py：从首页到最终房型 JSON 的全流程自动化脚本。

流程概述（基于你的描述 & mtl/*.xml）：
1. 打开携程 App，停在首页（对应 mtl/首页.xml）；
2. 点击左上角的「酒店」入口，进入酒店搜索页（mtl/搜索.xml）；
3. 在「城市」输入框里填写地址，在「位置/品牌/酒店」输入框里填写酒店名称；
4. 点击「查询」/「搜索」按钮，进入搜索结果页（mtl/查询结果.xml）；
5. 在搜索结果中点击目标酒店卡片，进入酒店详情页 → 房型页（mtl/折叠房型.xml）；
6. 在房型页中点击「酒店热卖！查看已订完房型」等折叠入口；
7. 将折叠区内每个房型的「展开套餐」按钮全部点开；
8. 调用 1.py 的 collect_all_rooms + build_output_json，生成 1.json。

说明：
- 由于真机分辨率 / UI 细节存在差异，下面代码里所有「选择具体节点」的部分都尽量基于
  uiautomator dump 的 text / content-desc / resource-id，而不是死写坐标；
- 但在你本机上，可能仍然需要根据 mtl/首页.xml / 搜索.xml / 查询结果.xml / 折叠房型.xml
  微调若干关键词或增加 fallback 的坐标点击逻辑。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Iterable, Optional, Tuple

import uiautomator2 as u2

from phone_agent.device_factory import get_device_factory


# ------------- 通用工具函数 -------------

def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    """解析 uiautomator bounds 字符串: "[l,t][r,b]" -> (l, t, r, b)。"""
    if not bounds:
        return None
    m = re.findall(r"\d+", bounds)
    if len(m) != 4:
        return None
    l, t, r, b = map(int, m)
    return l, t, r, b


def _center_of(bounds: str) -> Optional[Tuple[int, int]]:
    """返回 bounds 中心点坐标 (x, y)。"""
    b = _parse_bounds(bounds)
    if not b:
        return None
    l, t, r, btm = b
    return (l + r) // 2, (t + btm) // 2


def _resolve_device_id():
    """复用 1.py 的思路：若只连了一台设备，返回其 device_id。"""
    device_factory = get_device_factory()
    try:
        devices = device_factory.list_devices()
    except Exception:
        devices = []
    connected = [d for d in devices if getattr(d, "status", None) == "device"]
    if len(connected) == 1:
        return connected[0].device_id
    if len(connected) > 1:
        print(f"检测到多台设备: {[d.device_id for d in connected]}，使用第一台。")
        return connected[0].device_id
    # 回退：直接用 adb devices（交给 1.py 里已有逻辑会更好，这里先简单处理）
    return None


def _get_xml_via_adb(device_id: Optional[str] = None) -> str:
    """当 device_factory 取 UI 树失败时，直接走 adb dump 兜底（增强重试与多路径）。"""
    adb = ["adb"]
    if device_id:
        adb = ["adb", "-s", device_id]
    paths = ["/sdcard/window_dump.xml", "/sdcard/__phone_agent_window_dump.xml"]

    def _dump_and_cat(path: str, compressed: bool = False) -> str:
        cmd = ["uiautomator", "dump", "--compressed", path] if compressed else ["uiautomator", "dump", path]
        r1 = subprocess.run(
            adb + ["shell", *cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        if r1.returncode != 0:
            return ""
        r2 = subprocess.run(
            adb + ["shell", "cat", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
        )
        xml = r2.stdout or ""
        return xml if "<hierarchy" in xml else ""

    for _ in range(2):
        for p in paths:
            try:
                xml = _dump_and_cat(p, compressed=False)
                if xml:
                    return xml
                xml = _dump_and_cat(p, compressed=True)
                if xml:
                    return xml
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                continue
        time.sleep(0.25)
    return ""


def _get_xml(device_factory, device_id: Optional[str] = None, retry: int = 2, sleep_sec: float = 0.3) -> str:
    """多次尝试获取当前 UI 树 XML（优先走 adb 直连，失败再走 backend）。"""
    for i in range(retry):
        xml = _get_xml_via_adb(device_id)
        if xml:
            return xml
        try:
            xml = device_factory.get_ui_hierarchy_xml(device_id)
        except Exception:
            xml = ""
        if xml:
            return xml
        time.sleep(sleep_sec)
    return ""


def _safe_get_ui_xml_via_u2(device_id: Optional[str], retry: int = 2, sleep_sec: float = 0.25) -> str:
    """
    使用 uiautomator2 获取完整 UI 树（含 WebView 内容）。
    携程房型列表在 WebView 中渲染，adb uiautomator dump 看不到，
    uiautomator2.dump_hierarchy() 可以抓到。
    """
    for attempt in range(retry):
        try:
            d = u2.connect(device_id) if device_id else u2.connect()
            xml = d.dump_hierarchy()
            if xml and ("<hierarchy" in xml or "<node" in xml):
                return xml
        except Exception:
            # 第一轮失败时可以打印一条日志，但这里保持安静，避免刷屏
            pass
        time.sleep(sleep_sec)
    return ""


def _get_screen_size_via_adb(device_id: Optional[str] = None) -> Tuple[int, int]:
    """
    通过 adb shell wm size 获取当前设备分辨率。
    若失败则回退为典型的 1080x2400。
    """
    adb = ["adb"]
    if device_id:
        adb = ["adb", "-s", device_id]
    try:
        r = subprocess.run(
            adb + ["shell", "wm", "size"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
        )
        out = (r.stdout or "") + (r.stderr or "")
        # 形如: Physical size: 1080x2400
        m = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", out)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return 1080, 2400


def _iter_nodes(root: ET.Element) -> Iterable[ET.Element]:
    """
    遍历 UI 树中的可读节点。

    兼容两类 dump 结构：
    1) 传统 uiautomator：大量 <node .../>；
    2) 部分 ROM/工具链：直接是 android.view.* / android.widget.* 标签。
    """
    if root.tag == "hierarchy":
        for e in root.iter():
            if e is root:
                continue
            yield e
        return
    # 兜底：非 hierarchy 根节点也尽量遍历所有子节点
    for e in root.iter():
        yield e


def _match_text(s: str, keywords: Iterable[str]) -> bool:
    s = (s or "").strip()
    return any(k in s for k in keywords)


def _find_clickable_nodes_by_text(xml: str, keywords: Iterable[str]) -> list[ET.Element]:
    """在 XML 里根据 text / content-desc 包含关键词找到候选节点。"""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except Exception:
        return []
    result = []
    for node in _iter_nodes(root):
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        if _match_text(text, keywords) or _match_text(desc, keywords):
            result.append(node)
    return result


def _tap_nodes_center(device_factory, nodes: Iterable[ET.Element], device_id: Optional[str] = None, delay: float = 0.5):
    """依次点击给定节点的中心点。"""
    for node in nodes:
        bounds = node.attrib.get("bounds", "")
        center = _center_of(bounds)
        if not center:
            continue
        x, y = center
        try:
            device_factory.tap(x, y, device_id=device_id)
            time.sleep(delay)
        except Exception as e:
            print(f"tap 失败: {e}")


def _type_text_with_adb_keyboard(device_factory, text: str, device_id: Optional[str]) -> bool:
    """统一封装 ADB 键盘输入，返回是否成功。"""
    try:
        original_ime = device_factory.detect_and_set_adb_keyboard(device_id)
        time.sleep(0.2)
        device_factory.clear_text(device_id)
        time.sleep(0.1)
        device_factory.type_text(text, device_id)
        time.sleep(0.2)
        device_factory.restore_keyboard(original_ime, device_id)
        return True
    except Exception as e:
        print(f"  - ADB 键盘输入失败: {e}")
        return False


def _fill_search_conditions_by_coordinates(device_factory, device_id: Optional[str], city: str, hotel_keyword: str) -> bool:
    """
    当搜索页 XML 拿不到时，按已知页面坐标执行保底流程：
    - 先点酒店名框进入“名称搜索页”
    - 在名称搜索页输入酒店名
    - 点候选项返回搜索页
    - 点大「查 询」按钮
    坐标来自 mtl/搜索.xml 与 mtl/名称搜索.xml。
    """
    print("  - 进入坐标保底流程（无 UI 树）。")
    try:
        # 1) 先做地址搜索（城市）
        # 1.1 搜索页城市框（mtl/搜索.xml: [93,666][253,730]）
        device_factory.tap(173, 698, device_id=device_id)
        time.sleep(0.8)
        # 1.2 地址搜索页输入框（mtl/地址搜索.xml EditText: [130,244][954,343]）
        device_factory.tap(542, 293, device_id=device_id)
        time.sleep(0.3)
        ok_city = _type_text_with_adb_keyboard(device_factory, city, device_id)
        if not ok_city:
            return False
        print(f"  - 已输入地址(坐标模式): {city}")
        time.sleep(0.8)
        # 1.3 点地址候选（优先“上海”所在区域）
        # mtl/地址搜索.xml: 文本“上海”约 [521,719][597,811]
        device_factory.tap(560, 765, device_id=device_id)
        time.sleep(1.0)

        # 2) 再做酒店名称搜索
        # 搜索页酒店名框（mtl/搜索.xml: [417,666][833,730]）
        # 注意这里显式点击搜索框区域，避免误触上方“钟点房”tab（y≈491-557）
        device_factory.tap(625, 698, device_id=device_id)
        time.sleep(0.8)

        # 2.1 名称搜索页输入框（mtl/名称搜索.xml EditText: [130,113][809,212]）
        device_factory.tap(470, 162, device_id=device_id)
        time.sleep(0.3)
        ok = _type_text_with_adb_keyboard(device_factory, hotel_keyword, device_id)
        if not ok:
            return False
        print(f"  - 已输入酒店名(坐标模式): {hotel_keyword}")
        time.sleep(0.8)

        # 2.2 按你的要求：在名称搜索页“点击下面第一个结果”
        # 你提供的 XML 中第一条结果约在 [35,357][893,470]，
        # 其标题就是「美利居酒店(上海城市中心人民广场店)」。
        device_factory.tap(464, 413, device_id=device_id)
        time.sleep(1.0)
        print("  - 已点击名称搜索页第一个结果。")

        # 3) 点击第一个结果后通常会直接进入详情页或结果页，因此不再额外点击查询，
        # 避免误触其它控件（例如钟点房）。
        return True
    except Exception as e:
        print(f"  - 坐标保底流程失败: {e}")
        return False


def _tap_first_result_and_verify_leave_search_page(
    device_factory,
    device_id: Optional[str],
    x: int,
    y: int,
    page_marker: str = "hotel_search_page_view_root",
) -> bool:
    """点击“第一个结果”后，校验是否离开搜索子页。"""
    try:
        device_factory.tap(x, y, device_id=device_id)
        time.sleep(0.8)
        xml_after = _get_xml(device_factory, device_id, retry=1, sleep_sec=0.2)
        if not xml_after:
            print("  - 已点击第一个结果，但无法校验页面是否跳转。")
            return False
        return page_marker not in xml_after
    except Exception:
        return False


# ------------- 分步操作：首页 → 搜索 → 结果 → 详情 -------------

def go_to_hotel_search(device_factory, device_id: Optional[str] = None):
    """
    从首页点击左上角「酒店」入口，进入酒店搜索页。
    依赖 mtl/首页.xml 的结构：左上角有一个“酒店”tab 或按钮。
    """
    print("[步骤] 首页 → 酒店搜索页")
    xml = _get_xml(device_factory, device_id)
    if not xml:
        # UI 树拿不到时，直接按首页酒店宫格坐标兜底点击一次
        print("提示: 首页未拿到 UI 树，改用坐标兜底点击『酒店』入口。")
        try:
            device_factory.tap(130, 310, device_id=device_id)
            time.sleep(1.0)
            return True
        except Exception:
            return False
    try:
        root = ET.fromstring(xml)
    except Exception:
        print("提示: 首页 UI 树解析失败，无法点击『酒店』入口。")
        return False

    # 1) 优先精确匹配首页酒店入口（最稳定）
    exact_nodes: list[ET.Element] = []
    for node in _iter_nodes(root):
        rid = (node.attrib.get("resource-id") or "").strip()
        cdesc = (node.attrib.get("content-desc") or "").strip()
        combo = f"{rid} {cdesc}"
        if "home_grid_hotel_widget" in combo:
            exact_nodes.append(node)
    if exact_nodes:
        _tap_nodes_center(device_factory, [exact_nodes[0]], device_id)
        return True

    # 2) 次优先：文本/描述里有“酒店”
    nodes = _find_clickable_nodes_by_text(xml, ["酒店", "酒店·民宿", "酒店/民宿"])
    if nodes:
        # 只点最靠左上的一个
        nodes.sort(key=lambda n: _parse_bounds(n.attrib.get("bounds", "")) or (9999, 9999, 9999, 9999))
        _tap_nodes_center(device_factory, [nodes[0]], device_id)
        return True

    # 3) 最后兜底：按首页常见坐标点“酒店”宫格（参考 mtl/首页.xml: [33,226][223,392]）
    try:
        device_factory.tap(130, 310, device_id=device_id)
        time.sleep(0.6)
        return True
    except Exception:
        print("提示: 未通过 resource-id/文本/坐标找到『酒店』入口。")
        return False


def fill_search_conditions(
    device_factory,
    device_id: Optional[str],
    city: str,
    hotel_keyword: str,
):
    """
    在搜索页（mtl/搜索.xml）中：
    - 第一个输入框：城市 / 地址；
    - 第二个输入框：「位置/品牌/酒店」。

    这里假设 device_factory 提供了 send_text / input_text 能力；
    如果当前封装没有，需要你在 device_factory 中补一层封装。
    """
    print("[步骤] 填写搜索条件")
    xml = _get_xml(device_factory, device_id)
    if not xml:
        print("未获取到搜索页 UI 树，改走坐标保底输入。")
        return _fill_search_conditions_by_coordinates(device_factory, device_id, city, hotel_keyword)

    try:
        root = ET.fromstring(xml)
    except Exception:
        print("搜索页 XML 解析失败，跳过自动填写。")
        return False

    city_box = None
    hotel_box = None
    # 若页面已带默认值（例如城市已是“上海”、酒店名已是目标值），可直接判定为已填写
    city_already_set = city in xml
    hotel_already_set = hotel_keyword in xml

    # 先用稳定的 resource-id/content-desc 命中输入框
    for node in _iter_nodes(root):
        rid = (node.attrib.get("resource-id") or "").strip()
        cdesc = (node.attrib.get("content-desc") or "").strip()
        combo = f"{rid} {cdesc}"
        if not city_box and "htl_x_inquire_querybox_destbox_exposure" in combo:
            city_box = node
        if not hotel_box and "htl_x_inquire_querybox_keybox_exposure" in combo:
            hotel_box = node

    # 再用文案兜底（仅当上面没命中）
    for node in _iter_nodes(root):
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        hint = text or desc
        if not hint:
            continue
        # 非常粗糙的匹配：需要根据 mtl/搜索.xml 进一步微调关键字
        if not city_box and _match_text(hint, ["城市", "目的地", "上海", "北京"]):
            city_box = node
        if not hotel_box and _match_text(hint, ["位置/品牌/酒店", "酒店名", "品牌"]):
            hotel_box = node

    city_input_ok = False
    hotel_input_ok = False

    # 点击并输入城市（先地址）
    if city_box:
        center = _center_of(city_box.attrib.get("bounds", ""))
        if center:
            device_factory.tap(*center, device_id=device_id)
            time.sleep(0.8)
            xml_city = _get_xml(device_factory, device_id, retry=1, sleep_sec=0.2)
            on_city_search_page = "hotel_search_page_view_root" in (xml_city or "")
            if on_city_search_page:
                try:
                    ok_city = _type_text_with_adb_keyboard(device_factory, city, device_id)
                    if ok_city:
                        print(f"  - 已在地址搜索页输入城市: {city}")
                        time.sleep(0.8)
                        # 按你的要求：直接点击下面第一个结果
                        jumped = _tap_first_result_and_verify_leave_search_page(
                            device_factory, device_id, x=560, y=765
                        )
                        if jumped:
                            city_input_ok = True
                            print("  - 已点击地址搜索页第一个结果（校验通过）。")
                        else:
                            print("  - 地址搜索页点击第一个结果后仍未离开该页。")
                except Exception:
                    pass
            else:
                # 未进入地址搜索页时，尽量直接输入；失败再看是否已有目标城市
                try:
                    device_factory.send_text(city, device_id=device_id)
                    city_input_ok = True
                    print(f"  - 已输入城市: {city}")
                except Exception:
                    if city_already_set:
                        city_input_ok = True
                        print(f"  - 城市已是目标值: {city}（跳过输入）")
                    else:
                        print("device_factory.send_text(city) 未实现，且当前页面城市不是目标值。")
            time.sleep(0.5)
    else:
        if city_already_set:
            city_input_ok = True
            print(f"  - 未定位到城市输入框，但页面城市已是: {city}")
        else:
            print("  - 未找到城市输入框。")

    # 点击并输入酒店关键字：
    # 1) 在搜索页点击 keybox 进入“名称搜索页”（mtl/名称搜索.xml）
    # 2) 在名称搜索页 EditText 输入关键词
    # 3) 点击候选酒店，回到搜索页再点查询
    if hotel_box:
        center = _center_of(hotel_box.attrib.get("bounds", ""))
        if center:
            device_factory.tap(*center, device_id=device_id)
            time.sleep(0.8)

            xml_name = _get_xml(device_factory, device_id)
            on_name_search_page = "hotel_search_page_view_root" in (xml_name or "")

            if on_name_search_page:
                # 在名称搜索页里输入
                try:
                    root_name = ET.fromstring(xml_name)
                except Exception:
                    root_name = None

                edit_node = None
                if root_name is not None:
                    for node in _iter_nodes(root_name):
                        cls = (node.attrib.get("class") or "").strip()
                        text = (node.attrib.get("text") or "").strip()
                        hint = (node.attrib.get("hint") or "").strip()
                        if "EditText" in cls and ("位置/品牌/酒店" in text or "位置/品牌/酒店" in hint):
                            edit_node = node
                            break

                if edit_node is not None:
                    edit_center = _center_of(edit_node.attrib.get("bounds", ""))
                    if edit_center:
                        device_factory.tap(*edit_center, device_id=device_id)
                        time.sleep(0.3)

                try:
                    ok = _type_text_with_adb_keyboard(device_factory, hotel_keyword, device_id)
                    if ok:
                        print(f"  - 已在名称搜索页输入酒店名: {hotel_keyword}")
                    time.sleep(0.8)
                    # 按你的要求：搜索时直接点击“下面第一个结果”
                    # 名称搜索页第一条结果大致在 [35,357][893,470]
                    jumped = _tap_first_result_and_verify_leave_search_page(
                        device_factory, device_id, x=464, y=413
                    )
                    if jumped:
                        hotel_input_ok = True
                        print("  - 已点击名称搜索页第一个结果（校验通过）。")
                    else:
                        print("  - 名称搜索页点击第一个结果后仍未离开该页。")
                except Exception:
                    if hotel_already_set:
                        hotel_input_ok = True
                        print(f"  - 酒店名已是目标值: {hotel_keyword}（跳过输入）")
                    else:
                        print("酒店名称输入失败（ADB 键盘/输入流程异常）。")
            else:
                # 没进入名称搜索页：保持旧逻辑兜底
                try:
                    device_factory.send_text(hotel_keyword, device_id=device_id)
                    hotel_input_ok = True
                    print(f"  - 已输入酒店名: {hotel_keyword}")
                except Exception:
                    if hotel_already_set:
                        hotel_input_ok = True
                        print(f"  - 酒店名已是目标值: {hotel_keyword}（跳过输入）")
                    else:
                        print("device_factory.send_text(hotel_keyword) 未实现，且当前页面酒店名不是目标值。")
            time.sleep(0.5)
    else:
        if hotel_already_set:
            hotel_input_ok = True
            print(f"  - 未定位到酒店输入框，但页面酒店名已是: {hotel_keyword}")
        else:
            print("  - 未找到酒店名称输入框。")

    # 严格模式：未完成输入就不继续点查询
    if not city_input_ok or not hotel_input_ok:
        print("提示: 输入未完成，已停止执行，不会点击查询。")
        return False

    # 点击「查询」/「搜索」按钮
    xml = _get_xml(device_factory, device_id)
    # 1) 优先按 resource-id/content-desc 匹配底部大「查 询」按钮：
    #    <android.view.ViewGroup ... content-desc="htl_x_inquire_querybox_searchbut_exposure"
    #    内部 TextView text="查 询"
    search_nodes: list[ET.Element] = []
    if xml:
        try:
            root2 = ET.fromstring(xml)
        except Exception:
            root2 = None
        if root2 is not None:
            for node in _iter_nodes(root2):
                rid = (node.attrib.get("resource-id") or "").strip()
                cdesc = (node.attrib.get("content-desc") or "").strip()
                combo = f"{rid} {cdesc}"
                if "htl_x_inquire_querybox_searchbut_exposure" in combo:
                    search_nodes.append(node)

    if search_nodes:
        # 一般只有一个，就点它的中心（大查询按钮）
        _tap_nodes_center(device_factory, [search_nodes[0]], device_id)
        print("  - 已点击查询按钮。")
        return True
    else:
        # 2) 退化：尝试用图片搜索按钮作为备选（放大镜图标）
        pic_nodes: list[ET.Element] = []
        if xml:
            try:
                root3 = ET.fromstring(xml)
            except Exception:
                root3 = None
            if root3 is not None:
                for node in _iter_nodes(root3):
                    rid = (node.attrib.get("resource-id") or "").strip()
                    cdesc = (node.attrib.get("content-desc") or "").strip()
                    combo = f"{rid} {cdesc}"
                    if "htl_x_inquire_querybox_picSearch_exposure" in combo:
                        pic_nodes.append(node)
        if pic_nodes:
            _tap_nodes_center(device_factory, [pic_nodes[0]], device_id)
            print("  - 已点击图片搜索按钮（查询兜底）。")
            return True
        else:
            # 3) 再退化：尝试用文本匹配“查询/搜索”
            text_nodes = _find_clickable_nodes_by_text(xml, ["查询", "查 询", "搜索", "查看结果", "开始搜索"])
            if text_nodes:
                def _bottom(n):
                    b = _parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0)
                    return b[3]

                text_nodes.sort(key=_bottom, reverse=True)
                _tap_nodes_center(device_factory, [text_nodes[0]], device_id)
                print("  - 已点击文本查询按钮（查询兜底）。")
                return True
            else:
                print("提示: 未找到『查询/搜索』按钮（包括 searchbut/picSearch），fill_search_conditions 暂时无法自动触发搜索。")
                return False


def open_hotel_from_result(device_factory, device_id: Optional[str], hotel_name: str):
    """
    在查询结果页（mtl/查询结果.xml）中点击目标酒店卡片。
    简单策略：查找 text / content-desc 包含指定酒店名称的节点，并点击其卡片区域。
    """
    print("[步骤] 查询结果页 → 进入酒店详情页")
    # 这里改为快速尝试，避免在 dump 阶段长时间卡住
    xml = _get_xml(device_factory, device_id, retry=2, sleep_sec=0.4)
    if not xml:
        print("未获取到查询结果页 UI 树，改用坐标兜底点击第一条酒店。")
        try:
            # mtl/查询结果.xml 第一条酒店卡片约为 [35,622][1080,1249]
            device_factory.tap(560, 935, device_id=device_id)
            time.sleep(1.0)
            return True
        except Exception:
            return False
    try:
        root = ET.fromstring(xml)
    except Exception:
        print("查询结果页 XML 解析失败。")
        return False

    # 若已在酒店详情页，则无需再点查询结果
    if (
        "htl_x_dtl_header_tab_exposure" in xml
        or "htl_x_dtl_rmlist" in xml
        or "查看房型" in xml
    ):
        print("当前已在酒店详情页，跳过结果页点击。")
        return True

    target_nodes = []
    for node in _iter_nodes(root):
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        clickable = (node.attrib.get("clickable") or "").strip().lower() == "true"
        b = _parse_bounds(node.attrib.get("bounds", ""))
        top = b[1] if b else 0
        # 只考虑列表区域（避开顶部搜索栏）且可点击节点
        if top < 300 or not clickable:
            continue
        if hotel_name and (hotel_name in text or hotel_name in desc):
            target_nodes.append(node)

    if not target_nodes:
        print(f"提示: 查询结果中未通过文本找到酒店『{hotel_name}』，可在 open_hotel_from_result 中补充其他匹配方式。")
        # 兜底：点第一条酒店卡片
        try:
            device_factory.tap(560, 935, device_id=device_id)
            time.sleep(1.0)
            return True
        except Exception:
            return False

    # 选取屏幕上方的一个（通常是第一条搜索结果卡片）
    def _top(n):
        b = _parse_bounds(n.attrib.get("bounds", "")) or (9999, 9999, 9999, 9999)
        return b[1]

    target_nodes.sort(key=_top)
    _tap_nodes_center(device_factory, [target_nodes[0]], device_id)
    return True


# ------------- 在房型页自动展开 -------------

def expand_all_sections_and_packages(device_factory, device_id: Optional[str] = None, max_rounds: int = 40):
    """
    在酒店详情页 → 房型页（mtl/折叠房型.xml）中：
    1. 点击「酒店热卖！查看已订完房型」等折叠入口；
    2. 将当前屏及下方所有「折叠房型」从上到下依次点开，直到连续两轮无可点或达到轮数上限。

    每轮只点一次（已订完/展开一个房型/查看其他价格），避免跳点；用「房型标题」做去重，防止滑动后同一张卡被再次点击而合上，保证 1.py 能采到全部展开的套餐。
    """
    min_rounds_before_stable = 18   # 至少滑过约 18 屏后再允许“连续 N 轮无可点”退出
    consecutive_no_click_to_exit = 6  # 需连续 6 轮无可点才退出，减少误判“已全部展开”
    print("[步骤] 房型页：展开折叠区 & 套餐")
    same_count = 0

    # 先尽量通过文本点击“房型”标签，避免死坐标在不同机型上失效
    try:
        # 房型页优先使用 uiautomator2 获取 UI 树（可见 WebView 内容）
        xml_pre = _safe_get_ui_xml_via_u2(device_id, retry=2, sleep_sec=0.2)
        if xml_pre:
            try:
                root_pre = ET.fromstring(xml_pre)
            except Exception:
                root_pre = None
            room_tab = None
            if root_pre is not None:
                for node in _iter_nodes(root_pre):
                    text = (node.attrib.get("text") or "").strip()
                    b = _parse_bounds(node.attrib.get("bounds", ""))
                    # 顶部导航区域通常在屏幕上方，限制一个较小的 top 范围，例如 < 420
                    if text == "房型" and b and b[1] < 420:
                        room_tab = node
                        break
            if room_tab is not None:
                _tap_nodes_center(device_factory, [room_tab], device_id, delay=0.8)
                print("  - 已点击顶部『房型』标签（按文本定位）。")
            else:
                # 若未找到顶部“房型”Tab，则优先尝试点击底部蓝色「查看房型」按钮
                clicked_room_entry = False
                if root_pre is not None:
                    candidates: list[ET.Element] = []
                    for node in _iter_nodes(root_pre):
                        text = (node.attrib.get("text") or "").strip()
                        b = _parse_bounds(node.attrib.get("bounds", ""))
                        if not text or not b:
                            continue
                        # 靠近底部区域的「查看房型」按钮
                        if "查看房型" in text and b[1] > 1600:
                            candidates.append(node)
                    if candidates:
                        # 选最靠下的一个按钮
                        candidates.sort(key=lambda n: (_parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0))[1], reverse=True)
                        _tap_nodes_center(device_factory, [candidates[0]], device_id, delay=0.8)
                        print("  - 已点击底部『查看房型』按钮（按文本定位）。")
                        clicked_room_entry = True

                if not clicked_room_entry:
                    # 仍然兜底一次：用屏幕尺寸估算「查看房型」大按钮位置，不再点顶部箭头
                    try:
                        sw, sh = _get_screen_size_via_adb(device_id)
                        btn_x = int(sw * 0.85)
                        btn_y = int(sh * 0.90)
                        device_factory.tap(btn_x, btn_y, device_id=device_id)
                        time.sleep(0.8)
                        print("  - 未在 XML 中找到『房型』文本/按钮，使用底部坐标兜底点击『查看房型』。")
                    except Exception:
                        print("  - 未在 XML 中找到『房型』文本，且坐标兜底点击失败。")
    except Exception:
        print("  - 点击『房型』标签时出现异常（忽略，继续后续流程）。")

    # 按你提供的折叠套餐.xml / 展开房型.xml 固定元素特征定位
    section_signatures = [
        "htl_x_dtl_rmlist_mbRmCard_mbmore_exposure",  # 酒店热卖！查看已订完房型
    ]
    room_card_signatures = [
        "htl_x_dtl_rmlist_mbRmCard_exposure",  # 房型卡片容器
    ]
    more_price_signatures = [
        "htl_x_dtl_rmlist_mbRmCard_more_exposure",  # 查看其他价格
    ]

    def _find_nodes_by_signatures(xml: str, signatures: list[str], min_top: int = 300) -> list[ET.Element]:
        if not xml:
            return []
        try:
            root = ET.fromstring(xml)
        except Exception:
            return []
        out: list[ET.Element] = []
        for node in _iter_nodes(root):
            rid = (node.attrib.get("resource-id") or "").strip()
            cdesc = (node.attrib.get("content-desc") or "").strip()
            combo = f"{rid} {cdesc}"
            if not any(sig in combo for sig in signatures):
                continue
            b = _parse_bounds(node.attrib.get("bounds", ""))
            if not b:
                continue
            if b[1] < min_top:
                continue
            out.append(node)
        return out

    def _text_has_codepoint(text: str, cp: int) -> bool:
        if not text:
            return False
        try:
            return any(ord(ch) == cp for ch in text)
        except Exception:
            return False

    def _expand_room_cards_by_state_icon(xml: str) -> int:
        """
        通过房型卡片内的状态图标点击“未展开”项：
        - 未展开图标：codepoint 990101；已展开：990100（不点，避免重复展开）
        - 每轮只点当前屏从上到下第一个未展开的房型，实现顺序展开、不跳点。
        """
        if not xml:
            return 0
        try:
            root = ET.fromstring(xml)
        except Exception:
            return 0

        # 找所有房型卡片容器，按从上到下、从左到右排序
        room_cards = _find_nodes_by_signatures(xml, room_card_signatures, min_top=260)
        room_cards.sort(key=lambda n: (_parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0))[1:3])
        for card in room_cards:
            collapsed_icon_node = None
            expanded_icon_found = False
            for sub in card.iter():
                t = (sub.attrib.get("text") or "").strip()
                if _text_has_codepoint(t, 990100):
                    expanded_icon_found = True
                elif _text_has_codepoint(t, 990101):
                    collapsed_icon_node = sub

            if expanded_icon_found or collapsed_icon_node is None:
                continue

            center = _center_of(collapsed_icon_node.attrib.get("bounds", ""))
            if not center:
                continue
            try:
                device_factory.tap(*center, device_id=device_id)
                time.sleep(0.25)
                return 1  # 只点当前屏从上到下第一个未展开的，下一轮再点下一个
            except Exception:
                pass
        return 0

    def _expand_packages_without_xml(max_screens: int = 8):
        """
        无法获取 UI 树时的纯坐标遍历：
        - 只点击一次“查看已订完房型”（避免展开后又被点回收起）；
        - 之后只做滑动遍历，不再固定坐标乱点，避免误入订购页。
        """
        print("进入无 UI 树保守模式：仅展开已订完入口，随后只滑动遍历，避免误触订购页。")
        # 先确保在“房型”tab（坐标使用相对屏幕高度，以适配不同机型）
        try:
            sw, sh = _get_screen_size_via_adb(device_id)
            tab_x = int(sw * 0.16)   # 顶部左侧一小块区域
            tab_y = int(sh * 0.12)   # 靠近顶部导航栏
            device_factory.tap(tab_x, tab_y, device_id=device_id)
            time.sleep(0.6)
        except Exception:
            pass

        soldout_opened = False
        for i in range(max_screens):
            try:
                if not soldout_opened:
                    # “酒店热卖！查看已订完房型”入口（只点一次）
                    sw, sh = _get_screen_size_via_adb(device_id)
                    sold_x = sw // 2
                    sold_y = int(sh * 0.88)  # 靠近底部但不贴边
                    device_factory.tap(sold_x, sold_y, device_id=device_id)
                    time.sleep(0.8)
                    soldout_opened = True
                    print("  - 已点击『查看已订完房型』（一次性）。")

                # 不再固定坐标点击房型/套餐区域，防止误点“领券订/预订”进入下单页
                # 仅做滚动遍历，让后续 1.py 负责采集已展开/可见内容
                sw, sh = _get_screen_size_via_adb(device_id)
                sx = sw // 2
                sy1 = int(sh * 0.82)
                sy2 = int(sh * 0.36)
                device_factory.swipe(sx, sy1, sx, sy2, device_id=device_id, duration=260)
                time.sleep(0.6)
            except Exception:
                continue

    no_xml_rounds = 0
    section_opened_once = False
    tapped_room_keys: set[str] = set()
    for round_idx in range(max_rounds):
        # 房型页优先使用 uiautomator2 获取 UI 树
        xml = _safe_get_ui_xml_via_u2(device_id, retry=3, sleep_sec=0.25)
        if not xml:
            no_xml_rounds += 1
            print("未获取到房型页 UI 树，改用坐标兜底点击折叠入口。")
            # 第一次就进入“无 UI 树遍历模式”，避免反复点同一入口导致展开后又关闭
            if no_xml_rounds == 1:
                _expand_packages_without_xml(max_screens=max(6, max_rounds + 2))
            print("无 UI 树模式展开完成，直接进入抓取。")
            break
            continue

        clicked_this_round = False

        # 每轮只做一次点击：已订完 / 展开一个房型 / 查看其他价格 三选一，从上到下顺序、不跳点、不重复
        # 1) 已订完入口：只点一次
        if not section_opened_once:
            section_nodes = _find_nodes_by_signatures(xml, section_signatures, min_top=1200)
            if section_nodes:
                section_nodes.sort(key=lambda n: (_parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0))[1], reverse=True)
                _tap_nodes_center(device_factory, [section_nodes[0]], device_id, delay=0.5)
                section_opened_once = True
                clicked_this_round = True
                print(f"第 {round_idx + 1} 轮：按元素特征点击已订完入口 1 个。")

        if not clicked_this_round:
            # 2) 按房型卡片“展开状态图标”点当前屏从上到下第一个未展开项
            expanded_count = _expand_room_cards_by_state_icon(xml)
            if expanded_count > 0:
                print(f"第 {round_idx + 1} 轮：按状态图标展开房型 1 个（从上到下顺序）。")
                clicked_this_round = True

        if not clicked_this_round:
            # 2.1) 兜底：若状态图标未命中，按“房型卡片右上角展开图标”几何位点击
            #      每轮只点当前屏从上到下第一个未展开的房型，顺序展开、不跳点、不重复
            room_cards = _find_nodes_by_signatures(xml, room_card_signatures, min_top=260)
            room_cards.sort(key=lambda n: (_parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0))[1:3])
            geom_click_count = 0
            for card in room_cards:
                cb = _parse_bounds(card.attrib.get("bounds", ""))
                if not cb:
                    continue
                l, t, r, b = cb
                if r - l < 400 or b - t < 120:
                    continue
                has_expanded_icon = False
                for sub in card.iter():
                    txt = (sub.attrib.get("text") or "").strip()
                    if _text_has_codepoint(txt, 990100):
                        has_expanded_icon = True
                        break
                if has_expanded_icon:
                    continue
                title = ""
                for sub in card.iter():
                    txt = (sub.attrib.get("text") or "").strip()
                    if txt and len(txt) > 3 and "房" in txt:
                        title = txt
                        break
                room_key = f"{title}|{l},{t},{r}"
                if room_key in tapped_room_keys:
                    continue
                # 不再用「同标题就跳过」：多个不同房型可能同名（如多个「高级大床房」），否则会卡在第二个同名校卡

                icon_x = r - 32
                icon_y = t + 20
                try:
                    device_factory.tap(icon_x, icon_y, device_id=device_id)
                    time.sleep(0.25)
                    tapped_room_keys.add(room_key)
                    geom_click_count = 1
                except Exception:
                    pass
                break  # 每轮只点一个，下一轮再点下一个
            if geom_click_count > 0:
                print(f"第 {round_idx + 1} 轮：几何兜底点击房型右上角图标 1 个（从上到下顺序）。")
                clicked_this_round = True

        if not clicked_this_round:
            # 3) 点“查看其他价格”入口（每轮只点当前屏从上到下第一个，顺序展开、不跳点）
            more_nodes = _find_nodes_by_signatures(xml, more_price_signatures, min_top=350)
            more_nodes = [n for n in more_nodes if _parse_bounds(n.attrib.get("bounds", "")) and (_parse_bounds(n.attrib.get("bounds", ""))[3] - _parse_bounds(n.attrib.get("bounds", ""))[1]) <= 80]
            if more_nodes:
                more_nodes.sort(key=lambda n: (_parse_bounds(n.attrib.get("bounds", "")) or (0, 0, 0, 0))[1])
                _tap_nodes_center(device_factory, [more_nodes[0]], device_id, delay=0.25)
                print(f"第 {round_idx + 1} 轮：点击『查看其他价格』1 个（从上到下顺序）。")
                clicked_this_round = True

        # 每轮固定做两次上滑，让列表真的在动、带出新内容，而不是只靠延时
        try:
            start_x = 540
            start_y = 1920
            end_x = 540
            end_y = 840
            device_factory.swipe(start_x, start_y, end_x, end_y, device_id=device_id, duration=260)
            time.sleep(0.35)
            device_factory.swipe(start_x, start_y, end_x, end_y, device_id=device_id, duration=260)
        except Exception:
            pass
        time.sleep(1.2)
        # 若本轮无可点，再多滑两次，明显多滚一截，触发懒加载后再 dump
        if not clicked_this_round:
            try:
                device_factory.swipe(start_x, start_y, end_x, end_y, device_id=device_id, duration=280)
                time.sleep(0.3)
                device_factory.swipe(start_x, start_y, end_x, end_y, device_id=device_id, duration=280)
            except Exception:
                pass
            time.sleep(1.0)

        # 至少完成 min_rounds_before_stable 轮后，且连续 consecutive_no_click_to_exit 轮无可点，才视为已全部展开并结束。
        if not clicked_this_round:
            same_count += 1
            if same_count >= consecutive_no_click_to_exit and (round_idx + 1) >= min_rounds_before_stable:
                print("已滑过多屏且连续多轮无可点展开位，认为已全部展开，结束展开步骤。")
                break
        else:
            same_count = 0
    else:
        # 达到 max_rounds 未 break 时提示
        if round_idx + 1 >= max_rounds:
            print("已达到最大展开轮数，若还有折叠项可适当增大 max_rounds。")


# ------------- 调用 1.py 抓取房型 -------------

def run_room_scrape_via_one_py() -> str:
    """
    以模块方式调用 1.py 的 collect_all_rooms + build_output_json。
    返回生成的 1.json 路径。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    one_path = os.path.join(base, "1.py")
    if not os.path.isfile(one_path):
        raise FileNotFoundError(f"未找到 1.py: {one_path}")

    spec = importlib.util.spec_from_file_location("_one_script", one_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    print("[步骤] 调用 1.py 收集房型套餐")
    rooms, page_info = mod.collect_all_rooms()
    data = mod.build_output_json(rooms, page_info)

    out_path = os.path.join(base, "1.json")
    import json

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已生成房型 JSON：{out_path}")
    return out_path


# ------------- 主流程 -------------

def main(expand_only: bool = False):
    """
    从首页开始，一路到生成 1.json 的主入口。

    注意：
    - 运行前请确保手机已连接且当前在携程首页；
    - city / hotel_name 两个参数可以根据实际需要调整或改成命令行参数。
    """
    device_factory = get_device_factory()
    device_id = _resolve_device_id()
    if not device_id:
        print("未检测到可用设备，请先通过 adb 连接手机。")
        return
    print(f"使用设备: {device_id}")

    # 你可以根据需要调整这里的示例参数
    # 目前先写死为「上海 + 美利居酒店(上海城市中心人民广场店)」
    # 后续可以改为从命令行或配置读取。
    city = "上海"
    hotel_name = "美利居酒店(上海城市中心人民广场店)"

    # 1. 首页 → 酒店搜索页
    entered = go_to_hotel_search(device_factory, device_id)
    if not entered:
        print("未能从首页进入酒店搜索页，主流程停止。")
        return
    time.sleep(1.0)

    # 2. 填写搜索条件并查询
    search_ok = fill_search_conditions(device_factory, device_id, city=city, hotel_keyword=hotel_name)
    if not search_ok:
        print("搜索步骤未完成，主流程停止。")
        return
    time.sleep(2.0)

    # 3. 在查询结果中点击目标酒店
    opened = open_hotel_from_result(device_factory, device_id, hotel_name=hotel_name)
    if not opened:
        print("未能从查询结果页进入酒店详情页，主流程停止。")
        return
    time.sleep(3.0)  # 等详情页加载

    # 4. 在房型页展开折叠区 & 套餐（轮数足够多，直到连续两轮无可点或达上限）
    expand_all_sections_and_packages(device_factory, device_id, max_rounds=40)

    if expand_only:
        print("调试模式：只执行展开，不调用 1.py 抓取。")
        return

    # 5. 调用 1.py 抓取所有房型套餐（完整流程模式）
    run_room_scrape_via_one_py()


if __name__ == "__main__":
    import sys
    expand_only_flag = ("--expand-only" in sys.argv) or ("--expand" in sys.argv)
    main(expand_only=expand_only_flag)

