# -*- coding: utf-8 -*-
"""一键删除当前目录下所有 .xml 文件。"""
from pathlib import Path

def main():
    folder = Path(__file__).resolve().parent
    xml_files = list(folder.glob("*.xml"))
    if not xml_files:
        print("当前目录下没有 .xml 文件")
        return
    for f in xml_files:
        try:
            f.unlink()
            print(f"已删除: {f.name}")
        except OSError as e:
            print(f"删除失败 {f.name}: {e}")
    print(f"共删除 {len(xml_files)} 个文件")

if __name__ == "__main__":
    main()
