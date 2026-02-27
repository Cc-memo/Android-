"""快速测试：uiautomator2 能否抓到房型列表内容。"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import uiautomator2 as u2

serial = "euvsoz9xs4mv99ai"
print(f"连接设备: {serial}")
d = u2.connect(serial)
print(f"设备信息: {d.info}")

print("抓取 UI 树...")
xml = d.dump_hierarchy()
print(f"XML 长度: {len(xml)}")

# 保存到文件
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "1_u2_dump.xml")
with open(out, "w", encoding="utf-8", errors="replace") as f:
    f.write(xml)
print(f"已保存到: {out}")

# 检查是否有房型内容
import re
texts = [x for x in re.findall(r'text="([^"]*)"', xml) if x.strip()]
room_kw = ["大床房", "双床房", "单人间", "大床", "双床", "家庭房", "不可取消", "在线付", "含早", "无早餐", "rmlist", "rmCard"]
found = [t for t in texts if any(k in t for k in room_kw)]
print(f"\n非空 text 总数: {len(texts)}")
print(f"含房型关键词: {len(found)} 个")
for t in found[:30]:
    print(f"  -> {t}")

if "nearbyRec" in xml:
    print("\n[警告] 仍包含 nearbyRec（推荐酒店流）")
if "rmlist" in xml or "rmCard" in xml:
    print("\n[OK] 包含 rmlist/rmCard（房型列表）")

# 顺便用 2.py 解析试试
print("\n--- 用 2.py 解析 ---")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from importlib import import_module
import importlib.util
spec = importlib.util.spec_from_file_location("_two", os.path.join(os.path.dirname(os.path.abspath(__file__)), "2.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
elements = mod.load_elements_from_xml(out)
rooms = mod.parse_rooms_from_elements(elements)
print(f"元素数: {len(elements)}, 房型套餐数: {len(rooms)}")
for r in rooms[:10]:
    print(f"  {r.get('房型名称', '?')} | {r.get('价格', '?')} | {r.get('备注', '?')}")
