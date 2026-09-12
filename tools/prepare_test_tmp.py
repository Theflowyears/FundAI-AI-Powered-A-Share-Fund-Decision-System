# -*- coding: utf-8 -*-
"""预建并行测试用的临时 SQLite 文件（本机沙箱下 Python 不能新建文件，用 PowerShell 建）。

用法（PowerShell）：
    pwsh -Command "New-Item -ItemType Directory -Force _test_tmp\cls | Out-Null;
    'cls_score.db','cls_fetch.db' | %{ New-Item -ItemType File -Force "_test_tmp\cls\$_" | Out-Null }"

之后 `python -m unittest discover -s tests -p "test_parallel_cli.py"` 即可。
缺失这些文件时，相关用例会 **skip 并说明原因**，不会失败。
"""
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent / "_test_tmp" / "cls"
    print("需要在 PowerShell 里预建：")
    print('  New-Item -ItemType Directory -Force "{}" | Out-Null'.format(root))
    for name in ("cls_score.db", "cls_fetch.db"):
        print('  New-Item -ItemType File -Force "{}" | Out-Null'.format(root / name))


if __name__ == "__main__":
    main()
