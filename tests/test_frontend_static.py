# -*- coding: utf-8 -*-
"""前端静态资源的**语法级**回归测试：锁死"一份坏 JS 让整个界面打不开"的故障模式。

为什么必须有这一条（2026-09-15 真实事故）：
    用 shell heredoc 打补丁时，`$("btnMktRefresh")` 里的 `$` 被转义成了 `\`，
    于是 app.js 变成 `const _btnMkt = \;` —— **整个文件语法错误**。
    浏览器解析脚本失败 → 一句 JS 都不执行 → 页面永远停在"大盘指数加载中…"，
    而后端接口全部正常（curl 打什么都 200）。这种故障后端测试**测不出来**，
    只有语法检查 / 真浏览器才能发现。

覆盖：
  1. `node --check` 逐个检查静态 JS（node 可用时；不可用则跳过并说明）；
  2. 无论有没有 node，都扫描 **shell 转义残留**（`= \;`、`\$(`、行尾孤立 `\`）——
     这正是本次事故的特征，且不依赖任何外部工具；
  3. 静态目录下的 .html/.css 也扫一遍同类残留（它们同样会被 shell 打补丁）；
  4. `index.html` 里 `<script src>` 引用的文件必须存在（否则页面静默白屏）。
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIRS = (ROOT / "fundai" / "static", ROOT / "web" / "static")
TEXT_EXT = (".js", ".html", ".css")

# shell 转义残留的特征：整行赋值以 `\` 收尾、`\$(`, 或 `= \;`
ARTIFACT_PATTERNS = [
    (re.compile(r"=\s*\\\s*;"), "赋值语句被写成 `= \\;`（$ 被 shell 吃掉）"),
    (re.compile(r"\\\$\("), "jQuery 风格 `$(...)` 被转义成 `\\$(`"),
    (re.compile(r"=\s*\\\s*$", re.M), "整行赋值以孤立反斜杠结尾"),
]


def _node():
    """找可用的 node（语法检查用；找不到就跳过那条检查）。"""
    for name in ("node", "node.exe"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (r"D:\JS\node.exe", r"C:\Program Files\nodejs\node.exe"):
        if Path(cand).exists():
            return cand
    return None


def _static_files(exts=TEXT_EXT):
    out = []
    for d in STATIC_DIRS:
        if d.is_dir():
            out += [p for p in sorted(d.rglob("*"))
                    if p.is_file() and p.suffix.lower() in exts
                    and "vendor" not in p.parts]
    return out


class FrontendSyntaxTest(unittest.TestCase):
    """静态资源必须语法正确、且没有 shell 转义残留。"""

    def test_no_shell_escape_artifacts(self):
        """任何 .js/.html/.css 里都不许出现 shell 转义残留。"""
        bad = []
        for p in _static_files():
            txt = p.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(txt.splitlines(), 1):
                for pat, why in ARTIFACT_PATTERNS:
                    if pat.search(line):
                        bad.append("{}:{} → {}｜{}".format(
                            p.relative_to(ROOT), i, why, line.strip()[:80]))
        self.assertEqual(bad, [], "静态资源里有 shell 转义残留：\n" + "\n".join(bad))

    def test_js_files_parse(self):
        """`node --check` 逐个校验静态 JS（node 不可用时跳过并说明原因）。"""
        node = _node()
        if not node:
            self.skipTest("未找到 node，跳过 JS 语法检查（其余检查仍在跑）")
        js = [p for p in _static_files((".js",))]
        self.assertTrue(js, "没找到任何静态 JS 文件")
        failed = []
        for p in js:
            r = subprocess.run([node, "--check", str(p)], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip().splitlines()
                failed.append("{} → {}".format(p.relative_to(ROOT),
                                               msg[0] if msg else "语法错误"))
        self.assertEqual(failed, [], "JS 语法检查失败（页面会整个打不开）：\n"
                         + "\n".join(failed))

    def test_index_html_scripts_exist(self):
        """index.html 引用的本地脚本/样式必须存在于磁盘。"""
        for d in STATIC_DIRS:
            html = d / "index.html"
            if not html.exists():
                continue
            txt = html.read_text(encoding="utf-8")
            refs = re.findall(r'(?:src|href)="([^"]+)"', txt)
            for ref in refs:
                if ref.startswith(("http://", "https://", "//", "data:")):
                    continue
                target = (d / ref.lstrip("/")).resolve()
                self.assertTrue(target.exists(),
                                "{} 引用了不存在的资源 {}（页面会白屏）".format(
                                    html.relative_to(ROOT), ref))

    def test_two_static_layouts_are_identical(self):
        """源码树 `web/static` 与包内 `fundai/static` 必须逐字节一致。"""
        a, b = STATIC_DIRS[0], STATIC_DIRS[1]
        if not (a.is_dir() and b.is_dir()):
            self.skipTest("静态资源目录不完整")
        diff = []
        for p in sorted(a.rglob("*")):
            if not p.is_file():
                continue
            q = b / p.relative_to(a)
            if not q.exists():
                diff.append("{}(包内缺失)".format(p.relative_to(a)))
            elif p.read_bytes() != q.read_bytes():
                diff.append("{}(内容不一致)".format(p.relative_to(a)))
        self.assertEqual(diff, [], "两处静态布局不一致：{}".format(diff))

    def test_key_frontend_hooks_present(self):
        """主界面新组件依赖的元素 id 必须同时存在于 html 与 js。"""
        html = (STATIC_DIRS[0] / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIRS[0] / "app.js").read_text(encoding="utf-8")
        for eid in ("idxStrip", "mktPanel", "mktGrid", "btnMktRefresh",
                     "chIntraday", "srcAlertPanel",
                     # 2026-09-16 事故：午间复盘卡的元素**只在 app.js 里被引用、
                     # HTML 里从未定义**，`if (!box) return` 静默返回 → 卡片永不显示，
                     # 用户反馈"午间诊断没有自动运行 + 界面看不到结果"。
                     "middayPanel", "middayCard", "middayBadge", "middayView",
                     "middayMeta", "btnMidday", "btnMiddaySettle"):
            self.assertIn('id="{}"'.format(eid), html,
                          "index.html 缺少元素 id {}".format(eid))
        for fn in ("loadMarket", "renderMktPanel", "loadIntraday",
                   "renderIntraday", "mktLoop", "renderMidday"):
            self.assertIn("function {}".format(fn), js,
                          "app.js 缺少函数 {}".format(fn))

    def test_every_referenced_element_id_exists_in_html(self):
        """**反向检查**：app.js 里 `$("someId")` 引用的 id，HTML 里必须真的存在。

        为什么需要这条（2026-09-16 事故复盘）：前一条测试只检查"html 里有这个 id、
        js 里有这个函数"，两头分开看都通过；但真正致命的组合是
        **js 引用了 html 里根本没有的元素** —— 此时 `$("x")` 返回 null，
        代码里的 `if (!el) return` 会让整块功能**静默失效**（不报错、不显示、不写日志）。
        午间复盘卡就是这样消失了一轮：元素从未写进 HTML，而所有测试都是绿的。
        这里做双向一致性检查，避免同类问题再出现。
        """
        html = (STATIC_DIRS[0] / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIRS[0] / "app.js").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        # 只认"取单个元素"的写法 `$("id")`；模板字符串里的动态 id 不在此列
        used = set(re.findall(r'\$\("([A-Za-z][\w\-]*)"\)', js))
        missing = sorted(i for i in used if i not in html_ids)
        self.assertEqual(
            missing, [],
            "app.js 引用了 index.html 里不存在的元素 id：{}\n"
            "（这会让相关功能静默失效：不报错、不显示）".format(missing))

    def test_no_undefined_globals_in_app_js(self):
        """app.js 里引用的**大写全局变量**必须真的声明过（防 `STATE` vs `ST` 这类笔误）。

        2026-09-16 事故：`renderBudget()` 写了 `STATE`，而全局变量实际叫 `ST`，
        于是抛 ReferenceError 把整个 `renderNews()` 打断 → 用户看到
        "消息数据加载失败：STATE is not defined"。语法检查（node --check）**查不出**这类
        错误（它在语法上完全合法），只有真正执行到那一行才会炸。
        这里用"声明集合 vs 引用集合"做静态比对，能在打开页面前就拦住。

        注意：必须先剔除注释与字符串字面量，否则 `'GET'`、`'WEEKLY'`、`// CDN`
        这类**文本**会被误判成变量引用（第一版就是这么误报的）。
        """
        js = (STATIC_DIRS[0] / "app.js").read_text(encoding="utf-8")
        # 声明集合：取出每条 let/var/const **语句**，再在这条语句里找所有大写标识符。
        # 不能只抓关键字后紧跟的那一个名字 —— `const A = 1, B = 2;` 会漏掉 B
        # （实测 `const MKT_TICK = 60000, MKT_TICK_OFF = 300000;` 就被误报过）。
        declared = set()
        for stmt in re.findall(r"\b(?:let|var|const)\b([^;\n]*)", js):
            declared |= set(re.findall(r"\b([A-Z][A-Z0-9_]*)\b", stmt))
        # 去注释 → 去字符串/模板字面量；剩下的才是真正的代码
        code = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
        code = re.sub(r"//[^\n]*", " ", code)
        code = re.sub(r"'(?:[^'\\\n]|\\.)*'", " ", code)
        code = re.sub(r'"(?:[^"\\\n]|\\.)*"', " ", code)
        code = re.sub(r"`(?:[^`\\]|\\.)*`", " ", code, flags=re.S)
        builtin = {"JSON", "Math", "Date", "Number", "String", "Array", "Object",
                   "Promise", "Set", "Map", "RegExp", "Error", "NaN", "Infinity",
                   "Symbol", "Boolean", "Function", "URL", "URLSearchParams",
                   "Intl", "ECharts"}
        used = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", code))
        unknown = sorted(used - declared - builtin)
        self.assertEqual(
            unknown, [],
            "app.js 里引用了未声明的大写全局变量（多半是打错字）：{}\n"
            "已声明：{}".format(unknown, sorted(declared)))


if __name__ == "__main__":
    unittest.main()
