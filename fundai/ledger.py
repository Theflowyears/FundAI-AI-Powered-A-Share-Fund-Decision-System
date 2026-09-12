# -*- coding: utf-8 -*-
"""账本：SQLite 记录现金、持仓份额（按买入批次 FIFO）、订单、每日研判与资产快照。

设计要点
--------
- 份额按“批次”(lot) 记录：赎回按先进先出，按每笔持有天数计算赎回费；
  持有不足 min_hold_days(默认7天) 的份额不可赎回（惩罚性 1.5% 费率）。
- 现金 = 可用于申购的余额；买入扣款、卖出到账都即时入账（简化：赎回资金
  默认 T+1 可用，与实际场外基金大体一致）。
- 所有写操作加线程锁，供 HTTP 服务并发使用。
"""
import json
import sqlite3
import threading
from pathlib import Path

from . import util


class Ledger:
    def __init__(self, db_path, initial_cash=0.0):
        self.lock = threading.RLock()
        if str(db_path) != ":memory:":
            Path(str(db_path)).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._schema()
        with self.lock:
            if self.meta("cash") is None:
                self.set_meta("cash", str(round(float(initial_cash), 2)))
            if self.meta("fees") is None:
                self.set_meta("fees", "0")

    # ---------------- schema ----------------
    def _schema(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta(
                k TEXT PRIMARY KEY, v TEXT
            );
            CREATE TABLE IF NOT EXISTS lots(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fund_code TEXT NOT NULL,
                buy_date TEXT NOT NULL,
                shares REAL NOT NULL,
                nav REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_date TEXT NOT NULL,
                fund_code TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('buy','sell')),
                amount REAL NOT NULL DEFAULT 0,
                note TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created TEXT NOT NULL,
                submit_at TEXT,
                fill_date TEXT,
                fill_nav REAL,
                fill_shares REAL,
                fill_amount REAL,
                fill_fee REAL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS records(
                date TEXT PRIMARY KEY,
                market_score INTEGER,
                market_chg REAL,
                index_close REAL,
                view_title TEXT,
                source TEXT,
                analysis TEXT,
                decision TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots(
                date TEXT PRIMARY KEY,
                cash REAL,
                mv_eq REAL,
                mv_bond REAL,
                total REAL,
                fees REAL,
                index_close REAL,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS executed(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                date TEXT NOT NULL,
                shares REAL NOT NULL DEFAULT 0,
                nav REAL NOT NULL DEFAULT 0,
                fee REAL NOT NULL DEFAULT 0,
                cash_delta REAL NOT NULL DEFAULT 0,
                note TEXT DEFAULT ''
            );
            """)
        # 兼容旧库：为 orders 补 submit_at 列
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(orders)")]
        if "submit_at" not in cols:
            self.conn.execute("ALTER TABLE orders ADD COLUMN submit_at TEXT")
        # 兼容旧库：为 records 补 calc 列（当日评分计算明细 JSON，透明化用）
        rcols = [r["name"] for r in self.conn.execute("PRAGMA table_info(records)")]
        if "calc" not in rcols:
            self.conn.execute(
                "ALTER TABLE records ADD COLUMN calc TEXT DEFAULT ''")

    def close(self):
        self.conn.close()

    # ---------------- meta ----------------
    def meta(self, k):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k, v):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO meta(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def set_account_meta(self, account):
        self.set_meta("account", json.dumps(account, ensure_ascii=False))

    def account_meta(self):
        raw = self.meta("account")
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def cash(self):
        return float(self.meta("cash") or 0)

    def fees(self):
        return float(self.meta("fees") or 0)

    def add_cash(self, delta):
        with self.lock:
            new = max(0.0, util.r2(self.cash() + float(delta)))
            self.set_meta("cash", str(new))
        return new

    def add_fee(self, delta):
        with self.lock:
            new = util.r2(self.fees() + float(delta))
            self.set_meta("fees", str(new))

    # ---------------- 持仓批次 ----------------
    def add_lot(self, code, buy_date, shares, nav, fee):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO lots(fund_code,buy_date,shares,nav,fee) "
                    "VALUES(?,?,?,?,?)",
                    (code, buy_date, round(float(shares), 4),
                     round(float(nav), 6), round(float(fee), 2)))

    def positions(self):
        """{code: 剩余份额}"""
        rows = self.conn.execute(
            "SELECT fund_code, SUM(shares) s FROM lots WHERE shares>0 "
            "GROUP BY fund_code").fetchall()
        return {r["fund_code"]: round(r["s"], 4) for r in rows}

    def position_shares(self, code):
        return self.positions().get(code, 0.0)

    def basis(self, code):
        """持仓成本（含买入费）与份额 -> (cost, shares)。用于浮盈/浮亏计算。"""
        rows = self.conn.execute(
            "SELECT shares, nav, fee FROM lots WHERE fund_code=? AND shares>0",
            (code,)).fetchall()
        shares = sum(r["shares"] for r in rows)
        cost = sum(r["shares"] * r["nav"] + r["fee"] for r in rows)
        return cost, shares

    def score_ema(self):
        """平滑评分（用于仓位控制，跨日持久化）。"""
        try:
            v = self.meta("score_ema")
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def set_score_ema(self, v):
        self.set_meta("score_ema", str(round(float(v), 4)))

    def risk_lock_until(self):
        """组合止损/峰值回撤后的防御锁截止日（含），None 表示未锁定。"""
        v = self.meta("risk_lock_until")
        return v if v else None

    def set_risk_lock(self, until_date):
        self.set_meta("risk_lock_until", str(until_date)[:10])

    def clear_risk_lock(self):
        self.set_meta("risk_lock_until", "")

    def peak_total(self):
        try:
            return float(self.meta("peak_total") or 0)
        except (TypeError, ValueError):
            return 0.0

    def bump_peak(self, total):
        cur = self.peak_total()
        if total > cur:
            self.set_meta("peak_total", str(round(float(total), 2)))
            return float(total)
        return cur

    def sellable(self, code, asof, min_hold_days):
        """返回 (可卖份额, 锁定份额)。按批次持有天数判断。"""
        rows = self.conn.execute(
            "SELECT id,buy_date,shares FROM lots WHERE fund_code=? AND shares>0 "
            "ORDER BY buy_date,id", (code,)).fetchall()
        sellable = locked = 0.0
        for r in rows:
            days = (util.parse_d(asof) - util.parse_d(r["buy_date"])).days
            if days >= min_hold_days:
                sellable += r["shares"]
            else:
                locked += r["shares"]
        return round(sellable, 4), round(locked, 4)

    def exec_buy(self, code, buy_date, shares, nav, fee, budget=None):
        """买入成交：新增批次并按“实际占用 = 份额×净值 + 费”扣减现金。

        - 只扣 shares*nav+fee，计划金额与实际的尾差退回现金（不再静默蒸发）；
        - 余额不足时不产生任何批次/扣款副作用，返回 {"ok": False, "reason": ...}；
        - 每笔写入 executed 流水，供 check_consistency() 断言资金守恒。
        返回 {"ok": True, "debit", "fee"} 或 {"ok": False, "reason"}。
        """
        shares = round(float(shares), 2)
        fee = util.r2(float(fee or 0))
        if shares <= 0:
            return {"ok": False, "reason": "份额<=0，无法成交"}
        nav6 = round(float(nav), 6)
        debit = util.r2(shares * nav6 + fee)
        if budget is not None:
            # 尾差保护：实际占用不得超过计划金额；差额留在现金里
            debit = min(debit, util.r2(float(budget)))
        with self.lock:
            cur_cash = self.cash()
            if cur_cash + 1e-6 < debit:
                return {"ok": False,
                        "reason": "现金不足：本笔需 {:.2f} 元，可用 {:.2f} 元".format(
                            debit, cur_cash)}
            self._ensure_seed()
            with self.conn:
                self.conn.execute(
                    "INSERT INTO lots(fund_code,buy_date,shares,nav,fee) "
                    "VALUES(?,?,?,?,?)",
                    (code, buy_date, round(shares, 4), nav6, fee))
            if debit > 0:
                self.add_cash(-debit)
            if fee > 0:
                self.add_fee(fee)
            self._log_exec(code, "buy", buy_date, shares, nav6, fee, -debit,
                           "exec_buy")
        return {"ok": True, "debit": debit, "fee": fee, "shares": shares}

    def _ensure_seed(self):
        """资金守恒基线：老库首次启用时以当前现金为基线（此后成交流水可核对）。"""
        v = self.meta("seed_cash")
        if v is None:
            self.set_meta("seed_cash", str(util.r2(self.cash())))
        return float(self.meta("seed_cash") or 0)

    def _log_exec(self, code, action, date_s, shares, nav, fee, cash_delta,
                  note=""):
        with self.conn:
            self.conn.execute(
                "INSERT INTO executed(code,action,date,shares,nav,fee,"
                "cash_delta,note) VALUES(?,?,?,?,?,?,?,?)",
                (code, action, str(date_s)[:10], round(float(shares), 4),
                 round(float(nav), 6), util.r2(float(fee or 0)),
                 util.r2(float(cash_delta)), str(note or "")[:200]))

    def exec_sell(self, code, sell_date, shares_to_sell, nav, rate_fn):
        """卖出成交：按 FIFO 消费批次，逐批按持有天数计赎回费。

        rate_fn(days) -> 费率。返回 {'executed','gross','fee','net'}。
        """
        shares_to_sell = float(shares_to_sell)
        rows = self.conn.execute(
            "SELECT id,buy_date,shares FROM lots WHERE fund_code=? AND shares>0 "
            "ORDER BY buy_date,id", (code,)).fetchall()
        executed = gross = fee = 0.0
        with self.lock:
            for r in rows:
                if shares_to_sell <= 1e-9:
                    break
                take = min(r["shares"], shares_to_sell)
                days = (util.parse_d(sell_date) - util.parse_d(r["buy_date"])).days
                rate = rate_fn(days)
                g = take * float(nav)
                gross += g
                fee += g * rate
                executed += take
                shares_to_sell -= take
                with self.conn:
                    self.conn.execute(
                        "UPDATE lots SET shares=shares-? WHERE id=?",
                        (round(take, 4), r["id"]))
            net = gross - fee
            if net > 0:
                self.add_cash(net)
            if fee > 0:
                self.add_fee(fee)
            if executed > 0:
                self._log_exec(code, "sell", sell_date, executed,
                               round(float(nav), 6), util.r2(fee),
                               util.r2(net), "exec_sell")
        return {"executed": round(executed, 4), "gross": util.r2(gross),
                "fee": util.r2(fee), "net": util.r2(net)}

    def check_consistency(self, tol=0.02, raise_on_mismatch=True):
        """资金守恒断言：seed_cash + Σ成交流水现金变动 == 当前现金。

        另检查批次份额不为负。老库在首次调用时自动建立基线（此前历史不追溯）。
        """
        with self.lock:
            seed = self._ensure_seed()
            row = self.conn.execute(
                "SELECT COALESCE(SUM(cash_delta),0) s, COUNT(*) n "
                "FROM executed").fetchone()
            delta, n = float(row["s"]), int(row["n"])
            rec = util.r2(seed + delta)
            cur = self.cash()
            diff = abs(rec - cur)
            neg = self.conn.execute(
                "SELECT fund_code, MIN(shares) m FROM lots GROUP BY fund_code "
                "HAVING MIN(shares) < -0.0001").fetchall()
            problems = []
            if diff > tol:
                problems.append(
                    "资金偏差 {:.4f} 元（现金 {} vs 基线 {}+流水 {}={}，共 {} 笔成交）".format(
                        diff, cur, seed, delta, rec, n))
            for r in neg:
                problems.append("批次份额为负：{} {}".format(
                    r["fund_code"], r["m"]))
            ok = not problems
            if not ok and raise_on_mismatch:
                raise RuntimeError("账本一致性检查失败：" + "；".join(problems))
            return {"ok": ok, "seed": seed, "cash": cur,
                    "reconstructed": rec, "diff": round(diff, 4),
                    "executed": n, "problems": problems}

    # ---------------- 订单 ----------------
    def add_order(self, order_date, code, action, amount, note=""):
        with self.lock:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO orders(order_date,fund_code,action,amount,"
                    "note,status,created) VALUES(?,?,?,?,?,?,?)",
                    (order_date, code, action, round(float(amount), 2), note,
                     "pending", util.now_iso()))
                return cur.lastrowid

    def pending_orders(self):
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status='pending' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def confirm_order(self, oid, submit_at):
        """用户确认已在支付宝/天天基金提交 → 订单进入“已提交·待T+1确认”状态。"""
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE orders SET status='submitted', submit_at=? WHERE id=? AND status='pending'",
                    (str(submit_at), oid))
        return self.order_by_id(oid)

    def submitted_orders(self):
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status='submitted' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def order_by_id(self, oid):
        r = self.conn.execute(
            "SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None

    def complete_order(self, oid, fill_date, fill_nav, fill_shares,
                       fill_amount, fill_fee, detail=None):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE orders SET status='filled', fill_date=?, "
                    "fill_nav=?, fill_shares=?, fill_amount=?, fill_fee=?, "
                    "detail=? WHERE id=?",
                    (fill_date, round(float(fill_nav or 0), 6),
                     round(float(fill_shares or 0), 4),
                     util.r2(fill_amount or 0), util.r2(fill_fee or 0),
                     detail, oid))

    def skip_order(self, oid, detail=""):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "UPDATE orders SET status='skipped', detail=? WHERE id=?",
                    (detail, oid))

    def orders(self, limit=500):
        rows = self.conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 研判记录 ----------------
    def add_record(self, date_s, score, chg, index_close, view_title, source,
                   analysis, decision, calc=""):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO records(date,market_score,"
                    "market_chg,index_close,view_title,source,analysis,decision,"
                    "calc) VALUES(?,?,?,?,?,?,?,?,?)",
                    (date_s, int(score), (None if chg is None else util.r4(chg)),
                     util.r2(index_close), view_title, source, analysis,
                     decision, str(calc or "")))

    def has_record(self, date_s):
        return self.conn.execute(
            "SELECT 1 FROM records WHERE date=?", (date_s,)).fetchone() is not None

    def get_record(self, date_s):
        """取某日研判记录（dict 或 None）。供“AI 次日方向”重放/补跑时复用当日口径。"""
        r = self.conn.execute(
            "SELECT * FROM records WHERE date=?", (str(date_s)[:10],)).fetchone()
        return dict(r) if r else None

    def get_records(self, limit=400):
        rows = self.conn.execute(
            "SELECT * FROM records ORDER BY date DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 快照 ----------------
    def add_snapshot(self, date_s, cash, mv_eq, mv_bond, total, fees,
                     index_close, note=""):
        with self.lock:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO snapshots(date,cash,mv_eq,mv_bond,"
                    "total,fees,index_close,note) VALUES(?,?,?,?,?,?,?,?)",
                    (date_s, util.r2(cash), util.r2(mv_eq), util.r2(mv_bond),
                     util.r2(total), util.r2(fees),
                     (None if index_close is None else util.r2(index_close)),
                     note))

    def get_snapshots(self):
        rows = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY date").fetchall()
        return [dict(r) for r in rows]

    def latest_snapshot(self):
        r = self.conn.execute(
            "SELECT * FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # ---------------- 历史重建（资产曲线自愈回填用） ----------------
    def seed_cash(self):
        """资金守恒基线现金（老库启用流水前的基线）。"""
        try:
            return float(self.meta("seed_cash") or self.cash())
        except (TypeError, ValueError):
            return self.cash()

    def cashflow_rows(self):
        """全部成交现金流水 [(code, action, date, shares, cash_delta, fee)]。"""
        return [tuple(r) for r in self.conn.execute(
            "SELECT code, action, date, shares, cash_delta, fee FROM executed "
            "ORDER BY date, id").fetchall()]

    def lot_rows(self):
        """现存份额批次 [(fund_code, buy_date, shares)]（shares 为当前剩余）。"""
        return [tuple(r) for r in self.conn.execute(
            "SELECT fund_code, buy_date, shares FROM lots "
            "WHERE shares>0 ORDER BY buy_date, id").fetchall()]

    def has_snapshot(self, date_s):
        return self.conn.execute(
            "SELECT 1 FROM snapshots WHERE date=?", (date_s,)).fetchone() is not None

    # ---------------- 重置 ----------------
    def reset(self, initial_cash=0.0):
        with self.lock:
            with self.conn:
                self.conn.execute("DELETE FROM lots")
                self.conn.execute("DELETE FROM orders")
                self.conn.execute("DELETE FROM records")
                self.conn.execute("DELETE FROM snapshots")
                self.conn.execute("DELETE FROM executed")
            self.set_meta("cash", str(round(float(initial_cash), 2)))
            self.set_meta("seed_cash", str(round(float(initial_cash), 2)))
            self.set_meta("fees", "0")
            self.set_meta("peak_total", "0")
            self.set_meta("score_ema", "")
            self.set_meta("risk_lock_until", "")
