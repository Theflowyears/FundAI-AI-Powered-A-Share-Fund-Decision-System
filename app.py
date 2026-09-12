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
import re
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
    """初始化/重置账户：**投入金额可自定义**，目标 = 投入 × 倍数（默认 1.3）。

    优先级：`--cash` > 配置 `account.initial_cash`；
            `--target`（显式金额）> `--target-multiple` > 配置 `account.target_multiple`。
    改本金/倍数不需要动其他算法参数——目标、进度、LLM 提示词、回测判定都从这两个数推导。
    """
    cfg = settings.load_config()
    db = Path(args.db)
    cash = float(args.cash if args.cash is not None
                 else (cfg.get("account", {}) or {}).get("initial_cash", 1000.0))
    mult = args.target_multiple
    if mult is None:
        mult = float((cfg.get("account", {}) or {}).get("target_multiple", 1.3))
    target = float(args.target) if args.target is not None else round(cash * float(mult), 2)
    mult_eff = (target / cash) if cash else float(mult)
    ledger = Ledger(str(db), initial_cash=cash)
    eng = Engine(cfg, ledger)
    if args.reset:
        ledger.reset(cash)
    meta = eng.ensure_account()
    # **把计划写回配置**：本金/倍数/目标是"当前计划"，写回后参数面板、LLM 提示词、
    # 回测判定、界面进度都能看到同一套数字（走 configedit 的安全写：白名单+校验+备份+热加载）。
    cfg_written = None
    try:
        from fundai import configedit
        res = configedit.apply_changes({
            "account.initial_cash": cash,
            "account.target_multiple": round(mult_eff, 6),
        })
        cfg_written = bool(res.get("ok"))
        if not cfg_written and res.get("errors"):
            print("提示：配置写回跳过（{}）".format("；".join(res["errors"])[:160]))
    except Exception as e:
        print("提示：配置写回失败（{}），但账本已初始化".format(str(e)[:120]))
    jprint({"ok": True, "db": str(db),
            "initial_cash": cash, "target_value": target,
            "target_multiple": round(mult_eff, 6),
            "config_updated": cfg_written,
            "message": "账户就绪：{:.0f} 元起步，目标 {:.0f} 元（+{:.0f}%），期限 {} ~ {}".format(
                cash, target, (mult_eff - 1) * 100,
                meta.get("start_date"), meta.get("end_date"))})


def cmd_run_daily(args):
    cfg = settings.load_config()
    ledger = Ledger(str(Path(args.db)))
    eng = Engine(cfg, ledger)
    out = eng.run_daily(force=args.force)
    print("== {} ==".format(out.get("date", "?")))
    print("状态   :", out.get("status"))
    if out.get("status") in ("ok", "noop"):
        if out.get("view"):
            print("观点   :", out.get("view"), "评分", out.get("score"),
                  "来源", out.get("source"))
        if out.get("message"):
            print("消息   :", out.get("message"))
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


def cmd_signals_update(args):
    """把近期已入库消息结算成“消息→下一交易日涨跌”样本（幂等）。"""
    from fundai import calib, screening, util
    scr = screening.ScreeningStore()
    closes, dates = calib.load_closes(refresh=True)  # 20:30 跑：先确保今日收盘在缓存里
    lo = util.add_days(util.today_str(), -(max(3, int(args.days)) * 2))
    ds_list = [r[0] for r in scr.conn.execute(
        "SELECT DISTINCT date FROM items WHERE date>=?", (lo,))]
    total = 0
    for ds in ds_list:
        total += calib.update_samples(scr.items_for(ds), closes, dates)
    rep = calib.report()
    jprint({"updated": total, "settled_total": rep["total"],
            "by_event": rep["by_event"]})
    print("提示：样本按交易日逐日积累（含下一交易日涨跌才结算）；"
          "历史电报可用 python app.py news-calibrate --days 90 一次性回填校准"
          "（时间游标回溯，已实测可用），不必再手工导入。")
    return 0


def cmd_direction_check(args):
    """AI 次日方向判断：自动记录 + 自检命中率结算（幂等，可单独跑/补跑）。

    为什么要有这条命令（2026-09-12 用户反馈“每天跑完诊断不再自动记录方向”）：
      “记录方向 + 结算命中率”原先只写在 `run_daily` 主流程中段，而 `run_daily` 在
      “当日已有研判记录”时会短路——于是当天第二次运行时方向与自检双双被跳过。
      现在它是一条**独立任务链**：run_daily 主流程、run_daily 短路路径、计划任务
      都能调到同一个入口，出问题也能单跑这一条命令补齐，不必重跑整天研判。

    口径（与首页“观点”同源，未改动任何算法）：方向 = 平滑后合成评分经 view_of 推导，
    下一交易日收盘后按“看多=涨 / 看空=跌 / 中性=|涨跌|≤0.3%”结算命中率。

    用法：
        python app.py direction-check                 # 今天（=最新交易日）记录 + 结算
        python app.py direction-check --date 2026-09-11
        python app.py direction-check --date 2026-09-11 --refresh   # 先联网刷新指数K线
        python app.py direction-check --force         # 同日重跑也覆盖方向
    """
    from fundai import indicators, util
    cfg = settings.load_config()
    ledger = Ledger(str(Path(args.db)))
    eng = Engine(cfg, ledger)
    # 指数K线（决定“判断日”与当日涨跌）：默认走数据源缓存；--refresh 强制在线刷新
    need_from = util.add_days(util.today_str(), -175)
    if getattr(args, "refresh", False):
        try:
            idx = eng.market.index_history(need_from=need_from)
        except DataError as e:
            print("指数数据刷新失败：{}".format(e))
            idx = []
    else:
        try:
            idx = eng.market.index_history(need_from=need_from, offline=True)
        except (DataError, TypeError):
            idx = []
    if not idx:
        try:
            idx = eng.market.index_history(need_from=need_from)
        except DataError as e:
            print("指数数据获取失败：{}".format(e))
            return 1
    dates = [x[0] for x in idx]
    closes = [x[1] for x in idx]
    vols = [x[2] for x in idx]
    kline_date = str(dates[-1])[:10]              # 指数K线最新交易日（=结算口径的当日）
    D = str(args.date or kline_date)[:10]         # 判断日（可显式指定，含非交易日）
    st = indicators.last_stats(dates, closes, vols)
    chg = (st.get("chg_pct") if st.get("date") == kline_date else None)
    if kline_date != D:
        print("提示：指数K线最新交易日为 {}（{} 无收盘K线，按非交易日/未收盘处理）："
              "方向判断沿用该交易日收盘研判，结算用 {} 当日涨跌".format(
                  kline_date, D, kline_date))
    dc = eng.direction_record(D, force=bool(args.force),
                              settle_with_chg=chg, settle_date=kline_date)
    stats = eng.screen.direction_stats() if eng.screen else {}
    latest = (stats.get("history") or [None])[0]
    out = {
        "date": D,
        "market_date": kline_date,
        "index": eng._idx_name(),
        "day_chg": (None if chg is None else round(chg, 4)),
        "direction": dc.get("direction"),
        "judge_date": dc.get("from_date"),
        "filed_as": dc.get("filed_as"),
        "confidence": (None if dc.get("confidence") is None
                       else round(float(dc["confidence"]), 3)),
        "recorded": bool(dc.get("recorded")),
        "kept": bool(dc.get("kept")),
        "settled": dc.get("settled", 0),
        "stats": {"total": stats.get("total"), "hit": stats.get("hit"),
                  "hit_rate": stats.get("hit_rate"),
                  "by_dir": stats.get("by_dir")},
        "latest_settled": latest,
        "skipped": dc.get("skipped"),
        "error": dc.get("error"),
    }
    jprint(out)
    dircn = {"bull": "看多", "bear": "看空", "neutral": "中性"}.get(
        out["direction"], out["direction"] or "—")
    if dc.get("error"):
        print("结果   : 失败 —— {}".format(dc["error"]))
        return 1
    if out["skipped"]:
        print("结果   : 跳过 —— {}".format(out["skipped"]))
        return 1
    print("判断日 : {}（{} {} {:+.2%}）".format(
        D, eng._idx_name(), kline_date, chg or 0.0))
    if dc.get("from_date") and dc.get("from_date") != D:
        print("口径   : 沿用 {} 收盘研判，并记在该交易日的判断名下"
              "（{} 非交易日，避免同一份数据重复计数）".format(dc["from_date"], D))
    print("AI方向 : {}（信心 {:.0%}）{}".format(
        dircn, float(dc.get("confidence") or 0),
        "· 已记录" if dc.get("recorded") else "· 该判断日已有同向记录，保持不变"))
    if dc.get("settled"):
        print("已结算 : {} 条到期判断".format(dc["settled"]))
    if latest:
        print("最新自检: {} → {} 结算 {}（{}）".format(
            latest.get("date"), latest.get("dir"),
            latest.get("resolved_date"),
            "命中" if latest.get("hit") else "未中"))
    print("累计命中率: {}（命中 {} / 结算 {}）——网页「消息与进化」页可看明细".format(
        "—" if stats.get("hit_rate") is None else "{:.1%}".format(stats["hit_rate"]),
        stats.get("hit"), stats.get("total")))
    return 0


def cmd_signals_report(args):
    """输出 事件类型 × 命中率 校准报告。"""
    from fundai import calib
    jprint(calib.report())
    return 0


def cmd_cls_import(args):
    """把财联社手动导出的电报文件(json/jsonl)并入某日消息库（源=财联社电报·手动导入）。"""
    from fundai import calib, screening
    date_s = str(args.date or util.today_str())[:10]
    feed = calib.cls_export_rows(args.file)
    if not feed:
        print("文件无有效行（需 [{time,title,text},...] 或 JSONL）")
        return 1
    scr = screening.ScreeningStore()
    n = scr.ingest_feed(date_s, feed, skip_recent=True)
    print("已并入 {} 条（去重后新增 {}）到 {}；随后运行 python app.py signals-update 参与样本结算".format(
        len(feed), n, date_s))
    return 0


def cmd_fetch_history(args):
    """把主基准指数历史K线一次性延伸至 --years 年并写本地缓存（智兔/东财，
    约各 1 次全区间请求）。为长周期回测/历史语境准备数据（等价于 BaoStock
    免费十年行情的接入方式，无强依赖、无需装包）。"""
    from fundai import settings, util
    from fundai.datasource import Market
    cfg = settings.load_config()
    mkt = Market(cfg)
    res = mkt.extend_index_history(years=args.years,
                                   with_benchmarks=args.bench)
    res["cache_file"] = str(util.cache_file(
        "kline_{}.json".format(re.sub(r"[^0-9A-Za-z]", "_", mkt._index_key()))))
    jprint(res)
    print("提示：主基准指数缓存已延伸；长窗口样本外回测可用 python app.py oos --months 36 验证。")
    return 0


def cmd_news_calibrate(args):
    """事件命中率校准：历史电报按线上同口径打分 → 对照次日沪深300涨跌。"""
    from fundai import calib_history as calibration

    def prog(pages, oldest):
        if pages % 20 == 0:
            print("  …已回溯 {} 页".format(pages), flush=True)
    if getattr(args, "merge_caches", None):
        from fundai import news as _news
        names = [x.strip() for x in str(args.merge_caches).split(",") if x.strip()]
        res = _news.merge_history_caches(names)
        print("合并完成：{} 条 → {} 条（新增 {}）".format(
            res["before"], res["after"], res["added"]))
        return 0
    if getattr(args, "fetch_only", False):
        try:
            hist = calibration.fetch_only(days=args.days, max_pages=args.max_pages,
                                          delay=args.delay, progress=prog,
                                          until=getattr(args, "until", None),
                                          cache_name=getattr(args, "cache_name", None))
        except Exception as e:
            print("抓取失败：{}".format(e))
            return 1
        print("抓取完成：窗口内 {} 条电报，翻页 {} 页，缓存累计 {} 条".format(
            len(hist.get("items") or []), hist.get("pages"),
            hist.get("cache_total")))
        return 0
    try:
        cal = calibration.calibrate(
            days=args.days, max_pages=args.max_pages, delay=args.delay,
            min_samples=args.min_samples, progress=prog,
            use_cache=not args.fresh)
    except Exception as e:
        print("校准失败：{}".format(e))
        return 1
    print(calibration.text_report(cal))
    mults = cal.get("suggest_multipliers") or {}
    if mults:
        print("\n建议倍数（经收缩；默认不生效，config.json → strategy.event_calibration=true 才应用）：")
        for k, v in sorted(mults.items(), key=lambda kv: -kv[1]):
            print("  {:<26} {:.3f}".format(k, v))
    print("\n报告已写入 data\\event_calibration.json（网页「消息与进化」页可查看）")
    return 0


def cmd_event_model(args):
    """事件 → 次日方向：walk-forward 样本外评估 + 按把握度分档的选择性预测。"""
    from fundai import event_model
    try:
        rep = event_model.run(days=args.days, warmup=args.warmup, band=args.band,
                              min_edge=args.min_edge, l2=args.l2,
                              refit_every=args.refit, save=not args.no_save,
                              select=args.select, feature_set=args.features)
    except Exception as e:
        print("建模失败：{}".format(e))
        return 1
    print(event_model.text_report(rep))
    if rep.get("weights"):
        print("\n系数绝对值 top12（全样本拟合，仅供理解模型，不参与评估）：")
        for w in rep["weights"]:
            print("  {:<30} {:+.3f}".format(w["feature"], w["coef"]))
    if not args.no_save:
        print("\n报告已写入 data\\event_model.json")
    return 0


def cmd_news_fetch_db(args):
    """把财联社历史电报抓进 SQLite（可多段并行：--jobs，默认 CPU 核数）。"""
    from fundai import news, clsdb, util
    jobs = getattr(args, "jobs", None)

    def prog(pages, oldest, tag=None):
        if pages % 100 == 0:
            print("  …{}已回溯 {} 页，库内 {:.1f} 万条".format(
                (tag + " ") if tag else "", pages,
                clsdb.stats()["telegrams"] / 10000.0), flush=True)
    t0 = util.now_dt()
    res = news.cls_fetch_to_db(days=args.days, until=args.until,
                               max_pages=args.max_pages, delay=args.delay,
                               progress=prog, jobs=jobs)
    clsdb.backfill_dates()
    print("抓取完成：翻页 {}，本次新增 {} 条，库内共 {} 条（{} → {}），耗时 {:.1f} 分钟{}".format(
        res["pages"], res["added"], res["total"], res["from"], res["to"],
        (util.now_dt() - t0).total_seconds() / 60.0,
        "，并行 {} 段".format(res["jobs"]) if res.get("jobs") else ""))
    print("库状态:", clsdb.stats())
    return 0


def cmd_news_score_db(args):
    """把库内未打分的电报按线上同口径打分（增量，可多进程并行）。"""
    from fundai import news, clsdb
    jobs = getattr(args, "jobs", None)

    def prog(done, total):
        if done % 20000 == 0:
            print("  …已打分 {}/{}".format(done, total), flush=True)
    t0 = util.now_dt()
    res = news.score_db(limit=args.limit, progress=prog, jobs=jobs)
    print("打分完成：{} 条（剩余 {}，并行度 {}），耗时 {:.1f} 秒".format(
        res["scored"], res["pending"], res.get("jobs", 1),
        (util.now_dt() - t0).total_seconds()))
    print("库状态:", clsdb.stats())
    return 0


def cmd_intraday_pulse(args):
    """午间盘中诊断（12:00 计划任务）：上午行情 + 消息面 + 风险信号 → 短评并落库。"""
    from fundai import intraday
    if getattr(args, "settle", False):
        r = intraday.settle()
        print(json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r)
        return 0 if r.get("ok") else 1
    r = intraday.run_midday(force=getattr(args, "force", False))
    print(r.get("summary") or r.get("message") or json.dumps(r, ensure_ascii=False))
    st = intraday.stats(20)
    if st.get("total"):
        print("午间观点自检：命中 {}/{}（{:.0%}）".format(
            st["hit"], st["total"], st["hit_rate"] or 0))
    return 0 if r.get("ok") else 1


def cmd_micro_pulse(args):
    """涨停情绪复盘（微观结构脉搏）：借鉴 Cailianpress-Feishu-Bot 的同花顺量化复盘算法。"""
    from fundai import microstructure
    date_s = str(args.date or util.today_str())[:10]
    pulse = microstructure.load_pulse(date_s, force=args.force,
                                      cfg=settings.load_config())
    out = {"ok": bool(pulse.get("ok")), "date": date_s}
    if not pulse.get("ok"):
        out["message"] = pulse.get("message")
        jprint(out)
        return 1
    snap = pulse["snap"]
    out["score"] = snap.get("score")
    out["parts"] = snap.get("parts")
    out["qualitative"] = snap.get("qualitative")
    hist = microstructure.history_pulses(date_s, days=args.days)
    out["history"] = [{"date": h.get("date"), "score": h.get("score"),
                       "zt": h.get("zt"), "dt": h.get("dt"),
                       "promotion_rate": h.get("promotion_rate"),
                       "zb_rate": h.get("zb_rate")} for h in hist]
    jprint({k: v for k, v in out.items() if k != "history"})
    print("—— 复盘正文 ——")
    for ln in microstructure.review_lines(pulse):
        print(ln)
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
                                description="AI 基金实验（投入金额可自定义，目标 = 投入 × 1.3；仅场外基金）")
    sub = p.add_subparsers(dest="cmd")

    i = sub.add_parser("init", help="初始化正式账户（投入金额可自定义，目标=投入×倍数）")
    i.add_argument("--db", default=str(DB_LIVE))
    i.add_argument("--reset", action="store_true", help="清空账本重新开始")
    i.add_argument("--cash", type=float, default=None,
                   help="投入总金额（默认读取 config.json → account.initial_cash，默认 1000）")
    i.add_argument("--target-multiple", type=float, default=None,
                   help="目标倍数：目标 = 投入 × 该值（默认取配置 account.target_multiple，即 1.3）")
    i.add_argument("--target", type=float, default=None,
                   help="直接指定目标金额（优先于 --target-multiple，用于兼容旧用法）")
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

    fh = sub.add_parser("fetch-history",
                        help="把主基准指数历史K线延伸至N年写本地缓存（供长周期回测/历史语境）")
    fh.add_argument("--years", type=int, default=8)
    fh.add_argument("--bench", action="store_true",
                    help="同时抓取页面大盘指数条的长期K线（东财）")
    fh.set_defaults(fn=cmd_fetch_history)

    su = sub.add_parser("signals-update",
                        help="把已入库消息结算成 消息→下一交易日涨跌 样本（幂等）")
    su.add_argument("--days", type=int, default=7)
    su.set_defaults(fn=cmd_signals_update)

    dc = sub.add_parser("direction-check",
                        help="AI 次日方向判断：自动记录 + 自检命中率结算（可补跑指定日期）")
    dc.add_argument("--db", default=str(DB_LIVE))
    dc.add_argument("--date", default=None,
                    help="判断日 YYYY-MM-DD（默认=指数K线最新交易日）")
    dc.add_argument("--refresh", action="store_true",
                    help="先用数据源刷新指数K线（需要刚收盘那根 bar 时用）")
    dc.add_argument("--force", action="store_true",
                    help="该日已有记录时也覆盖方向（默认保留首次判断）")
    dc.set_defaults(fn=cmd_direction_check)

    sr = sub.add_parser("signals-report", help="输出 事件类型×命中率 校准报告")
    sr.set_defaults(fn=cmd_signals_report)

    ci = sub.add_parser("cls-import",
                        help="导入财联社手动导出的电报 json/jsonl 到指定日期消息库")
    ci.add_argument("--file", required=True)
    ci.add_argument("--date", default=None)
    ci.set_defaults(fn=cmd_cls_import)

    mp = sub.add_parser("micro-pulse",
                        help="涨停情绪复盘：晋级率/炸板率/题材持续性/连板梯队/情绪分（同花顺微观结构）")
    mp.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    mp.add_argument("--force", action="store_true", help="忽略缓存强制重抓")
    mp.add_argument("--days", type=int, default=5, help="附带最近 N 日情绪史")
    mp.set_defaults(fn=cmd_micro_pulse)

    nc = sub.add_parser("news-calibrate",
                        help="事件命中率校准：财联社历史电报（时间游标回溯）→ 次日涨跌命中率")
    nc.add_argument("--days", type=int, default=10, help="回溯自然日数（默认 10）")
    nc.add_argument("--max-pages", type=int, default=600,
                    help="最多翻页数（每页 50 条，默认 600）")
    nc.add_argument("--delay", type=float, default=0.3, help="翻页间隔秒，礼貌限速")
    nc.add_argument("--min-samples", type=int, default=5,
                    help="给出建议倍数所需最小样本数")
    nc.add_argument("--fresh", action="store_true", help="忽略磁盘缓存重新抓取")
    nc.add_argument("--fetch-only", dest="fetch_only", action="store_true",
                    help="只抓历史电报入库（不跑校准），适合先长时间回填")
    nc.add_argument("--until", default=None,
                    help="分段并行用：从该日期（YYYY-MM-DD）开始往回抓")
    nc.add_argument("--cache-name", dest="cache_name", default=None,
                    help="分段并行用：自定义缓存文件名（各 worker 各写各的）")
    nc.add_argument("--merge-caches", dest="merge_caches", default=None,
                    help="把逗号分隔的分段缓存合并进主缓存（可与其他参数同时使用）")
    nc.set_defaults(fn=cmd_news_calibrate)

    em = sub.add_parser("event-model",
                        help="事件→次日方向 的样本外(walk-forward)评估与选择性预测（分档命中率）")
    em.add_argument("--days", type=int, default=400, help="使用最近 N 个自然日的缓存电波")
    em.add_argument("--warmup", type=int, default=40, help="前 N 天只用于训练/热身")
    em.add_argument("--band", type=float, default=0.003,
                    help="次日 |涨跌| 小于该值算无方向噪声（默认 0.3%%）")
    em.add_argument("--min-edge", type=float, default=0.02,
                    help="|p−0.5| 达到该值才算出“出手”")
    em.add_argument("--l2", type=float, default=2.0, help="L2 正则强度")
    em.add_argument("--refit", type=int, default=5, help="每 N 天重训一次（walk-forward）")
    em.add_argument("--no-save", action="store_true", help="不写 data/event_model.json")
    em.add_argument("--select", action="store_true",
                    help="额外做“配置选择期/留出期分离”评估（较慢但最可信）")
    em.add_argument("--features", default="market",
                    choices=["all", "market", "market_events", "no_dict", "events"],
                    help="特征组（默认 market＝指数状态+跨市场风格，实测最稳）")
    em.set_defaults(fn=cmd_event_model)

    fd = sub.add_parser("news-fetch-db",
                        help="把财联社历史电报抓进 SQLite（多段并行回填，4 年量级）")
    fd.add_argument("--days", type=int, default=300, help="本段回溯自然日数")
    fd.add_argument("--until", default=None, help="起点（YYYY-MM-DD），指定后只抓这一段")
    fd.add_argument("--max-pages", type=int, default=20000, help="本段最多翻页数")
    fd.add_argument("--delay", type=float, default=0.25, help="翻页间隔秒")
    fd.add_argument("--jobs", type=int, default=None,
                    help="并行段数（默认 CPU 核数；1=串行；也可用 FUNDAI_JOBS）")
    fd.set_defaults(fn=cmd_news_fetch_db)

    sd = sub.add_parser("news-score-db",
                        help="把库内未打分电报按线上同口径打分（增量，可多进程并行）")
    sd.add_argument("--limit", type=int, default=None, help="最多打分条数")
    sd.add_argument("--jobs", type=int, default=None,
                    help="并行度（默认 CPU 核数；1=串行；也可用环境变量 FUNDAI_JOBS）")
    sd.set_defaults(fn=cmd_news_score_db)

    ip = sub.add_parser("intraday-pulse",
                        help="午间盘中诊断（12:00 自动任务）：上午行情+消息面+风险信号→短评并落库")
    ip.add_argument("--force", action="store_true", help="忽略缓存重取行情")
    ip.add_argument("--settle", action="store_true",
                    help="收盘后结算当日午间观点（用 收盘/11:30 判定命中）")
    ip.set_defaults(fn=cmd_intraday_pulse)

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
