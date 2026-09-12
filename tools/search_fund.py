# -*- coding: utf-8 -*-
"""按名称搜索场外基金，帮你扩充基金池（仅返回候选，不自动修改配置）。

用法: python tools/search_fund.py 沪深300
"""
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fundai import util  # noqa: E402

SEARCH_URL = ("https://searchapi.eastmoney.com/api/suggest/get?input={key}"
              "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=12")


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/search_fund.py <关键词>")
        return 1
    key = sys.argv[1]
    txt = util.http_get_text(
        SEARCH_URL.format(key=urllib.parse.quote(key)))
    m = re.search(r"=\s*(.*)\s*;?\s*$", txt, re.S)
    obj = json.loads(m.group(1)) if m else {}
    items = (obj.get("QuotationCodeTable") or {}).get("Data") or []
    print("搜索「{}」结果（仅展示前12条，字段: 代码 名称 类型）:".format(key))
    for it in items:
        print("{:<8}{:<32}{}".format(it.get("Code"), it.get("Name"),
                                     it.get("SecurityTypeName") or ""))
    print("\n确认代码可用后：python tools/check_fund.py <代码>")
    print("然后把它加入 config.json 的 pool（kind=equity/bond，C 类费率 0 申购 + 7天免赎回更合适）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
