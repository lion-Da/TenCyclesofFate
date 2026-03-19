#!/usr/bin/env python3
"""
邀请码生成脚本
==============

用法:
    python -m backend.scripts.generate_invite_code [--count N] [--uses M] [--note "备注"]

示例:
    # 生成1个邀请码，默认10次使用上限
    python -m backend.scripts.generate_invite_code

    # 生成5个邀请码，每个可用20次
    python -m backend.scripts.generate_invite_code --count 5 --uses 20

    # 生成3个邀请码并附加备注
    python -m backend.scripts.generate_invite_code --count 3 --uses 50 --note "内测第一批"
"""

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from backend.app.invite_code import generate_code


def main():
    parser = argparse.ArgumentParser(description="浮生十梦 - 邀请码生成工具")
    parser.add_argument(
        "--count", "-c", type=int, default=1, help="生成数量 (默认: 1)"
    )
    parser.add_argument(
        "--uses", "-u", type=int, default=10, help="每个码的最大使用次数 (默认: 10)"
    )
    parser.add_argument(
        "--note", "-n", type=str, default="", help="备注信息"
    )
    args = parser.parse_args()

    if args.count < 1:
        print("错误: --count 必须 >= 1")
        sys.exit(1)
    if args.uses < 1:
        print("错误: --uses 必须 >= 1")
        sys.exit(1)

    print(f"\n{'='*40}")
    print(f"  浮生十梦 · 邀请码生成")
    print(f"{'='*40}")
    print(f"  数量: {args.count}")
    print(f"  每码可用次数: {args.uses}")
    if args.note:
        print(f"  备注: {args.note}")
    print(f"{'='*40}\n")

    codes = []
    for i in range(args.count):
        result = generate_code(max_uses=args.uses, note=args.note)
        codes.append(result["code"])
        print(f"  [{i+1}] {result['code']}  (最大 {args.uses} 次)")

    print(f"\n{'='*40}")
    print(f"  生成完毕！共 {len(codes)} 个邀请码")
    print(f"  数据文件: game_data/invite_codes.json")
    print(f"{'='*40}\n")

    # 方便复制的纯码列表
    if len(codes) > 1:
        print("纯码列表（方便复制）:")
        for c in codes:
            print(c)
        print()


if __name__ == "__main__":
    main()
