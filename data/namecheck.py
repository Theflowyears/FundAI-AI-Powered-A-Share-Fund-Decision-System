# -*- coding: utf-8 -*-
"""未定义名检查器（stdlib symtable）：找出引用了但既非局部、也非模块级、也非内置的名字。

专治本轮这类 bug（删代码时留下 `departure` 这种悬空引用）：
  · 模块级：函数/类/import/赋值 都算已定义（含 `__all__` 前向引用）；
  · 函数内：局部变量、参数、嵌套作用域自由变量、模块全局、内置名 都算可用；
  · 只报“全局名但模块里查不到、也不是内置”的，避开绝大多数误报。
用法：python data/namecheck.py [目录或文件 ...]
"""
import builtins
import io
import os
import symtable
import sys


def _use_utf8_stdout():
    """把 stdout 切成 UTF-8（已是 UTF-8 就不重包，见 data/parallel.py 的说明）。

    这里刻意**不 import parallel**：本文件会被测试用 importlib 按路径直接加载
    （tests/test_project_integrity.py），那时 `data/` 不在 sys.path 上，
    多一个跨脚本导入就会 ModuleNotFoundError —— 工具脚本保持自包含。
    """
    try:
        enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
        if enc == "utf8":
            return
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    except Exception:
        pass


_use_utf8_stdout()

BUILTINS = set(dir(builtins))
SKIP_NAMES = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
              "__loader__", "__builtins__", "self", "cls"}


def module_globals(src):
    """模块级已定义的名字（含函数内 import 的近似：直接扫 AST 太糙，这里用符号表）。"""
    st = symtable.symtable(src, "<mod>", "exec")
    names = set()
    for sym in st.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            names.add(sym.get_name())
        elif sym.is_parameter():
            names.add(sym.get_name())
    return names, st


def walk(table, globals_, path, out):
    for sym in table.get_symbols():
        name = sym.get_name()
        if (sym.is_global() and not sym.is_assigned() and not sym.is_imported()
                and name not in globals_ and name not in BUILTINS
                and name not in SKIP_NAMES):
            out.append(("{}::{}".format(path, table.get_name()), name))
    for child in table.get_children():
        walk(child, globals_, path, out)


def check(path):
    src = open(path, encoding="utf-8").read()
    try:
        globals_, st = module_globals(src)
    except SyntaxError as e:
        return [("{}::<syntax>".format(path), str(e))]
    out = []
    walk(st, globals_, path, out)
    return out


def main(argv):
    targets = argv[1:] or ["fundai", "app.py", "tests", "data"]
    files = []
    for t in targets:
        if os.path.isdir(t):
            for root, _dirs, names in os.walk(t):
                if any(x in root for x in ("__pycache__", ".git")):
                    continue
                files += [os.path.join(root, n) for n in names if n.endswith(".py")]
        elif t.endswith(".py"):
            files.append(t)
    total = 0
    for f in sorted(set(files)):
        for where, name in check(f):
            print("未定义名: {:<52} → {}".format(where, name))
            total += 1
    print("检查 {} 个文件，发现 {} 处可疑未定义名".format(len(set(files)), total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
