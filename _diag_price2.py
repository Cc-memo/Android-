"""诊断：对比 Appium XML 和 u2 dump 中价格节点的位置关系。"""
import re, sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

base = r"f:\Work\docx\mobile\room_rules_isolated"

def analyze(path, label):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        s = f.read()
    print(f"\n{'='*60}")
    print(f"分析: {label}")
    print(f"{'='*60}")
    
    # 找所有包含价格符号或数字的text
    price_kw = ["\u00a5", "起", "预订"]
    for m in re.finditer(r'text="([^"]*)"', s):
        t = m.group(1).strip()
        if not t:
            continue
        if not any(k in t for k in price_kw):
            continue
        # 找 bounds
        start = max(0, m.start() - 500)
        chunk = s[start:m.end()+200]
        bounds_m = re.search(r'bounds="(\[[^\]]+\]\[[^\]]+\])"', chunk)
        cls_m = re.search(r'class="([^"]*)"', chunk)
        b = bounds_m.group(1) if bounds_m else "?"
        c = cls_m.group(1) if cls_m else "?"
        print(f"  text={t!r:40s} bounds={b:30s} class={c}")

    # 找 promotionBt / 预订按钮
    print(f"\n  --- promotionBt / booking buttons ---")
    for m in re.finditer(r'content-desc="([^"]*(?:promotion|booking|price)[^"]*)"', s, re.I):
        desc = m.group(1)
        start = max(0, m.start() - 300)
        chunk = s[start:m.end()+300]
        bounds_m = re.search(r'bounds="(\[[^\]]+\]\[[^\]]+\])"', chunk)
        b = bounds_m.group(1) if bounds_m else "?"
        # find text nodes inside
        texts_nearby = re.findall(r'text="([^"]+)"', chunk)
        print(f"  desc={desc}  bounds={b}  nearby_texts={texts_nearby[:5]}")

# Appium XML
appium_xml = os.path.join(base, "xml", "app-source-2026-02-15T14_34_50.547Z.xml")
if os.path.isfile(appium_xml):
    analyze(appium_xml, "Appium Inspector XML (有3套餐的那个)")

# u2 dump
u2_xml = os.path.join(base, "1_u2_dump.xml")
if os.path.isfile(u2_xml):
    analyze(u2_xml, "uiautomator2 dump")

# xml/2.xml
xml2 = os.path.join(base, "xml", "2.xml")
if os.path.isfile(xml2):
    analyze(xml2, "xml/2.xml (4套餐)")
