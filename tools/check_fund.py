# -*- coding: utf-8 -*-
"""校验基金代码：拉取名称与最新净值，确认代码有效且属于场外基金。

用法: python tools/check_fund.py 005918 270049 005919
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundai.datasource import Market  # noqa: E402
from fundai.util import DataError  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/check_fund.py <基金代码> [更多代码...]")
        return 1
    m = Market()
    for code in sys.argv[1:]:
        try:
            name = m.fund_name(code)
            d, nav = m.fund_latest(code)
            print("{}  {}  最新净值 {:.4f}  ({})".format(code, name, nav, d))
        except DataError as e:
            print("{}  校验失败: {}".format(code, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
