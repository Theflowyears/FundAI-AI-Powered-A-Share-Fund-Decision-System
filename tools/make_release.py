# -*- coding: utf-8 -*-
"""构建发行包：同步静态资源 → 生成 config.example.json → 打 wheel/sdist → 打便携 ZIP。

产物（`dist/`）：
  * `fundai-<version>-py3-none-any.whl` / `.tar.gz` —— 可 `pip install` 的包
    （含 `fundai` 命令；静态资源打进 `fundai/static/`）
  * `fundai-portable-<version>.zip` —— **推荐给普通用户**的自包含便携包：
    解压即用（源码 + 前端 + 示例配置 + 启动脚本 + 计划任务注册脚本），
    不含任何数据库/缓存/密钥。

为什么做这个脚本而不是手敲命令：
  1. **静态资源两处布局**必须一致（源码树 `web/static` 与安装包 `fundai/static`），
     否则装完的包打开网页会 404 —— 这里统一复制并校验哈希；
  2. **不能把真实 `config.json` 打进包**（里面有智兔 Token 与 LLM api_key）——
     这里生成脱敏的 `config.example.json`；
  3. 便携包要**排除** data/ 下的大文件（十年电报库 960MB、行情缓存、账本），
     只留目录骨架，否则包会有 1GB 级体积。

用法：
    python tools/make_release.py            # 全量：wheel + sdist + 便携 ZIP
    python tools/make_release.py --zip-only # 只打便携 ZIP（快）
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 便携包里**不要**的东西（运行时产物 / 私密 / 体积大）
EXCLUDE_DIRS = {".git", "__pycache__", "dist", "build", ".venv", "venv",
                ".pytest_cache", ".idea", ".vscode"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm", ".zip",
                  ".whl", ".tar.gz"}
EXCLUDE_FILES = {"config.json", "cls_history.json", "search_results.json"}
# 便携包里 data/ 下**只保留**研究脚本与说明：脚本是可复现性的一部分
# （README_PORTABLE 让用户跑 data/regime_study.py 等），而缓存/数据库/大结果文件不进包。
DATA_INCLUDE_SUFFIX = {".py"}


def version():
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in txt.splitlines():
        if line.startswith("version"):
            return line.split("=")[1].strip().strip('"').strip("'")
    return "0.0.0"


def sync_static():
    """web/static → fundai/static（wheel 里必须**在包内**才能被 package-data 收走）。"""
    src = ROOT / "web" / "static"
    dst = ROOT / "fundai" / "static"
    if not src.exists():
        raise SystemExit("缺少 web/static 目录")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    same = _digest(src / "app.js") == _digest(dst / "app.js")
    print("静态资源同步：web/static → fundai/static（%d 个文件，校验 %s）"
          % (sum(1 for _ in dst.rglob("*") if _.is_file()),
             "一致" if same else "**不一致**"))
    return dst


def _digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_example_config():
    """脱敏示例配置（去掉 Token / api_key / 账户名），供新用户起步。"""
    src = ROOT / "config.json"
    cfg = json.loads(src.read_text(encoding="utf-8"))
    cfg.setdefault("data", {})["zhitu_token"] = ""
    cfg["data"]["note"] = cfg["data"].get("note", "")
    cfg.setdefault("llm", {})["api_key"] = ""
    cfg.setdefault("account", {})["name"] = "我的基金实验"
    for k in ("exec_mode",):
        cfg["account"].setdefault(k, "manual")
    out = ROOT / "config.example.json"
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("示例配置：config.example.json（Token / api_key 已清空，%d 字节）"
          % out.stat().st_size)
    return out


def build_wheel():
    """优先 `python -m build`；没有就用 pip wheel 兜底。"""
    for cmd in ([sys.executable, "-m", "build", "--wheel", "--sdist"],
                [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", "dist", "."]):
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, timeout=900)
        except Exception as e:
            print("  %s 失败：%s" % (" ".join(cmd[:3]), e))
            continue
        if p.returncode == 0:
            print("包构建成功：%s" % " ".join(cmd[:3]))
            for f in sorted(DIST.iterdir()):
                print("   · %s（%.1f KB）" % (f.name, f.stat().st_size / 1024))
            return True
        print("  %s 返回 %d：%s" % (" ".join(cmd[:3]), p.returncode,
                                   (p.stderr or b"").decode("utf-8", "replace")[-300:]))
    print("⚠️ 无法构建 wheel/sdist（缺 build 与 pip wheel 环境）；便携 ZIP 不受影响")
    return False


def portable_zip():
    ver = version()
    out = DIST / "fundai-portable-{}.zip".format(ver)
    DIST.mkdir(exist_ok=True)
    n = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(ROOT.rglob("*")):
            rel = p.relative_to(ROOT)
            parts = set(rel.parts)
            if parts & EXCLUDE_DIRS:
                continue
            if p.is_dir():
                continue
            if p.name in EXCLUDE_FILES or p.suffix in EXCLUDE_SUFFIX:
                continue
            if rel.parts[0] == "data" and len(rel.parts) > 1:
                # 只留研究脚本（*.py）与说明文件；缓存/数据库/大 JSON 结果一律不进包
                if p.suffix not in DATA_INCLUDE_SUFFIX and \
                        p.name not in ("README.md", ".gitkeep"):
                    continue
            if p.name == "config.json":
                continue
            arc = "fundai-{}/{}".format(ver, rel.as_posix())
            z.write(p, arc)
            n += 1
            total += p.stat().st_size
        # 说明文件与空数据目录
        z.writestr("fundai-{}/data/.gitkeep".format(ver), "")
    print("便携包：%s（%d 个文件，原始 %.1f MB → 压缩 %.1f MB）"
          % (out.name, n, total / 1048576, out.stat().st_size / 1048576))
    return out


def main():
    zip_only = "--zip-only" in sys.argv
    print("=== fundai 发行包构建（版本 %s）===" % version())
    sync_static()
    make_example_config()
    if not zip_only:
        build_wheel()
    portable_zip()
    print("\n完成。提醒：便携包**不含** config.json / 数据库 / 缓存；"
          "首次运行会自动从默认值生成 config.json（见 README「快速开始」）。")


if __name__ == "__main__":
    main()
