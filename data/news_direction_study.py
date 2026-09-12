# -*- coding: utf-8 -*-
"""消息面「净情绪」方向性实测：证明 net_score 忽略 label 符号是 BUG。

背景（2026-09-11 定位）
----------------------
`semantics.infer_news()` 返回的是 **label=方向 + strength=强度（正数）**，
而 `lexicon.score_text()` 返回的是 **带符号的 strength**。`news.net_score()`
把两者都当作有符号量直接相加：

    raw += float(e.get("auto_strength") or 0)

结果：口径内（cbank_ease / cbank_tight / holder_flow / geo_conflict）的
79,327 条消息里，`bear` 条目的 strength **全部为正**（地缘冲突、央行收紧），
净情绪因此**永远不可能为负**（3674 天实测 min = 0），利空被算成利多。

本脚本在同一份十年数据上对比两种口径：
  A) 现状（直接求和，方向盲）
  B) 修正（按 auto_label 取符号：bull=+mag，bear=−mag）
指标：与"次一交易日沪深300涨跌"的相关系数、分档命中率、以及方向非零占比。

用法：python data/news_direction_study.py
输出：控制台表格 + data/news_direction_study.json
"""
import collections
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import parallel  # noqa: E402
parallel.use_utf8_stdout()  # 已是 UTF-8 就不重包（避免包装器被 GC 关掉底层 buffer）

from fundai import calib, clsdb, news as newsmod, settings, util  # noqa: E402


def feed_of(rows):
    """DB 行（label/strength/event，见 clsdb.scores 表）→ net_score 需要的 feed 键。

    这是**键名口径**修正：scored_items() 返回 label/strength/event，
    而 net_score/net_scope 读的是 auto_label/auto_strength/event_type。
    不改键名时，任何"直接喂 DB 行"的研究都会得到恒为 0 的净情绪。
    """
    out = []
    for r in rows:
        lab = r.get("label") or ""
        if lab not in ("bull", "bear", "neutral"):
            continue
        out.append({"auto_label": lab,
                    "auto_strength": int(r.get("strength") or 0),
                    "event_type": r.get("event") or "",
                    "important": int(r.get("important") or 0),
                    "title": r.get("title") or ""})
    return out


def net_legacy(feed, mode="scaled", scale=70.0, focus=None, dict_mode="off",
               include_other=False):
    """口径 B：修复前的线上实现——直接求和、不看 label（方向盲）。"""
    raw, n_dir = 0.0, 0
    for e in feed:
        if e.get("auto_label") not in ("bull", "bear"):
            continue
        if focus is not None and not newsmod.net_scope(e, focus, dict_mode,
                                                       include_other):
            continue
        n_dir += 1
        raw += float(e.get("auto_strength") or 0)
    if mode == "sum" or not scale:
        return max(-8.0, min(8.0, raw)), raw, n_dir
    return max(-8.0, min(8.0, raw / float(scale))), raw, n_dir


def net_signed(feed, mode="scaled", scale=70.0, focus=None, dict_mode="off",
               include_other=False):
    """口径 C：按 label 取符号后求和（等价于修复后的 news.net_score）。"""
    raw, n_dir = 0.0, 0
    for e in feed:
        lab = e.get("auto_label")
        if lab not in ("bull", "bear"):
            continue
        if focus is not None and not newsmod.net_scope(e, focus, dict_mode,
                                                       include_other):
            continue
        n_dir += 1
        raw += newsmod.signed_strength(lab, e.get("auto_strength"))
    if mode == "sum" or not scale:
        return max(-8.0, min(8.0, raw)), raw, n_dir
    return max(-8.0, min(8.0, raw / float(scale))), raw, n_dir


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs) ** 0.5
    vy = sum((b - my) ** 2 for b in ys) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    return pearson(rank(xs), rank(ys))


def main():
    cfg = settings.load_config()
    st = cfg.get("strategy") or {}
    amp = int(st.get("news_amp") or 8)
    scale = float(st.get("news_net_scale") or 70.0)
    focus = list(st.get("news_focus_events") or newsmod.DEFAULT_FOCUS_EVENTS)
    dict_mode = str(st.get("news_dict_mode") or "off")
    include_other = bool(st.get("news_include_other_events", False))

    print("口径：focus={} dict_mode={} scale={} amp={}".format(
        focus, dict_mode, scale, amp))
    rows = clsdb.scored_items()
    per_day = collections.defaultdict(list)
    for r in rows:
        ct = r.get("ctime")
        if ct:
            d = datetime.fromtimestamp(int(ct), util.TZ_CN).strftime("%Y-%m-%d")
            per_day[d].append(r)
    print("DB 已打分条目 %d 条，覆盖 %d 天" % (len(rows), len(per_day)))

    closes, dates = calib.load_closes(refresh=False)
    idx = {d: closes[d] for d in dates}

    recs = []
    zero_keys_old = 0
    for i, d in enumerate(dates[:-1]):
        raw_rows = per_day.get(d)
        if not raw_rows:
            continue                      # 无消息的交易日不进样本
        nxt = closes[dates[i + 1]] / closes[d] - 1.0
        feed_keys = feed_of(raw_rows)
        # 现状口径：把 DB 行直接交给 net_score（键名不匹配 → 全 0）
        net_naive, _, _ = newsmod.net_score(raw_rows, mode="scaled", scale=scale,
                                            focus=focus, dict_mode=dict_mode)
        if net_naive == 0:
            zero_keys_old += 1
        # 修复前的线上口径：键名修好但仍不取符号（方向盲）
        net_a, _, n_a = net_legacy(feed_keys, mode="scaled", scale=scale,
                                   focus=focus, dict_mode=dict_mode)
        # 现行口径：news.net_score（已按 label 取符号）；net_signed 为等价显式计算
        net_b, _, n_b = net_signed(feed_keys, mode="scaled", scale=scale,
                                   focus=focus, dict_mode=dict_mode)
        net_live, _, _ = newsmod.net_score(feed_keys, mode="scaled", scale=scale,
                                           focus=focus, dict_mode=dict_mode)
        assert net_live == net_b, (d, net_live, net_b)
        recs.append({"date": d, "next_ret": nxt, "net_naive": net_naive,
                     "net_raw": net_a, "net_fixed": net_b,
                     "n_dir_raw": n_a, "n_dir_fixed": n_b})
    print("样本交易日（有消息且有次日收益）：%d" % len(recs))
    print("直接喂 DB 行（键名不匹配）净情绪为 0 的天数：%d" % zero_keys_old)

    def block(name, key):
        xs = [r[key] for r in recs]
        ys = [r["next_ret"] for r in recs]
        neg = sum(1 for x in xs if x < 0)
        nz = sum(1 for x in xs if x != 0)
        out = {"pearson": pearson(xs, ys), "spearman": spearman(xs, ys),
               "neg_days": neg, "nonzero_days": nz, "days": len(xs)}
        # 分档命中率：按净情绪排序取首尾 20%
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        k = max(1, int(len(order) * 0.2))
        low = [ys[i] for i in order[:k]]
        high = [ys[i] for i in order[-k:]]
        out["low_mean_ret"] = sum(low) / len(low)
        out["high_mean_ret"] = sum(high) / len(high)
        out["low_up_rate"] = sum(1 for r in low if r > 0) / len(low)
        out["high_up_rate"] = sum(1 for r in high if r > 0) / len(high)
        out["all_up_rate"] = sum(1 for r in ys if r > 0) / len(ys)
        print("\n[%s]" % name)
        print("  非零天数 %d/%d（其中负值 %d 天）" % (nz, out["days"], neg))
        print("  Pearson %.4f | Spearman %.4f" % (out["pearson"], out["spearman"]))
        print("  净情绪最低 20%% 的次日：均值 %+.3f%%  上涨率 %.1f%%"
              % (out["low_mean_ret"] * 100, out["low_up_rate"] * 100))
        print("  净情绪最高 20%% 的次日：均值 %+.3f%%  上涨率 %.1f%%"
              % (out["high_mean_ret"] * 100, out["high_up_rate"] * 100))
        print("  全样本上涨率 %.1f%%" % (out["all_up_rate"] * 100))
        return out

    print("\n=== 三种口径对比（同一份十年数据）===")
    res = {"days": len(recs), "focus": focus, "scale": scale, "amp": amp,
           "zero_naive_days": zero_keys_old}
    res["A_naive_keys"] = block("A 现状：DB 行直接喂 net_score（键名不匹配 → 恒 0）",
                                "net_naive")
    res["B_legacy_blind"] = block("B 修复前：直接求和（方向盲，利空算利多）", "net_raw")
    res["C_label_signed"] = block("C 修复后：按 auto_label 取符号（现行 news.net_score）",
                                  "net_fixed")

    a, b = res["B_legacy_blind"], res["C_label_signed"]
    print("\n=== 结论 ===")
    print("修复前口径净情绪非零但**负值天数 = %d**（结构性只能看多）" % a["neg_days"])
    print("修复后负值天数 = %d，Spearman 由 %.4f → %.4f"
          % (b["neg_days"], a["spearman"], b["spearman"]))
    print("分档差（最高档 − 最低档 次日均值）：修复前 %+.3f%% vs 修复后 %+.3f%%"
          % ((a["high_mean_ret"] - a["low_mean_ret"]) * 100,
             (b["high_mean_ret"] - b["low_mean_ret"]) * 100))

    util.save_json(str(util.data_file("news_direction_study.json")), res)
    print("\n已存档 data/news_direction_study.json")


if __name__ == "__main__":
    main()
