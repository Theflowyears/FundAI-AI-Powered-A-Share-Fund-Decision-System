# -*- coding: utf-8 -*-
"""从现有 `config.json` 生成"随包起始基金池" `fundai/starter.py`。

为什么需要（2026-09-11 审计发现的致命上手缺陷）
------------------------------------------------
`settings.DEFAULT_CFG` 里 `pool` 与 `screening.universe` 都是空列表，而
`settings.validate()` 要求"至少一只权益 + 一只债券"。结果是：**全新环境
（没有 config.json）里 `python app.py serve` / `run-daily` 直接抛
ValueError: pool 为空** —— 便携包/新装用户第一步就走不下去。

修法：把当前这套**通用**基金池（宽基指数 + 行业指数 + 债基，不含任何密钥）
固化进包内 `fundai/starter.py`，`DEFAULT_CFG` 引用它；真实 `config.json`
仍然优先（存在就用用户的）。

用法：python tools/gen_starter.py     # 重新生成 fundai/starter.py
"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def py_literal(obj, indent="    "):
    """把 list[dict] 渲染成可读的 Python 字面量（一行一只基金）。"""
    lines = ["["]
    for it in obj:
        parts = ", ".join('"{}": {}'.format(k, json.dumps(v, ensure_ascii=False))
                          for k, v in it.items())
        lines.append(indent + "{" + parts + "},")
    lines.append("]")
    return "\n".join(lines)


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    pool = cfg.get("pool") or []
    univ = (cfg.get("screening") or {}).get("universe") or []
    if len(pool) < 2 or len(univ) < 10:
        raise SystemExit("config.json 里的 pool/universe 太少，不像是可用起始池")
    body = '''# -*- coding: utf-8 -*-
"""随包起始基金池（通用宽基/行业指数 + 债基，**不含任何密钥**）。

用途：全新环境（没有 `config.json`）第一次运行时，`settings.DEFAULT_CFG`
直接引用这里，避免"pool 为空 → 校验失败 → 服务根本起不来"。
用户自己的 `config.json` 永远优先；这里只是一份能立刻跑通的起点。

本文件由 `tools/gen_starter.py` 从当时的 `config.json` 生成，
可以随时重新生成，也可以手工编辑（都是普通的基金元数据）。
"""
POOL = {pool}

# 动态筛选候选池（refresh_pool 从这里按 20 日动量重建 ≥15 只备选池）
UNIVERSE = {univ}
'''.format(pool=py_literal(pool), univ=py_literal(univ))
    out = ROOT / "fundai" / "starter.py"
    out.write_text(body, encoding="utf-8")
    print("已生成 %s：pool %d 只 / universe %d 只（%.1f KB）"
          % (out.name, len(pool), len(univ), out.stat().st_size / 1024))


if __name__ == "__main__":
    main()
