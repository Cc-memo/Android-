"""诊断：找出 u2 dump 中价格和仅剩节点的位置。"""
import re, sys, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

path = r"f:\Work\docx\mobile\room_rules_isolated\1_u2_dump.xml"
with open(path, "r", encoding="utf-8", errors="replace") as f:
    s = f.read()

# 找包含价格/剩余的 text
keywords = ["\u00a5", "299", "319", "348", "369", "399", "仅剩", "已订完", "售罄"]
for m in re.finditer(r'text="([^"]*)"', s):
    t = m.group(1).strip()
    if not t:
        continue
    if not any(k in t for k in keywords):
        continue
    # 往前找最近的 bounds 和 class
    start = max(0, m.start() - 500)
    chunk = s[start:m.end()+200]
    bounds_m = re.search(r'bounds="(\[[^\]]+\]\[[^\]]+\])"', chunk)
    cls_m = re.search(r'class="([^"]*)"', chunk)
    b = bounds_m.group(1) if bounds_m else "?"
    c = cls_m.group(1) if cls_m else "?"
    print(f"text={t!r:40s}  bounds={b:30s}  class={c}")

print("\n--- 套餐框(ViewGroup with rmCard) ---")
for m in re.finditer(r'content-desc="([^"]*rmCard[^"]*)"', s):
    desc = m.group(1)
    start = max(0, m.start() - 300)
    chunk = s[start:m.end()+200]
    bounds_m = re.search(r'bounds="(\[[^\]]+\]\[[^\]]+\])"', chunk)
    b = bounds_m.group(1) if bounds_m else "?"
    print(f"  desc={desc}  bounds={b}")
