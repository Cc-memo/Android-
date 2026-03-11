# -*- coding: utf-8 -*-
"""
meituan_navigate.py：从美团主页 → 酒店旅行页 → 输入地址/酒店名 → 点击第一个联想条 → 若在结果页则点击第一个酒店进入详情。

依赖：uiautomator2，设备已连接且已打开美团 App 至首页。
元素依据：xml/10.xml（首页）、xml/11.xml（酒店旅行）、xml/222.xml（联想条）、xml/12.xml（搜索结果）。

用法：
  python meituan_navigate.py
  python meituan_navigate.py --address "上海" --hotel "美利居酒店上海人民广场店"
  python meituan_navigate.py --address "南阳" --hotel "维也纳酒店"
"""

from __future__ import annotations

import argparse
import time

try:
    import uiautomator2 as u2
except ImportError:
    u2 = None


# 默认搜索关键词（与三张图一致）
DEFAULT_ADDRESS = "上海"
DEFAULT_HOTEL = "美利居酒店上海人民广场店"

# 每步操作后等待（秒）
WAIT_AFTER_CLICK = 1.2
WAIT_PAGE = 2.5


def step1_click_hotel_entry(d):
    """第一屏（xml/10.xml）：点击「酒店民宿」入口。"""
    # content-desc="酒店民宿" 的 View，bounds="[436,316][645,538]"
    el = d(description="酒店民宿")
    if el.exists:
        el.click()
        print("  已点击「酒店民宿」")
        return True
    # 备用：按 10.xml 中心坐标点击
    d.click(540, 427)
    print("  已按坐标点击「酒店民宿」区域")
    return True


def step2_search_hotel(d, address: str, hotel_name: str):
    """第二屏（xml/11.xml）：点击搜索框 → 输入地址+酒店名 → 等联想 → 点击第一个联想条（参考 xml/222.xml）。"""
    time.sleep(WAIT_PAGE)

    # 点击「位置/品牌/酒店」搜索框区域（弹出输入/键盘）
    search_hint = d(text="位置/品牌/酒店")
    if search_hint.exists:
        search_hint.click()
        time.sleep(WAIT_AFTER_CLICK)
    else:
        q_hint = d(textContains="位置")
        if q_hint.exists:
            q_hint.click()
            time.sleep(WAIT_AFTER_CLICK)
        else:
            d.click(332, 836)
            time.sleep(WAIT_AFTER_CLICK)

    # 输入：先清空再输入「地址 酒店名」
    time.sleep(0.5)
    edit = d(className="android.widget.EditText")
    if edit.exists:
        edit.clear_text()
        time.sleep(0.2)
        edit.set_text(f"{address} {hotel_name}")
    else:
        d.send_keys(f"{address} {hotel_name}", clear=True)
    time.sleep(0.5)

    # 等联想列表出现后，点击第一个联想条（不点「找酒店」）
    time.sleep(1.5)
    # 优先用联想条文案定位（222.xml 中第一条为「美利居酒店（上海人民广场店）」；带括号的文案不会点到输入框）
    for selector in [
        lambda: d(text="美利居酒店（上海人民广场店）"),
        lambda: d(textContains="（上海人民广场店）"),
    ]:
        el = selector()
        if el.exists:
            try:
                el.click()
                print("  已点击第一个联想条（按文案）")
                return True
            except Exception:
                pass
    # 备用：按 222.xml 第一条联想行中心点击 [0,205][1080,384] -> (540, 294)
    d.click(540, 294)
    print("  已点击第一个联想条（按坐标）")
    return True


def step3_click_first_result(d, hotel_keyword: str = ""):
    """第三屏（xml/12.xml）：点击搜索结果里「第一张酒店卡片」进入酒店详情（有预订/房型的那一页），避免点到下方筛选标签或推荐区。"""
    time.sleep(WAIT_PAGE + 1)

    # 优先用「结果卡片上的标题」精确匹配，避免点到「以上是符合…」下面的小标签（美利居酒店上海人民广场店）
    # 12.xml 中第一张卡片的标题是 "美利居酒店(上海人民广场店)"（带括号）
    exact_title = d(text="美利居酒店(上海人民广场店)")
    if exact_title.exists:
        try:
            exact_title.click()
            print("  已点击第一张酒店卡片，进入酒店详情")
            return True
        except Exception:
            pass
    # 备用：按 12.xml 第一张卡片中心点击 [0,580][1080,1045] -> (540,812)，保证进的是顶部大卡不是推荐区
    d.click(540, 812)
    print("  已按坐标点击第一张酒店卡片，进入酒店详情")
    return True


def main():
    parser = argparse.ArgumentParser(description="美团：主页→酒店搜索→进入第一个酒店详情")
    parser.add_argument("--address", default=DEFAULT_ADDRESS, help="搜索地址/城市")
    parser.add_argument("--hotel", default=DEFAULT_HOTEL, help="酒店名称关键词")
    parser.add_argument("--device", default="", help="设备 serial，空则默认首台")
    args = parser.parse_args()

    if u2 is None:
        print("请先安装 uiautomator2: pip install uiautomator2")
        return 1

    try:
        d = u2.connect(args.device) if args.device else u2.connect()
    except Exception as e:
        print("连接设备失败:", e)
        return 1

    print("请确保：美团 App 已打开且当前在首页（推荐 Tab）。")
    input("按回车开始执行导航…")

    try:
        step1_click_hotel_entry(d)
        time.sleep(WAIT_PAGE)

        step2_search_hotel(d, args.address, args.hotel)
        time.sleep(WAIT_PAGE)

        step3_click_first_result(d, hotel_keyword=args.hotel)
        time.sleep(WAIT_PAGE)

        print("导航结束，当前应已进入酒店详情页。可在此后调用 meituan_extract --device 采集房型。")
    except Exception as e:
        print("执行出错:", e)
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
