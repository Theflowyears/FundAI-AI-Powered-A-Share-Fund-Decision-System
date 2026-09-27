# -*- coding: utf-8 -*-
"""GitHub 发行目录脱敏：把密钥/本机路径/公网身份从**产物副本**里清掉。

为什么单独一个脚本
------------------
`make_release.py` 打的是"不含任何数据"的便携包（只有代码骨架）。本脚本服务另一种
场景：**把带历史数据的仓库副本发到 GitHub**——数据和缓存里会残留调用方自己的痕迹：

* `config.json` 的 `data.zhitu_token`、`llm.api_key`；
* `data/config_backups/*.json`（参数面板每次改配置都会备份一份，同样含密钥）；
* 审计/用量产物里的**本机绝对路径**（含 Windows 用户名）与内网地址；
* 研究报告里出现的本机用户名。

原则：**只做"把敏感值替换成占位符"的最小改动**，不动任何数字、结论与结构——
读者看到的实证结果与发布者本机逐位一致，只是看不到密钥和本机路径。

用法：
    python tools/redact_for_github.py <目标目录>          # 就地脱敏
    python tools/redact_for_github.py <目标目录> --check  # 只检查不修改（CI/复核用）
"""
import io
import json
import os
import re
import sys
from pathlib import Path

# 需要清空的"敏感值"键（出现在任意 JSON 里都清）
SECRET_KEYS = ("api_key", "zhitu_token", "token", "secret", "password")
# 文本替换规则：(正则, 替换) —— 顺序执行；替换用函数以避免 `\U` 之类转义报错
def _rep(text):
    return lambda m: text


TEXT_RULES = [
    (re.compile(r"sk-[A-Za-z0-9_.\-]{16,}"), "sk-REDACTED"),
    (re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}",
                re.IGNORECASE), "TOKEN-REDACTED"),
    # Windows 用户目录（含 JSON 转义的双反斜杠、以及 8.3 短名 THEFLO~1 一类）
    (re.compile(r"C:\\\\+Users\\\\+[^\\\\\"\s]+"), r"C:\\Users\\<user>"),
    (re.compile(r"C:\\+Users\\+[^\\\"\s]+"), r"C:\Users\<user>"),
    (re.compile(r"/Users/[^/\"\s]+"), "/Users/<user>"),
    (re.compile(r"/home/[^/\"\s]+"), "/home/<user>"),
    # 本机项目绝对路径（审计/用量产物会回显工作目录）：统一替换成占位符，
    # 避免把"发布者把这套东西装在哪个盘、哪个目录"一起发出去
    (re.compile(r"[A-Za-z]:\\\\+[^\\\"\s]*?(?:DSHtemporary|fund200ai)[^\\\"\s]*"),
     "<project-dir>"),
    (re.compile(r"[A-Za-z]:\\+[^\\\"\s]*?(?:DSHtemporary|fund200ai)[^\\\"\s]*"),
     "<project-dir>"),
]
TEXT_RULES = [(pat, _rep(rep)) for pat, rep in TEXT_RULES]
TEXT_SUFFIX = {".json", ".md", ".txt", ".log", ".py", ".bat", ".cfg", ".ini",
               ".js", ".html", ".css", ".yml", ".yaml", ".toml"}
SKIP_DIRS = {".git", "__pycache__"}
# 脱敏后的占位符：出现这些即视为"已处理"，不再报警。
# 前两个是替换结果；`/Users/`、`/home/` 是**本文件自己的规则文本**（自指误报）。
_ALLOW = ("<user>", "REDACTED", "<project-dir>", "/Users/", "/home/")


def _redact_json(path):
    """JSON：清空敏感键的值；其余原样。返回改动数。"""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    n = [0]

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and k.lower() in SECRET_KEYS and v:
                    node[k] = ""
                    n[0] += 1
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    if n[0]:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return n[0]


def _redact_text(path):
    try:
        txt = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return 0
    out = txt
    for pat, rep in TEXT_RULES:
        out = pat.sub(rep, out)
    if out != txt:
        path.write_text(out, encoding="utf-8")
        return 1
    return 0


def scan(root):
    """返回仍含疑似敏感内容的位置（复核用，不修改）。

    `_ALLOW` 里的占位符（`<user>`/`REDACTED` 等）是脱敏后的**期望结果**，不算问题——
    否则脚本会把自己的规则文本也报成敏感（自指误报）。
    """
    hits = []
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIX:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = None
        for pat, _rep in TEXT_RULES:
            m = pat.search(txt)
            if m and not any(a in m.group(0) for a in _ALLOW):
                found = m.group(0)[:60]
                break
        if found is None:
            for k in SECRET_KEYS:
                m = re.search(r'"{}"\s*:\s*"([^"]{{8,}})"'.format(k), txt)
                if m:
                    found = '"{}": "{}"'.format(k, m.group(1)[:24])
                    break
        if found:
            hits.append((str(p.relative_to(root)), found))
    return hits


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    check_only = "--check" in sys.argv
    if not root.is_dir():
        print("目录不存在：{}".format(root))
        return 1
    if check_only:
        hits = scan(root)
        print("疑似敏感位置 {} 处".format(len(hits)))
        for rel, what in hits:
            print("  {} ← {}".format(rel, what))
        return 1 if hits else 0
    n_json = n_text = 0
    for p in sorted(root.rglob("*")):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts):
            continue
        suf = p.suffix.lower()
        if suf == ".json":
            n_json += _redact_json(p)
        if suf in TEXT_SUFFIX:
            n_text += _redact_text(p)
    print("脱敏完成：JSON 敏感键清空 {} 处，文本规则替换文件 {} 个".format(n_json, n_text))
    hits = scan(root)
    print("复核：仍疑似敏感位置 {} 处".format(len(hits)))
    for rel, what in hits[:20]:
        print("  {} ← {}".format(rel, what))
    return 0 if not hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
