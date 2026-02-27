"""诊断脚本：提取 1_debug_dump.xml 里所有非空 text，判断页面内容。"""
import re, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1_debug_dump.xml")
with open(path, "r", encoding="utf-8", errors="replace") as f:
    s = f.read()

texts = [x for x in re.findall(r'text="([^"]*)"', s) if x.strip()]
descs = [x for x in re.findall(r'content-desc="([^"]*)"', s) if x.strip()]

print(f"=== 1_debug_dump.xml: {len(texts)} 个非空 text, {len(descs)} 个非空 content-desc ===")
print("\n--- 所有 text ---")
for i, t in enumerate(texts, 1):
    print(f"  T{i}: {t}")
print("\n--- 所有 content-desc ---")
for i, d in enumerate(descs, 1):
    print(f"  D{i}: {d}")

# 房型关键词检测
room_kw = ["房", "单人间", "大床", "双床", "三人间", "家庭房"]
found = [t for t in texts if any(k in t for k in room_kw)]
print(f"\n--- 含房型关键词的 text ({len(found)} 个) ---")
for t in found:
    print(f"  -> {t}")

nearby = [d for d in descs if "nearbyRec" in d]
print(f"\n--- nearbyRec 相关 desc ({len(nearby)} 个) ---")
for d in nearby:
    print(f"  -> {d}")
