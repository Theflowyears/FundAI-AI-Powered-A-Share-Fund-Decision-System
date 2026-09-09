# -*- coding: utf-8 -*-
"""内置 Web 服务：静态界面 + JSON API（仅标准库 http.server）。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import news as newsmod
from . import screening, settings, util
from .datasource import Market
from .engine import Engine
from .ledger import Ledger
from .util import DataError

STATIC_DIR = util.PROJECT / "web" / "static"
BACKTEST_FILE = util.data_file("backtest_last.json")

ALLOWED_STATIC = {"index.html", "app.js", "style.css", "vendor/echarts.min.js"}


class ApiApp:
    """共享给所有请求的上下文（含线程安全的 SQLite 连接）。"""

    def __init__(self, cfg, db_path, demo=False):
        self.cfg = cfg
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

    def _invalidate(self):
        with self._snap_lock:
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
        items = scr.items_for(date_s)
        # 打标前体验：未打标(待你复核)的排在最前，已打标的垫底（组内保持时间倒序）
        items.sort(key=lambda r: 0 if not (r.get("user_label") or "") else 1)
        todo_n = sum(1 for r in items if not (r.get("user_label") or ""))
        feed = []
        for r in items:
            d = dict(r)
            # 前端打标按钮用 it.id（见 app.js renderNews）；SQLite 行主键叫 item_id，
            # 这里补一个 id 别名，避免前端提交 “undefined” 导致打标 500。
            d["id"] = d.get("item_id") or d.get("id") or ""
            d["funds_links"] = [{"code": c, "name": nmap.get(c, c)}
                                for c in (d.get("funds") or [])]
            feed.append(d)
        auto_net = round(sum(x.get("auto_strength") or 0 for x in items
                             if x.get("auto_label") in ("bull", "bear")), 2)
        auto_score = int(max(-100, min(100, max(-8, min(8, auto_net)) *
                                       max(1, amp))))
        labels = scr.labels_summary(date_s)
        human = scr.human_news_meta(date_s, amplitude=amp)
        learned = scr.lexicon_rows(120)
        from . import lexicon
        direction = scr.direction(date_s)
        stats = scr.direction_stats()
        qstate = util.load_json(screening.SEARCH_QUEUE_FILE, {"items": []})
        queue = qstate.get("items", [])
        return {
            "date": date_s,
            "feed": feed,
            "feed_count": len(items),
            "todo": todo_n,          # 待你打标（未打标）条数
            "done": len(items) - todo_n,
            "auto_score": auto_score, "auto_net": auto_net,
            "human": human,
            "labels": labels,
            "learned": learned,
            "learned_total": len(learned),
            "base_lexicon": {"bull": len(lexicon.BULL_STRONG) + len(lexicon.BULL_WEAK),
                             "bear": len(lexicon.BEAR_STRONG) + len(lexicon.BEAR_WEAK)},
            "direction": direction,
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

    def _err(self, msg, code=500):
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
            self._json({"ok": True, "config": cfg})
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
        else:
            self._err("404 not found: " + path, 404)

    def _serve_static(self, name, ctype):
        if name not in ALLOWED_STATIC:
            return self._err("forbidden", 403)
        p = STATIC_DIR / name
        if not p.exists():
            return self._err("static missing: " + name, 500)
        self._send(200, p.read_bytes(), ctype)

    def _state(self):
        app = self.app
        try:
            cached = app._snapshot("state", 75, app.engine.state_payload)
        except DataError as e:
            cached = {"error": str(e)}
        st = dict(cached)  # 浅拷贝，避免污染共享缓存
        st["readonly_demo"] = app.demo
        st["config"] = {
            "funds": app.cfg.get("pool", []),
            "strategy": app.cfg.get("strategy", {}),
            "llm_enabled": bool((app.cfg.get("llm") or {}).get("enabled")),
            "llm_provider": (app.cfg.get("llm") or {}).get("provider"),
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
                return self._err("演示库为只读，请用正式库运行每日研判（python app.py run-daily）")
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
                return self._err("演示库为只读，动态筛选请用正式库")
            if not app._busy.acquire(blocking=False):
                return self._err("系统忙，请稍候", 409)
            try:
                res = app.engine.refresh_pool()
                app._invalidate()
                return self._json({"ok": bool(res.get("ok")), "result": res})
            except Exception as e:
                return self._err("重建备选池失败：" + str(e))
            finally:
                app._busy.release()
        elif path == "/api/orders/confirm":
            oid = body.get("id")
            o = app.ledger.order_by_id(oid)
            if not o or o["status"] != "pending":
                return self._err("订单不存在或不可确认")
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
                return self._err("订单不存在或当前状态不可跳过/撤销")
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
                return self._json(res)
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
                return self._err(str(e))
            except Exception as e:
                return self._err("回测失败：" + str(e))
            finally:
                app._busy.release()
        elif path == "/api/news/pull":
            if app.demo:
                return self._err("演示库为只读；消息筛选请用正式库（python app.py serve）")
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
                return self._err("消息抓取失败：" + str(e))
            finally:
                app._busy.release()
        elif path == "/api/news/rate":
            if app.demo:
                return self._err("演示库只读")
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
        elif path == "/api/news/direction":
            # (已停用：方向判断由 AI 自动记录，不再要求用户输入)
            return self._err("方向判断已由 AI 自动记录，无需手动保存", 400)
        elif path == "/api/news/search/export":
            if app.demo:
                return self._err("演示库只读")
            date_s = str(body.get("date") or util.today_str())[:10]
            n = len(app.screen.items_for(date_s))
            screening.ensure_daily_news_queue(date_s, n > 0, n,
                                              note="人工筛选页手动登记")
            return self._json({"ok": True,
                               "queue_open": screening.open_queue_items(),
                               "payload": app.news_payload(date_s)})
        elif path == "/api/news/search/import":
            if app.demo:
                return self._err("演示库只读")
            m = screening.import_search_results(cfg=app.cfg)
            return self._json({"ok": True, "merge": m,
                               "payload": app.news_payload(
                                   str(body.get("date") or util.today_str())[:10])})
        else:
            self._err("404 not found: " + path, 404)


def make_server(cfg, db_path, demo=False, port=None, host=None):
    app = ApiApp(cfg, db_path, demo=demo)
    Handler.app = app
    host = host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(port or cfg.get("server", {}).get("port", 8787))
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv, app
