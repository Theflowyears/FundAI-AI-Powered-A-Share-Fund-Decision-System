# -*- coding: utf-8 -*-
"""fundai 命令行入口。

用法示例
--------
python app.py init                 # 初始化正式账户（1000 元起步）
python app.py run-daily            # 每个交易日收盘后运行一次（研判+下单）
python app.py run-daily --force    # 重跑当日
python app.py demo                 # 用合成演示数据灌满演示库（离线体验）
python app.py serve                # 启动可视化界面 http://127.0.0.1:8787
python app.py serve --demo         # 打开演示库界面
python app.py backtest --months 6  # 回测最近半年
python app.py state                # 打印账户状态
"""
import argparse
import json
import os
import sys
from pathlib import Path

from fundai import settings, util
from fundai.datasource import Market
from fundai.engine import Engine
from fundai.ledger import Ledger
from fundai.util import DataError

PROJECT = util.PROJECT
DB_LIVE = util.data_file("fund200.db")
DB_DEMO = util.data_file("demo.db")


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def jprint(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_init(args):
    cfg = settings.load_config()
    db = Path(args.db)
    ledger = Ledger(str(db), initial_cash=float(cfg["account"]["initial_cash"]))
    eng = Engine(cfg, ledger)
    if args.reset:
        ledger.reset(float(args.cash))
    eng.ensure_account()
    meta = ledger.account_meta()
    jprint({"ok": True, "db": str(db),
            "message": "账户就绪：{} 元起步，目标 {} 元，期限 {} ~ {}".format(
                meta.get("initial_cash"), meta.get("target_value"),
                meta.get("start_date"), meta.get("end_date"))})


def cmd_run_daily(args):
    cfg = settings.load_config()
    ledger = Ledger(str(Path(args.db)))
    eng = Engine(cfg, ledger)
    out = eng.run_daily(force=args.force)
    print("== {} ==".format(out.get("date", "?")))
    print("状态   :", out.get("status"))
    if out.get("status") in ("ok", "noop"):
        print("观点   :", out.get("view"), "评分", out.get("score"), "来源", out.get("source"))
        for f in out.get("fills", []):
            print(" 成交  :", f.get("action"), f.get("code"), f.get("date"))
        for o in out.get("orders", []):
            print(" 指令  :", o["action"], o["code"], o["amount_yuan"], "元")
        for m in out.get("messages", []):
            print(" 提示  :", m)
    else:
        print("消息   :", out.get("message"))
    return 0 if out.get("status") in ("ok", "noop") else 1


def cmd_state(args):
    cfg = settings.load_config()
    ledger = Ledger(str(Path(args.db)))
    eng = Engine(cfg, ledger)
    try:
        st = eng.state_payload()
    except DataError as e:
        st = {"error": str(e)}
    jprint(st)


def cmd_demo(args):
    cfg = settings.load_config()
    ledger = Ledger(str(DB_DEMO))
    eng = Engine(cfg, ledger, market=Market(cfg), demo=True)
    res = eng.run_demo(reset=True)
    jprint(res)
    print("演示库已生成：", DB_DEMO)
    print("查看界面：python app.py serve --demo")


def cmd_backtest(args):
    cfg = settings.load_config()
    ledger = Ledger(":memory:", initial_cash=float(cfg["account"]["initial_cash"]))
    eng = Engine(cfg, ledger, market=Market(cfg))
    try:
        res = eng.backtest(months=args.months, source=args.source)
    except DataError as e:
        print("回测失败：", e)
        return 1
    jprint({k: v for k, v in res.items() if k != "series"})
    return 0


def cmd_refresh_pool(args):
    cfg = settings.load_config()
    ledger = Ledger(str(Path(args.db)))
    eng = Engine(cfg, ledger, market=Market(cfg))
    res = eng.refresh_pool(force=args.force)
    jprint(res)
    return 0 if res.get("ok") else 1


def cmd_news_pull(args):
    """在线多源拉取当日消息并入库（供“消息与进化”页打标前预热）。"""
    cfg = settings.load_config()
    from fundai import news as newsmod
    from fundai import screening
    date_s = str(args.date or util.today_str())[:10]
    amp = int(((cfg.get("strategy") or {}).get("news_amp") or 8))
    store = screening.ScreeningStore()
    obj = newsmod.load_news(date_s, amplitude=amp, force=args.force, cfg=cfg)
    out = {"ok": bool(obj.get("ok")), "date": date_s,
           "items": obj.get("items", 0), "net": obj.get("net"),
           "score": obj.get("score")}
    if obj.get("ok"):
        store.ingest_feed(date_s, obj.get("feed") or [])
        out["human"] = store.human_news_meta(date_s, amplitude=amp)
        out["message"] = "已入库 {} 条，打开网页「消息与进化」即可人工打标".format(
            len(obj.get("feed") or []))
    else:
        out["message"] = obj.get("message") or "抓取失败"
    screening.ensure_daily_news_queue(date_s, bool(obj.get("ok")),
                                      int(obj.get("items") or 0))
    m = screening.import_search_results(cfg=cfg)
    if m.get("added"):
        out["search_merge"] = m
    jprint(out)
    return 0 if obj.get("ok") else 1


def cmd_search_queue(args):
    """查看待 DSH 本地搜索补全的清单。"""
    from fundai import screening
    state = util.load_json(screening.SEARCH_QUEUE_FILE, {"items": []})
    open_items = [x for x in state.get("items", []) if x.get("status") == "open"]
    jprint({
        "queue_file": str(screening.SEARCH_QUEUE_FILE),
        "results_file": str(screening.SEARCH_RESULTS_FILE),
        "open": open_items,
        "all": state.get("items", []),
        "last_merge": util.load_json(screening.SEARCH_LAST_FILE),
        "hint": "让 DSH 助手按 open 清单搜索，把结果写入 search_results.json 后运行 python app.py search-import",
    })
    return 0


def cmd_search_import(args):
    """合并 DSH 本地搜索回填结果（data/search_results.json）进当天消息库。"""
    from fundai import screening
    m = screening.import_search_results()
    jprint(m)
    return 0


def is_python_stub(exe=None):
    """Windows 应用商店的 python 存根（点了只会弹商店，不真执行）。"""
    exe = (exe or sys.executable or "").lower()
    return "windowsapps" in exe


def cmd_doctor(args):
    """环境体检：解释器/数据源/图表组件/配置口径。"""
    import platform
    from pathlib import Path
    from fundai import screening
    out = {
        "interpreter": sys.executable,
        "python_stub": is_python_stub(),
        "python_version": platform.python_version(),
        "pythonw_local": str(Path(os.environ.get("LOCALAPPDATA", "")) /
                             "Python/bin/pythonw.exe"),
        "vendor_echarts": str(PROJECT / "web" / "static" / "vendor" /
                              "echarts.min.js"),
        "data_source": str(settings.load_config().get("data", {}).get("provider")),
    }
    out["pythonw_exists"] = Path(out["pythonw_local"]).exists()
    out["vendor_echarts_exists"] = Path(out["vendor_echarts"]).exists()
    cfg = settings.load_config()
    out["pool_count"] = len(cfg.get("pool", []))
    out["universe_count"] = len(((cfg.get("screening") or {}).get("universe")) or [])
    out["oos_file"] = str(util.data_file("backtest_oos.json"))
    out["hint"] = []
    if out["python_stub"]:
        out["hint"].append("当前解释器是 Windows 应用商店存根，请改用："
                           "%LOCALAPPDATA%\\Python\\bin\\python.exe app.py …"
                           "（或桌面 .bat 快捷方式已自动指向该路径）")
    if not out["vendor_echarts_exists"]:
        out["hint"].append("图表库未内置（离线时图表不可用）：联网后运行 "
                           "python app.py vendor-echarts 可本地化一次")
    jprint(out)
    return 0


def cmd_vendor_echarts(args):
    """把 ECharts 下载到本地 web/static/vendor/，离线也能画图。"""
    dst = PROJECT / "web" / "static" / "vendor" / "echarts.min.js"
    dst.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js",
        "https://registry.npmmirror.com/echarts/5.5.0/files/dist/echarts.min.js",
        "https://unpkg.com/echarts@5.5.0/dist/echarts.min.js",
    ]
    for u in urls:
        try:
            data = util.http_get(u, timeout=60, tries=1)
            if data and len(data) > 100000:
                dst.write_bytes(data)
                print("已内置 ECharts：{}（{} 字节）".format(dst, len(data)))
                return 0
        except Exception as e:
            print("下载失败 {}：{}".format(u[:60], e))
    print("所有 CDN 均不可达。请在可联网的机器上重试；在此之前页面会自动切换为文字摘要模式。")
    return 1


def cmd_oos(args):
    """时间顺序切分回测（full/train/test），输出样本内 vs 样本外(近似)口径。"""
    from fundai import screening  # noqa
    cfg = settings.load_config()
    ledger = Ledger(":memory:", initial_cash=float(cfg["account"]["initial_cash"]))
    eng = Engine(cfg, ledger, market=Market(cfg))
    try:
        res = eng.backtest_split(months=args.months, test_frac=args.test,
                                 source=args.source)
    except DataError as e:
        print("样本外回测失败：", e)
        return 1
    jprint({k: v for k, v in res.items() if k != "series"})
    util.save_json(util.data_file("backtest_oos.json"), res)
    return 0


def cmd_serve(args):
    cfg = settings.load_config()
    db = DB_DEMO if args.demo else DB_LIVE
    from fundai import web
    srv, app = web.make_server(cfg, db, demo=args.demo, port=args.port,
                               host=args.host)
    mode = "演示库(合成数据)" if args.demo else "正式账户(在线行情)"
    print("fundai 可视化界面已启动")
    print("数据   :", mode)
    print("账本   :", db)
    print("地址   : http://{}:{}".format(srv.server_address[0],
                                         srv.server_address[1]))
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main():
    _utf8()
    p = argparse.ArgumentParser(prog="app.py",
                                description="AI 基金 1000 元半年挑战 1500 实验（仅场外基金）")
    sub = p.add_subparsers(dest="cmd")

    i = sub.add_parser("init", help="初始化正式账户")
    i.add_argument("--db", default=str(DB_LIVE))
    i.add_argument("--reset", action="store_true", help="清空账本重新开始")
    i.add_argument("--cash", type=float, default=None,
                   help="重置时使用的初始资金（默认读取 config.json）")
    i.set_defaults(fn=cmd_init)

    r = sub.add_parser("run-daily", help="运行当日研判与指令")
    r.add_argument("--db", default=str(DB_LIVE))
    r.add_argument("--force", action="store_true", help="重跑当日（撤销当日挂单）")
    r.set_defaults(fn=cmd_run_daily)

    s = sub.add_parser("state", help="打印账户状态")
    s.add_argument("--db", default=str(DB_LIVE))
    s.set_defaults(fn=cmd_state)

    d = sub.add_parser("demo", help="生成离线演示库（合成数据）")
    d.set_defaults(fn=cmd_demo)

    b = sub.add_parser("backtest", help="回测策略")
    b.add_argument("--months", type=int, default=6)
    b.add_argument("--source", default="auto", choices=["auto", "live", "demo"])
    b.set_defaults(fn=cmd_backtest)

    rp = sub.add_parser("refresh-pool", help="从候选中重建备选池（保证>=8只，保留持仓与债基）")
    rp.add_argument("--db", default=str(DB_LIVE))
    rp.add_argument("--force", action="store_true",
                    help="即使 screening.enabled=false 也执行")
    rp.set_defaults(fn=cmd_refresh_pool)

    sv = sub.add_parser("serve", help="启动可视化界面")
    sv.add_argument("--demo", action="store_true", help="打开演示库")
    sv.add_argument("--port", type=int, default=None)
    sv.add_argument("--host", default=None)
    sv.set_defaults(fn=cmd_serve)

    np_ = sub.add_parser("news-pull", help="拉取今日消息（多源）并入“消息与进化”库")
    np_.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    np_.add_argument("--force", action="store_true", help="忽略当日缓存强制重抓")
    np_.set_defaults(fn=cmd_news_pull)

    sq = sub.add_parser("search-queue", help="查看待 DSH 本地搜索补全的清单")
    sq.set_defaults(fn=cmd_search_queue)

    si = sub.add_parser("search-import", help="合并 DSH 本地搜索回填结果")
    si.set_defaults(fn=cmd_search_import)

    doc = sub.add_parser("doctor", help="环境体检（解释器/图表库/数据口径）")
    doc.set_defaults(fn=cmd_doctor)

    ve = sub.add_parser("vendor-echarts", help="把 ECharts 下载到本地（离线可用）")
    ve.set_defaults(fn=cmd_vendor_echarts)

    oos = sub.add_parser("oos", help="时间切分回测：样本内/样本外(近似)口径")
    oos.add_argument("--months", type=int, default=12)
    oos.add_argument("--test", type=float, default=0.35, help="后段测试占比(0.2~0.6)")
    oos.add_argument("--source", default="auto", choices=["auto", "live", "demo"])
    oos.set_defaults(fn=cmd_oos)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 1
    if args.cmd != "doctor" and is_python_stub():
        print("错误：当前 python 是 Windows 应用商店存根，不会真正执行。")
        print("请改用完整解释器，例如：")
        print('  %LOCALAPPDATA%\\Python\\bin\\python.exe app.py %s …' % args.cmd)
        print("或直接运行： python app.py doctor")
        return 3
    fn = args.fn
    if args.cmd == "init" and args.cash is None:
        cfg = settings.load_config()
        args.cash = float(cfg["account"]["initial_cash"])
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
