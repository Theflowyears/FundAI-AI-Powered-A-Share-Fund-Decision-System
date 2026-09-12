# -*- coding: utf-8 -*-
"""内置 Web 服务：静态界面 + JSON API（仅标准库 http.server）。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import analysis
from . import news as newsmod
from . import screening, settings, util
from .datasource import Market
from .engine import Engine
from .ledger import Ledger
from .util import DataError


def _static_dir():
    """静态资源目录（同时支持源码树与安装包两种布局）。

    * 源码树 / 便携包：`<项目根>/web/static`
    * pip 安装（wheel）：`fundai/static`（打进包里的副本）
    打包纪律：两处内容必须一致（构建前由 `tools/make_release.py` 同步校验）。
    """
    cands = [util.PROJECT / "web" / "static",
             Path(__file__).resolve().parent / "static"]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


STATIC_DIR = _static_dir()
BACKTEST_FILE = util.data_file("backtest_last.json")

ALLOWED_STATIC = {"index.html", "app.js", "style.css", "vendor/echarts.min.js"}

# 人工打标额度：每天最多挑这么多“AI 无法确定(中性)”的消息给用户确认，
# 其余方向明确(利好/利空)的消息直接采用 AI 判断，减少人工负担（audit 迭代）。
MAX_REVIEW_QUEUE = 50


class ApiApp:
    """共享给所有请求的上下文（含线程安全的 SQLite 连接）。"""

    def __init__(self, cfg, db_path, demo=False):
        self._cfg = cfg
        self.db_path = db_path
        self.demo = demo
        self.ledger = Ledger(str(db_path),
                             initial_cash=float(cfg["account"]["initial_cash"]))
        self.market = Market(cfg)
        self.engine = Engine(cfg, self.ledger, self.market, demo=demo)
        self.engine.ensure_account()
        self.screen = screening.ScreeningStore()
        self._busy = threading.Lock()
        # 服务端快照缓存：GET 只读轮询不再每次都遍历全池在线拉净值（audit P1-9）
        self._snap = {}
        self._snap_lock = threading.Lock()
        # 资产曲线自愈：启动后台补记近日缺失快照（缺净值日自动跳过，不阻塞启动）
        if not demo:
            def _heal_curve():
                time.sleep(3.0)
                try:
                    self.engine.backfill_snapshots(days=7)
                except Exception:
                    pass
            threading.Thread(target=_heal_curve, daemon=True).start()

    @property
    def cfg(self):
        """配置热更新：config.json 改动后无需重启服务即生效（settings 自带
        mtime 缓存，未变更时是零成本判断；演示库的内存改值不参与）。
        文件被编辑成非法 JSON 时保留旧配置继续服务，不打断运行中的流程。"""
        if not self.demo:
            try:
                fresh = settings.load_config()
            except Exception:
                return self._cfg
            if fresh is not self._cfg:
                self._cfg = fresh
                self.engine.cfg = fresh
                self.market.reload(fresh)
        return self._cfg

    def _snapshot(self, key, ttl, fn):
        """按 key 做 TTL 快照缓存（ttl 秒）。fn 返回可 JSON 序列化对象。"""
        now = time.time()
        with self._snap_lock:
            hit = self._snap.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self._snap_lock:
            self._snap[key] = (time.time(), val)
        return val

    def _invalidate(self, key=None):
        with self._snap_lock:
            if key:
                self._snap.pop(key, None)
            else:
                self._snap.clear()

    # ---------------- 消息与进化（筛选/学习/搜索补全） ----------------
    def news_payload(self, date_s=None):
        """组装“消息与进化”页：当日 feed、打标概况、学习词、方向命中率、补全队列。"""
        date_s = date_s or util.today_str()
        scr = self.screen
        amp = int(((self.cfg.get("strategy") or {}).get("news_amp") or 8))
        nmap = {}
        for f in self.engine._pool_items():
            nmap[f["code"]] = f.get("name") or f["code"]
        items = scr.items_for(date_s)  # 时间倒序
        # 例行噪音自动归档：ETF 溢价/临停（产品层面）、机构持股比例权益披露
        # （单一个股筹码趋势）等 → AI 判中性，不占人工额度；已打标的照常处理
        auto_ids = {r["item_id"] for r in items if not r.get("user_label")
                    and r.get("event_type") in screening.AUTO_NEUTRAL_EVENTS}
        auto_first = [r for r in items if r["item_id"] in auto_ids]
        rest = [r for r in items if r["item_id"] not in auto_ids]
        # 兜底：同模板批量刷屏（骨架相似的 AI 中性消息）归并，只留代表条待确认
        rest, dupes = screening.collapse_same_template(rest)
        dup_ids = {r["item_id"] for r in dupes}
        review_head, neutral_extra, ai_head, done = \
            screening.ScreeningStore.review_split(rest, MAX_REVIEW_QUEUE)
        todo_ids = {r["item_id"] for r in review_head}
        ordered = review_head + auto_first + neutral_extra + dupes + ai_head + done
        feed = []
        for r in ordered:
            d = dict(r)
            # 前端打标按钮用 it.id（见 app.js renderNews）；SQLite 行主键叫 item_id，
            # 这里补一个 id 别名，避免前端提交 “undefined” 导致打标 500。
            d["id"] = d.get("item_id") or d.get("id") or ""
            dup_of = d.pop("_dup_of", None)
            if dup_of:
                d["dup_of"] = dup_of
            d["needs_review"] = not d.get("user_label") and \
                d.get("item_id") in todo_ids
            et = d.get("event_type")
            d["auto_handled"] = not d.get("user_label") and \
                (d.get("item_id") in auto_ids or d.get("item_id") in dup_ids)
            d["auto_kind"] = ("dup" if d.get("item_id") in dup_ids else
                              ("premium" if et == "fund_premium_warning" else
                               ("disclosure" if et == "holdings_disclosure"
                                else "")))
            d["ai_decided"] = not d.get("user_label") and \
                not d["needs_review"] and not d["auto_handled"] and \
                (d.get("auto_label") or "neutral") != "neutral"
            d["funds_links"] = [{"code": c, "name": nmap.get(c, c)}
                                for c in (d.get("funds") or [])]
            feed.append(d)
        # 自动消息分：**必须与引擎同一口径**（来源过滤 + 抗饱和刻度 + LLM 降噪）。
        # 旧实现用 clamp(Σ强度,±8)：日强度常达 300~900 → 天天顶格 +8 → 卡片恒显示 +64，
        # 且与引擎实际使用的分值不一致（audit 2026-09-11 用户反馈）。
        st_cfg = (self.cfg.get("strategy") or {})
        auto_scope, auto_net, auto_net_src = {}, None, "store"
        cached_news = {}
        try:
            p = newsmod.news_cache_file(date_s)
            if p.exists():
                cached_news = util.load_json(p, {}) or {}
        except Exception:
            cached_news = {}
        if cached_news.get("ok") and cached_news.get("scope"):
            # 当日消息面已抓过 → 直接用它的分数与口径（引擎用的就是这份）
            auto_net = cached_news.get("net")
            auto_score = cached_news.get("score")
            auto_scope = cached_news.get("scope") or {}
            auto_net_src = "cache"
        else:
            focus = list(st_cfg.get("news_focus_events")
                         or newsmod.DEFAULT_FOCUS_EVENTS)
            dict_mode = str(st_cfg.get("news_dict_mode") or "off")
            include_other = bool(st_cfg.get("news_include_other_events", False))
            mode = str(st_cfg.get("news_net_mode") or "scaled")
            scale = float(st_cfg.get("news_net_scale") or 70.0)
            net, raw, n_in = newsmod.net_score(
                items, mode=mode, scale=scale, focus=focus, dict_mode=dict_mode,
                include_other=include_other)
            auto_net = round(net, 3)
            auto_score = int(max(-100, min(100, net * max(1, amp))))
            auto_scope = {"focus_events": focus, "dict_mode": dict_mode,
                          "include_other": include_other, "n_in_scope": n_in,
                          "net_raw": round(raw, 1), "mode": mode, "scale": scale,
                          "note": "按当前口径近似重算（当日无消息面缓存，LLM 降噪未计入）"}
        labels = scr.labels_summary(date_s)
        human = scr.human_news_meta(date_s, amplitude=amp)
        learned = scr.lexicon_rows(120)
        from . import lexicon
        direction = scr.direction(date_s)
        # 跨天显示：今天还没跑研判时，把“昨日给出、针对今天”的判断补上（否则面板空白）
        direction_for_today = direction or scr.direction_pending(date_s)
        stats = scr.direction_stats()
        qstate = util.load_json(screening.SEARCH_QUEUE_FILE, {"items": []})
        queue = qstate.get("items", [])
        return {
            "date": date_s,
            "feed": feed,
            "feed_count": len(items),
            "todo": len(review_head),        # 待你人工确认（≤MAX_REVIEW_QUEUE）
            "review_extra": len(neutral_extra),  # 超出额度未排入优先的中性条数
            "auto_archived": len(auto_first) + len(dupes),  # 例行披露/同模板归并
            "ai_accepted": len(ai_head),     # 直接采用 AI 判断（利好/利空明确）
            "done": len(done),
            "auto_score": auto_score, "auto_net": auto_net,
            "auto_scope": auto_scope, "auto_net_src": auto_net_src,
            "human": human,
            "labels": labels,
            "learned": learned,
            "learned_total": len(learned),
            "base_lexicon": {"bull": len(lexicon.BULL_STRONG) + len(lexicon.BULL_WEAK),
                             "bear": len(lexicon.BEAR_STRONG) + len(lexicon.BEAR_WEAK)},
            "direction": direction,
            "direction_today": direction_for_today,
            "direction_is_pending": bool(direction is None and direction_for_today),
            "stats": stats,
            "queue": queue,
            "queue_open": [x for x in queue if x.get("status") == "open"],
            "last_merge": util.load_json(screening.SEARCH_LAST_FILE),
            "results_ready": bool((util.load_json(screening.SEARCH_RESULTS_FILE, {})
                                   or {}).get("results")),
            "cfg": {"news_amp": amp,
                    "news_weight": float((self.cfg.get("strategy") or {}).get(
                        "news_weight", 0.35))},
            "now": util.now_iso(),
        }

    def news_pull(self, date_s, force=True):
        """在线多源拉取并入库；force=True 时忽略当日缓存（重抓以便学习词生效）。"""
        amp = int(((self.cfg.get("strategy") or {}).get("news_amp") or 8))
        news_obj = newsmod.load_news(date_s, amplitude=amp, force=force,
                                     cfg=self.cfg)
        if news_obj.get("ok"):
            self.screen.ingest_feed(date_s, news_obj.get("feed") or [])
        return news_obj

    # ---------------- 事件命中率校准 & 历史数据状态（可视化） ----------------
    def start_calib_backfill(self, days=20, max_pages=700):
        """后台跑“财联社历史电报 → 事件命中率校准”（约 2–5 分钟，逐页回溯+缓存续跑）。

        常驻服务每 15 分钟会被保活任务重启，因此进度随时落盘（每 10 页存一次），
        被重启也能断点续跑；前端轮询 /api/calibration 看进度。
        """
        if getattr(self, "_calib_busy", False):
            return {"ok": False, "message": "历史回填校准正在运行中（{}）".format(
                getattr(self, "_calib_msg", ""))}
        self._calib_busy = True
        self._calib_msg = "启动中…"

        def _run():
            try:
                from . import calib_history
                cal = calib_history.calibrate(
                    days=int(days), max_pages=int(max_pages), delay=0.3,
                    progress=lambda p, o: setattr(
                        self, "_calib_msg", "已回溯 {} 页（每页 50 条）".format(p)))
                self._calib_msg = "完成：{} → {}，电报 {} 条".format(
                    cal["window"]["from"], cal["window"]["to"],
                    (cal.get("data") or {}).get("telegrams"))
                self._invalidate("calib")
            except Exception as e:
                self._calib_msg = "失败：{}".format(str(e)[:120])
            finally:
                self._calib_busy = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "已开始历史回填校准（后台运行，约 2–5 分钟）"}

    def start_event_model(self, days=400, select=True, warmup=60):
        """后台跑“事件→次日方向”样本外模型（打分缓存 + walk-forward + 选择性出手）。"""
        if getattr(self, "_model_busy", False):
            return {"ok": False, "message": "模型正在运行中（{}）".format(
                getattr(self, "_model_msg", ""))}
        self._model_busy = True
        self._model_msg = "启动中…"

        def _run():
            try:
                from . import event_model
                self._model_msg = "打分缓存 + walk-forward 评估中…"
                rep = event_model.run(days=int(days), warmup=int(warmup),
                                      select=bool(select))
                m = rep.get("metrics") or {}
                self._model_msg = "完成：{} 天（样本外 {} 天，整体命中 {}）".format(
                    (rep.get("window") or {}).get("days"), m.get("n"),
                    ("{:.0%}".format(m["acc"]) if m.get("acc") is not None else "—"))
                self._invalidate("calib")
            except Exception as e:
                self._model_msg = "失败：{}".format(str(e)[:120])
            finally:
                self._model_busy = False
        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "已开始样本外建模（后台运行，约 1–3 分钟）"}

    def calibration_payload(self):
        """消息→次日涨跌 样本统计 + 主指数历史缓存状态 + 最近结算日志尾部。"""
        from . import calib
        rep = {"total": 0, "by_event": {}}
        try:
            rep = calib.report(cfg=self.cfg)
        except Exception as e:
            rep = {"total": 0, "by_event": {}, "error": str(e)[:150]}
        hist = {}
        try:
            _closes, dates = calib.load_closes(cfg=self.cfg)
            hist = {"bars": len(dates),
                    "start": dates[0] if dates else None,
                    "end": dates[-1] if dates else None}
        except Exception:
            hist = {"bars": 0}
        tail = []
        try:
            p = util.data_file("signals_update.log")
            if p.exists():
                raw = p.read_text("utf-8", errors="replace")
                tail = [ln for ln in raw.strip().splitlines() if ln.strip()][-6:]
        except Exception:
            tail = []
        back = {}
        try:
            from . import calib_history
            back = calib_history.load_calibration()
        except Exception:
            back = {}
        model = {}
        try:
            from . import event_model
            model = util.load_json(util.data_file(event_model.MODEL_FILE), {}) or {}
        except Exception:
            model = {}
        return {"report": rep, "history": hist, "last_run": tail,
                "hist_backfill": back, "backfill_running":
                    bool(getattr(self, "_calib_busy", False)),
                "backfill_msg": getattr(self, "_calib_msg", ""),
                "event_model": model, "model_running":
                    bool(getattr(self, "_model_busy", False)),
                "model_msg": getattr(self, "_model_msg", ""),
                "next_auto": "每交易日 20:30 自动结算（计划任务 fundai_daily_signals）"}

    # ---------------- 微观结构情绪复盘（涨停池/晋级率/题材热度） ----------------
    def micro_payload(self, force=False):
        from . import microstructure
        st = self.cfg.get("strategy", {}) or {}
        if not st.get("micro_enable", True):
            return {"ok": False, "enabled": False,
                    "message": "已关闭（strategy.micro_enable=false）"}
        D = util.today_str()
        pulse = microstructure.load_pulse(D, force=force, cfg=self.cfg)
        if not pulse.get("ok") and not microstructure.load_snap(D):
            # 盘中/清晨当日尚无收盘数据 → 回退上一交易日复盘（morning 可用）
            D2 = util.prev_trading_day(D)
            p2 = microstructure.load_pulse(D2, cfg=self.cfg)
            if p2.get("ok"):
                p2["asof_note"] = "当日收盘数据尚未生成，展示 {} 复盘".format(D2)
                pulse = p2
        hist = microstructure.history_pulses(
            (pulse.get("snap") or {}).get("date") or D,
            days=int(st.get("micro_hist_days", 5)), cfg=self.cfg)
        heat, heat_meta, context = {}, {}, {}
        try:
            context = microstructure.sentiment_context(
                (pulse.get("snap") or {}).get("date") or D, cfg=self.cfg)
        except Exception:
            context = {}
        try:
            rep = microstructure.theme_heat_report(hist)   # 内部按 date 自排，勿再反转
            heat = rep.get("map") or {}
            heat_meta = {"days": rep.get("days") or [], "decay": rep.get("decay") or [],
                         "date": rep.get("date"), "market_heat": rep.get("market_heat"),
                         "trend": rep.get("trend"), "ignitions": rep.get("ignitions") or [],
                         "zt_latest": rep.get("zt_latest"), "zt_prev": rep.get("zt_prev"),
                         "rows": (rep.get("rows") or [])[:12]}
        except Exception:
            pass
        snap = dict(pulse.get("snap") or {})
        snap.pop("zt_codes", None)  # 体积控制：不向浏览器下发全量代码
        slim_hist = [{"date": h.get("date"), "score": h.get("score"),
                      "zt": h.get("zt"), "zb_rate": h.get("zb_rate"),
                      "dt": h.get("dt"), "max_board": h.get("max_board"),
                      "promotion_rate": h.get("promotion_rate"),
                      "rise_ratio": h.get("rise_ratio"),
                      "mood": (h.get("qualitative") or {}).get("mood")}
                     for h in hist]
        return {"ok": bool(pulse.get("ok")), "enabled": True,
                "date": snap.get("date") or D, "asof": pulse.get("asof_note"),
                "snap": snap, "stale": bool(pulse.get("stale")),
                "errors": pulse.get("errors") or [],
                "message": pulse.get("message"),
                "lines": microstructure.review_lines(pulse),
                "history": slim_hist, "theme_heat": heat,
                "sentiment_context": context,
                "theme_heat_meta": heat_meta,
                "weight": float(st.get("micro_weight", 0.15) or 0),
                "cap": float(st.get("micro_score_cap", 30) or 0),
                "now": util.now_iso()}

    # ---------------- 指令操作 ----------------
    def manual_fill(self, payload):
        oid = payload.get("id")
        o = self.ledger.order_by_id(oid)
        if not o:
            return {"ok": False, "message": "订单不存在"}
        if o["status"] not in ("pending", "submitted"):
            return {"ok": False, "message": "订单状态为 {}，不可执行".format(o["status"])}
        code = o["fund_code"]
        if not self.engine.pool_item(code):
            return {"ok": False,
                    "message": "基金不在当前备选池中（池为动态，可先执行“重建备选池”再试）：{}".format(code)}
        fill_date = str(payload.get("fill_date") or util.today_str())[:10]
        nav = payload.get("fill_nav")
        nav_warn = None
        if nav is None:
            # 场外基金按“成交日收盘净值”成交：优先取成交日净值；取不到再回退最新净值并提示
            try:
                seq = self.engine.market.fund_history(
                    code, need_from=util.add_days(fill_date, -8))
                hit = next((n for d, n in seq if d == fill_date), None)
            except DataError:
                hit = None
            if hit is not None:
                nav = hit
            else:
                pair = self.engine.nav_latest_map().get(code)
                if pair and pair[1]:
                    nav_date, nav = pair
                    if nav_date and nav_date != fill_date:
                        nav_warn = ("成交日 {} 的净值尚未公布，已暂按最新净值 {}（{}）估算；"
                                    "净值公布后建议核对并更正。").format(
                                        fill_date, nav, nav_date)
                else:
                    nav = None
        if not nav:
            return {"ok": False, "message": "请提供成交净值（fill_nav）或稍后再试"}
        nav = float(nav)
        fee = float(payload.get("fill_fee") or 0)
        amount = float(payload.get("fill_amount") or o["amount"] or 0)
        shares = payload.get("fill_shares")
        if shares is not None:
            shares = float(shares)
        if o["action"] == "buy":
            rate = self.engine._rate_of(code)["buy"]
            if fee <= 0 and rate > 0:
                fee = amount - amount / (1.0 + rate)
            if not shares:
                shares = ((amount - fee) / nav if (amount - fee) > 0 else 0)
            shares = float(int(shares * 100)) / 100.0
            if shares < 0.01:
                return {"ok": False, "message": "金额过小，无法形成有效份额"}
            res = self.ledger.exec_buy(code, fill_date, shares, nav, fee,
                                       budget=amount)
            if not res["ok"]:
                return {"ok": False, "message": res.get("reason") or "买入失败"}
            amount = res["debit"]  # 实际扣款（尾差已退回现金）
        else:  # sell
            if not shares:
                sellable, _ = self.ledger.sellable(code, fill_date,
                                                   self.engine.min_hold)
                shares = min(float(amount) / nav, sellable)
            sellable, _ = self.ledger.sellable(code, fill_date,
                                               self.engine.min_hold)
            shares = min(shares, sellable)
            if shares <= 0.001:
                return {"ok": False, "message": "无可卖份额或持有不足{}天".format(
                    self.engine.min_hold)}
            res = self.ledger.exec_sell(code, fill_date, shares, nav,
                                        self.engine._sell_rate_fn(code))
            amount = res["net"]
            fee = res["fee"]
            shares = res["executed"]
        self.ledger.complete_order(oid, fill_date, nav, shares, amount, fee,
                                   "人工录入成交")
        return {"ok": True, "order": self.ledger.order_by_id(oid),
                "nav_warn": nav_warn}


class Handler(BaseHTTPRequestHandler):
    app = None  # 由 make_server 注入

    # ---------------- helpers ----------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        try:
            data = body if isinstance(body, bytes) else \
                json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400):
        """返回失败 JSON。

        **状态码语义**（2026-09-11 审计后统一）：
          * 400 参数/状态不合法（默认）——例如"订单不存在""方向判断已停用"；
          * 403 只读拒绝（演示库不允许写操作）、路径穿越；
          * 404 路由/静态文件不存在；
          * 409 并发冲突（上一次任务仍在运行、系统忙）；
          * 500 **只用于真正的未预期异常**（`except Exception` 分支）。
        旧实现把所有失败都写成 500，导致"预期内的业务拒绝"与"真故障"在监控/日志里
        无法区分（前端 `api()` 只看 `ok` 字段，所以界面行为不变）。
        """
        self._json({"ok": False, "message": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass  # 关闭默认访问日志噪音

    # ---------------- GET ----------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self._serve_static("index.html", "text/html; charset=utf-8")
        elif path in ("/app.js", "/style.css", "/vendor/echarts.min.js"):
            ctype = ("application/javascript; charset=utf-8" if path.endswith(".js")
                     else "text/css; charset=utf-8")
            self._serve_static(path.lstrip("/"), ctype)
        elif path == "/api/state":
            self._state()
        elif path == "/api/history":
            self._json({"ok": True, "snapshots": self.app.ledger.get_snapshots()})
        elif path == "/api/records":
            # 界面轮询常拖全量研判正文（LLM 长文可到 MB 级）：只给最近 150 条
            self._json({"ok": True, "records": self.app.ledger.get_records(150)})
        elif path == "/api/orders":
            self._json({"ok": True, "orders": self.app.ledger.orders()})
        elif path == "/api/funds":
            funds = self.app._snapshot("funds", 60, self.app.engine.fund_status)
            self._json({"ok": True, "funds": funds})
        elif path == "/api/indices":
            try:
                items = self.app.market.indices_quote()
            except Exception:
                items = []
            self._json({"ok": True, "indices": items})
        elif path == "/api/config":
            cfg = settings.public_cfg(self.app.cfg)
            cfg["account_meta"] = self.app.ledger.account_meta()
            # 参数开关面板所需的元数据（中文名/说明/类型/范围/分组/开关清单）
            from . import configedit, param_docs
            resp = {"ok": True, "config": cfg}
            if (q.get("params") or ["1"])[0] not in ("0", "false"):
                resp["params"] = param_docs.PARAMS
                resp["groups"] = param_docs.GROUPS
                resp["toggles"] = param_docs.TOGGLES
                resp["sensitive"] = list(configedit.SENSITIVE)
                resp["restart_keys"] = list(configedit.RESTART_KEYS)
                # 当前值（读盘、每次请求现取，写入后立即一致）与代码默认值（供重置）
                live = self.app.cfg
                resp["values"] = {p["key"]: configedit._get(live, p["key"])
                                  for p in param_docs.PARAMS}
                resp["defaults"] = {p["key"]: configedit._get(settings.DEFAULT_CFG,
                                                             p["key"])
                                    for p in param_docs.PARAMS}
            resp["backups"] = ([b["name"] for b in configedit.list_backups()][:10]
                               if not self.app.demo else [])
            self._json(resp)
        elif path == "/api/backtest":
            last = util.load_json(BACKTEST_FILE)
            self._json({"ok": True, "last": last})
        elif path == "/api/news/screen":
            date_s = (q.get("date") or [None])[0] or util.today_str()
            payload = self.app.news_payload(date_s[:10])
            self._json({"ok": True, "news": payload})
        elif path == "/api/news/dirstats":
            self._json({"ok": True,
                        "stats": self.app.screen.direction_stats(400)})
        elif path == "/api/calibration":
            self._json({"ok": True, "cal": self.app.calibration_payload()})
        elif path == "/api/micro":
            payload = self.app._snapshot("micro", 300, self.app.micro_payload)
            self._json({"ok": True, "micro": payload})
        else:
            self._err("404 not found: " + path, 404)

    def _serve_static(self, name, ctype):
        if name not in ALLOWED_STATIC:
            return self._err("forbidden", 403)
        p = STATIC_DIR / name
        if not p.exists():
            # 本地未内置（如 vendor/echarts.min.js 还没执行 vendor-echarts）→ 干净 404，
            # 由前端优雅降级到 CDN/文字模式，不再返回 500
            return self._err("static missing: " + name, 404)
        self._send(200, p.read_bytes(), ctype)

    def _state(self):
        app = self.app
        try:
            cached = app._snapshot("state", 75, app.engine.state_payload)
        except DataError as e:
            cached = {"error": str(e)}
        st = dict(cached)  # 浅拷贝，避免污染共享缓存
        st["readonly_demo"] = app.demo
        _llm = app.cfg.get("llm") or {}
        st["config"] = {
            "funds": app.cfg.get("pool", []),
            "strategy": app.cfg.get("strategy", {}),
            # 与引擎实际门控一致：enabled 且 api_key 非空才算启用
            "llm_enabled": bool(_llm.get("enabled")
                                and str(_llm.get("api_key") or "").strip()),
            "llm_provider": (" · ".join(x for x in [
                analysis.llm_label(app.cfg),
                str(_llm.get("model") or "")] if x) or None),
            "llm_needs_key": bool(_llm.get("enabled")
                                  and not str(_llm.get("api_key") or "").strip()),
            "index": (app.cfg.get("market") or {}).get("index", {}),
            "fees": app.cfg.get("fees", {}),
        }
        self._json({"ok": True, "state": st})

    # ---------------- POST ----------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.app
        body = self._body()
        if path == "/api/run-daily":
            if app.demo:
                return self._err("演示库为只读，请用正式库运行每日研判（python app.py run-daily）", 403)
            if not app._busy.acquire(blocking=False):
                return self._err("上一次任务仍在运行，请稍候", 409)
            try:
                out = app.engine.run_daily(force=bool(body.get("force")))
                if out.get("status") == "error":
                    return self._err(out.get("message", "运行失败"))
                app._invalidate()
                return self._json({"ok": True, **out})
            finally:
                app._busy.release()
        elif path == "/api/pool/refresh":
            if app.demo:
                return self._err("演示库为只读，动态筛选请用正式库", 403)
            if not app._busy.acquire(blocking=False):
                return self._err("系统忙，请稍候", 409)
            try:
                res = app.engine.refresh_pool()
                app._invalidate()
                return self._json({"ok": bool(res.get("ok")), "result": res})
            except Exception as e:
                return self._err("重建备选池失败：" + str(e), 500)
            finally:
                app._busy.release()
        elif path == "/api/orders/confirm":
            oid = body.get("id")
            o = app.ledger.order_by_id(oid)
            if not o or o["status"] != "pending":
                return self._err("订单不存在或不可确认", 404)
            submit_at = str(body.get("submit_at") or util.now_iso())
            updated = app.ledger.confirm_order(oid, submit_at)
            # 计算给用户的 T+1 时间线
            V = util.nav_value_date(submit_at)
            C = util.add_trading_days(V, 1)
            app._invalidate()
            return self._json({"ok": True, "order": updated,
                               "submit_at": submit_at,
                               "nav_value_date": V, "confirm_date": C,
                               "message": "已确认：{} 提交 → {} 净值成交 → {} 份额确认（届时自动入账）".format(
                                   submit_at[:10], V, C)})
        elif path == "/api/orders/skip":
            oid = body.get("id")
            o = app.ledger.order_by_id(oid)
            if not o or o["status"] not in ("pending", "submitted"):
                return self._err("订单不存在或当前状态不可跳过/撤销", 404)
            app.ledger.skip_order(oid, "用户手动跳过/撤销")
            app._invalidate()
            return self._json({"ok": True,
                               "message": "已跳过该指令" if o["status"] == "pending"
                               else "已撤销该提交（如需已发生的真实成交，请以平台为准）"})
        elif path == "/api/orders/fill":
            if not app._busy.acquire(blocking=False):
                return self._err("系统忙", 409)
            try:
                res = app.manual_fill(body)
                app._invalidate()
                # 统一状态码（2026-09-11 审计）：失败时不再回 200+ok:false
                if res.get("ok"):
                    return self._json(res)
                code = 404 if "订单不存在" in str(res.get("message") or "") else 400
                return self._err(res.get("message") or "录入成交失败", code)
            finally:
                app._busy.release()
        elif path == "/api/positions/refresh":
            # “持仓盈亏”独立刷新：强制在线取持仓基金最新净值（绕过 75s 状态快照）
            if app.demo:
                return self._err("演示库只读，持仓为合成数据无需刷新", 403)
            if not app._busy.acquire(blocking=False):
                return self._err("正在刷新，请稍候", 409)
            try:
                res = app.engine.refresh_positions()
                app._invalidate()
                return self._json(res)
            except Exception as e:
                return self._err("持仓刷新失败：" + str(e), 500)
            finally:
                app._busy.release()
        elif path == "/api/backtest":
            if not app._busy.acquire(blocking=False):
                return self._err("上一次任务仍在运行，请稍候", 409)
            try:
                months = int(body.get("months") or 6)
                # 演示界面只回放合成数据：禁止偷偷用正式指数/净值烧配额
                source = "demo" if app.demo else str(body.get("source") or "auto")
                res = app.engine.backtest(months=months, source=source)
                res["_run_at"] = util.now_iso()
                util.save_json(BACKTEST_FILE, res)
                return self._json({"ok": True, "result": res})
            except DataError as e:
                # 数据不可用（接口失败 / 区间不足）→ 503（暂时不可用），不是参数错
                return self._err(str(e), 503)
            except Exception as e:
                return self._err("回测失败：" + str(e), 500)
            finally:
                app._busy.release()
        elif path == "/api/news/pull":
            if app.demo:
                return self._err("演示库为只读；消息筛选请用正式库（python app.py serve）", 403)
            if not app._busy.acquire(blocking=False):
                return self._err("系统忙，请稍候", 409)
            try:
                date_s = str(body.get("date") or util.today_str())[:10]
                news_obj = app.news_pull(date_s, force=True)
                payload = app.news_payload(date_s)
                if not news_obj.get("ok"):
                    payload["pull_error"] = news_obj.get("message", "抓取失败")
                    return self._json({"ok": True, "news": payload,
                                       "pull_error": payload.get("pull_error")})
                return self._json({"ok": True, "news": payload,
                                   "fetched": news_obj.get("items", 0),
                                   "auto_score": news_obj.get("score")})
            except DataError as e:
                return self._err("消息抓取失败：" + str(e), 500)
            finally:
                app._busy.release()
        elif path == "/api/micro/refresh":
            try:
                payload = app.micro_payload(force=True)
                app._invalidate("micro")
                return self._json({"ok": True, "micro": payload})
            except Exception as e:
                return self._err("微观结构重抓失败：" + str(e)[:160], 500)
        elif path == "/api/news/rate":
            if app.demo:
                return self._err("演示库只读", 403)
            date_s = str(body.get("date") or util.today_str())[:10]
            try:
                item = app.screen.rate_item(
                    date_s, str(body.get("item_id") or ""),
                    str(body.get("label") or ""),
                    body.get("strength"))
                learned_n = len(app.screen.lexicon_rows(10000))
                return self._json({"ok": True, "item": item,
                                   "learned_total": learned_n,
                                   "payload": app.news_payload(date_s)})
            except ValueError as e:
                return self._err(str(e))
        elif path == "/api/calibration/backfill":
            if app.demo:
                return self._err("演示库只读，无需历史回填", 403)
            days = int(body.get("days") or 20)
            max_pages = int(body.get("max_pages") or 700)
            return self._json(app.start_calib_backfill(days=days,
                                                       max_pages=max_pages))
        elif path == "/api/event-model/run":
            if app.demo:
                return self._err("演示库只读，无需建模", 403)
            return self._json(app.start_event_model(
                days=int(body.get("days") or 400),
                select=bool(body.get("select", True)),
                warmup=int(body.get("warmup") or 60)))
        elif path == "/api/history/extend":
            if app.demo:
                return self._err("演示库只读，无需延伸历史", 403)
            if not app._busy.acquire(blocking=False):
                return self._err("系统忙，请稍候", 409)
            try:
                years = int(body.get("years") or 8)
                res = app.market.extend_index_history(years=years)
                return self._json({"ok": True, "result": res})
            except Exception as e:
                return self._err("历史延伸失败：" + str(e), 500)
            finally:
                app._busy.release()
        elif path == "/api/config/set":
            if app.demo:
                return self._err("演示库只读，不写配置", 403)
            from . import configedit
            res = configedit.apply_changes(
                changes=body.get("set") or {},
                resets=body.get("reset") or [],
                confirm_sensitive=bool(body.get("confirm_sensitive")))
            if res.get("ok"):
                app._invalidate()          # 参数变了 → 清掉所有快照缓存
            return self._json(dict(res, backups=[
                b["name"] for b in configedit.list_backups()][:10]))
        elif path == "/api/config/rollback":
            if app.demo:
                return self._err("演示库只读，不写配置", 403)
            from . import configedit
            res = configedit.rollback(str(body.get("name") or "") or None)
            if res.get("ok"):
                app._invalidate()
            return self._json(dict(res, backups=[
                b["name"] for b in configedit.list_backups()][:10]))
        elif path == "/api/news/direction":
            # (已停用：方向判断由 AI 自动记录，不再要求用户输入)
            return self._err("方向判断已由 AI 自动记录，无需手动保存", 400)
        elif path == "/api/news/search/export":
            if app.demo:
                return self._err("演示库只读", 403)
            date_s = str(body.get("date") or util.today_str())[:10]
            n = len(app.screen.items_for(date_s))
            screening.ensure_daily_news_queue(date_s, n > 0, n,
                                              note="人工筛选页手动登记")
            return self._json({"ok": True,
                               "queue_open": screening.open_queue_items(),
                               "payload": app.news_payload(date_s)})
        elif path == "/api/news/search/import":
            if app.demo:
                return self._err("演示库只读", 403)
            m = screening.import_search_results(cfg=app.cfg)
            return self._json({"ok": True, "merge": m,
                               "payload": app.news_payload(
                                   str(body.get("date") or util.today_str())[:10])})
        else:
            self._err("404 not found: " + path, 404)


def make_server(cfg, db_path, demo=False, port=None, host=None):
    # 长驻服务规避 akshare 的 V8 依赖崩溃（akshare 1.18 import 即加载 py_mini_racer）：
    # 有智兔 token 时净值通道禁用 akshare（智兔每日 200 次 + 缓存足够）；
    # 无 token 才回退 akshare（子进程探测，崩了不伤宿主）。
    from . import datasource
    if (cfg.get("data", {}) or {}).get("zhitu_token"):
        datasource._AK_STATE.update(checked=True, ok=False)
    else:
        datasource.probe_akshare_safe()
    app = ApiApp(cfg, db_path, demo=demo)
    Handler.app = app
    host = host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(port or cfg.get("server", {}).get("port", 8787))
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv, app
