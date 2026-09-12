# -*- coding: utf-8 -*-
"""大盘研判：量化评分 + 中文文案 +（可选）LLM 深度研判。"""
import json

from . import settings, util

VIEWS = [
    # (阈值下限, key, 标题, 含义)
    (55,  "hot_bull", "强烈看多", "进攻仓位拉满"),
    (25,  "bull",     "谨慎看多", "偏积极，可加仓"),
    (-25, "neutral",  "中性震荡", "半仓均衡，高抛低吸"),
    (-55, "bear",     "谨慎看空", "防御为主，控制仓位"),
    (-101,"hot_bear", "强烈看空", "空仓避险，等待企稳"),
]


def view_of(score):
    for lo, key, title, desc in VIEWS:
        if score >= lo:
            return {"key": key, "title": title, "desc": desc}
    return VIEWS[-1][1:]


def score_market(st):
    """基于技术指标的综合评分 -> (score, signals[])。

    score: -100（极空）~ +100（极多）。signals: 逐条依据。
    """
    s = 0.0
    sigs = []
    c = st.get("close")
    if not c:
        return 0, [{"name": "无数据", "impact": 0, "note": "数据不足，默认中性"}]

    def sig(name, impact, note):
        sigs.append({"name": name, "impact": round(impact), "note": note})

    ma5, ma20, ma60 = st.get("ma5"), st.get("ma20"), st.get("ma60")
    if ma60:
        if c > ma60:
            s += 12
            sig("60日线", +12, "收盘价站上60日线，中期趋势偏多")
        else:
            s -= 12
            sig("60日线", -12, "收盘价位于60日线下方，中期趋势偏弱")
        if ma20 and ma20 > ma60:
            s += 10
            sig("均线多头", +10, "20日线位于60日线上方，均线结构转强")
        elif ma20:
            s -= 10
            sig("均线空头", -10, "20日线位于60日线下方，均线结构偏弱")
    if ma5 and ma20:
        if ma5 > ma20:
            s += 8
            sig("短线强势", +8, "5日线在20日线上方，短线动能向上")
        else:
            s -= 8
            sig("短线弱势", -8, "5日线在20日线下方，短线动能向下")
    if ma20:
        dev = c / ma20 - 1.0
        s += util.clamp(dev * 120.0, -12, 12)
        sig("乖离率", int(util.clamp(dev * 120.0, -12, 12)),
            "价格偏离20日线 {:.1%}".format(dev))
    r = st.get("rsi14")
    if r is not None:
        if r >= 70:
            s -= 8
            sig("RSI超买", -8, "RSI={:.0f}，短线过热，注意回调".format(r))
        elif r <= 30:
            s += 6
            sig("RSI超卖", +6, "RSI={:.0f}，超卖区或有技术性反弹".format(r))
        else:
            sig("RSI", 0, "RSI={:.0f}，处于中性区".format(r))
    m5 = st.get("mom5")
    if m5 is not None:
        impact = int(util.clamp(m5 * 300, -6, 6))
        s += impact
        sig("5日动量", impact, "近5日涨幅 {:.2%}".format(m5))
    m20 = st.get("mom20")
    if m20 is not None:
        impact = int(util.clamp(m20 * 80, -8, 8))
        s += impact
        sig("20日动量", impact, "近20日涨幅 {:.2%}".format(m20))
    vr = st.get("vol_ratio")
    chg = st.get("chg_pct")
    if vr is not None and chg is not None:
        if chg > 0 and vr >= 1.2:
            s += 5
            sig("量价配合", +5, "放量上涨（量比 {:.2f}），多方占优".format(vr))
        elif chg < 0 and vr >= 1.3:
            s -= 6
            sig("放量下跌", -6, "放量下杀（量比 {:.2f}），注意风险".format(vr))
        elif chg > 0 and vr <= 0.8:
            s -= 4
            sig("缩量上涨", -4, "缩量反弹（量比 {:.2f}），持续性存疑".format(vr))
        else:
            sig("量能", 0, "量能正常（量比 {:.2f}）".format(vr))
    return int(util.clamp(s, -100, 100)), sigs


def signal_confidence(signals, extra_view=None):
    """多信号共识度（借鉴 ai-hedge-fund 的多观点投票模型）。

    signals: score_market 产出的逐条信号 [{impact: -100..100, ...}]；
    extra_view: 独立“一票”（-1~+1，如消息面/板块广度），None 则不计。
    返回 0~1：越高表示各信号方向越一致（共识强）；越低表示分歧大。
    """
    bull = sum(max(0, s.get("impact", 0)) for s in (signals or []))
    bear = sum(max(0, -s.get("impact", 0)) for s in (signals or []))
    if extra_view is not None:
        v = util.clamp(float(extra_view), -1, 1)
        if v > 0:
            bull += v * 60.0
        elif v < 0:
            bear += -v * 60.0
    total = bull + bear
    if total <= 0:
        return 0.5  # 无有效信号 → 中性共识
    return round(abs(bull - bear) / total, 3)


def build_report(date_s, idx_name, close, chg, st, score, sigs,
                 source="内置量化引擎", extra_lines=None):
    """生成一段有“基金经理风格”的中文研判。"""
    v = view_of(score)
    chg_txt = ("{:.2f}%".format(chg * 100) if chg is not None else "—")
    lines = []
    lines.append("【盘面】{}收于 {:.2f} 点，当日{}。".format(idx_name, close, chg_txt))
    ma_parts = []
    for k, lab in (("ma5", "MA5"), ("ma20", "MA20"), ("ma60", "MA60")):
        val = st.get(k)
        ma_parts.append("{} {:.0f}".format(lab, val) if val else "{} —".format(lab))
    lines.append("【均线】{}；RSI14 ≈ {:.0f}；近5日动量 {:.2%}；量比 {:.2f}。".format(
        "  ".join(ma_parts),
        st.get("rsi14") or 0,
        st.get("mom5") or 0.0,
        st.get("vol_ratio") or 0.0))
    if sigs:
        strong = [x for x in sigs if abs(x.get("impact", 0)) >= 5]
        if strong:
            lines.append("【关键信号】" + "；".join(
                "{}{:+d}".format(x["name"], x["impact"]) for x in strong))
    lines.append("【AI研判】综合评分 {:+.0f}，观点：{}（{}）。".format(score, v["title"], v["desc"]))
    lines.append("【策略含义】评分映射为权益基金目标仓位：当前观点下 " +
                 _trend_desc(score) + "。")
    if extra_lines:
        lines.extend(extra_lines)
    lines.append("（来源：{}；自动生成，不构成投资建议）".format(source))
    return "\n".join(lines)


def _trend_desc(score):
    if score >= 55:
        return "维持高权益仓位（约 78%~92%），顺势持有为主"
    if score >= 25:
        return "权益仓位上调至 65%~78% 区间，逢回调分批买入"
    if score >= -25:
        return "半仓均衡（约 40%~60%），债基托底、留足机动现金"
    if score >= -55:
        return "权益仓位降至 25%~40% 防御区间，以债基/现金为主"
    return "权益仓位压至 25% 以下轻仓避险，等待评分回到 -25 上方再行动"


# ---------------- 可选 LLM 研判 ----------------
_MODEL_FAMILY = (("qwen", "Qwen"), ("deepseek", "DeepSeek"), ("glm", "GLM"),
                 ("kimi", "Kimi"), ("moonshot", "Moonshot"), ("ernie", "ERNIE"),
                 ("doubao", "Doubao"), ("hunyuan", "Hunyuan"),
                 ("llama", "LLaMA"), ("gpt", "GPT"))


def llm_label(cfg):
    """LLM 来源展示名（记录来源 = “LLM·<此名>”）。

    优先用 config 的 llm.provider；但若它仍是模板默认的 deepseek（或为空）、
    而 model 明显属于其他家族（如 qwen3.8-flash），按真实模型家族显示——
    避免“用着百炼 Qwen、记录却标 deepseek”的误导。
    """
    llm = (cfg or {}).get("llm") or {}
    provider = str(llm.get("provider") or "").strip()
    model = str(llm.get("model") or "").strip().lower()
    fam = next((label for key, label in _MODEL_FAMILY if key in model), None)
    if not provider or provider.lower() == "deepseek":
        return fam or "DeepSeek"
    return provider


def dynamic_weights(cfg, news_net=None, micro_pct=None, vol_rank=None):
    """按市场动态（消息净情绪 + 情绪分位 + 波动分位）微调三路权重。

    依据（README 5.1「动态权重」/ 5.2「消息面口径治理」，全部来自 10 年数据的实测）：
    - 消息面正边际薄（+1~2pp），只有在**净情绪极端**（|net|≥4，事件驱动日）时信息量更大；
    - 情绪分与次日涨跌 IC ≈ −0.09（轻微反向），处在**分位两端**时更不可靠；
    - 波动率极高（≥0.9 分位）时两路噪声都放大。
    因此：只在有信息时**小幅**倾斜（±0.03~0.05），并严格限幅，权重可复现、可回退。
    返回 (w_news, w_micro, note)。
    """
    st = (cfg or {}).get("strategy", {}) or {}

    def _f(key, dft):
        """读数值配置：**只有键缺失/为 None 才用默认值**。

        修 BUG（2026-09-11）：旧写法 `st.get("news_weight", 0.35) or 0.35` 会把
        **显式配置的 0 当成"没配"**替换成默认权重 —— 于是"阉割消息面/情绪"
        （把权重设为 0）的实验实际上仍在按 0.35/0.10 计分，两档结果一模一样
        （`data/regime_backtest.py` 的 news_off 与 news_035 曾给出逐位相同的结果）。
        """
        v = st.get(key)
        return float(dft) if v is None else float(v)

    wn0 = _f("news_weight", 0.35)
    wm0 = _f("micro_weight", 0.10)
    if not st.get("dynamic_weights", True):
        return wn0, wm0, "静态权重"
    wn, wm, notes = wn0, wm0, []
    step = _f("dynamic_step", 0.03)
    if news_net is not None and abs(float(news_net)) >= 4.0:
        wn += step
        notes.append("消息净情绪 {:.1f}（事件驱动日）→ 消息权重 +{:.0%}".format(
            float(news_net), step))
    if micro_pct is not None:
        p = float(micro_pct)
        if p <= 0.2 or p >= 0.8:
            wm -= step
            notes.append("情绪分位 {:.0%}（{}）→ 情绪权重 −{:.0%}".format(
                p, "偏冷" if p <= 0.2 else "偏热", step))
    if vol_rank is not None and float(vol_rank) >= 0.9:
        wn -= step
        wm -= step
        notes.append("波动率 {:.0%} 分位（极端）→ 消息/情绪各 −{:.0%}".format(
            float(vol_rank), step))
    lo_n, hi_n = float(st.get("news_weight_min", 0.25) or 0.25), \
        float(st.get("news_weight_max", 0.40) or 0.40)
    lo_m, hi_m = float(st.get("micro_weight_min", 0.05) or 0.05), \
        float(st.get("micro_weight_max", 0.15) or 0.15)
    wn = util.clamp(wn, lo_n, hi_n)
    wm = util.clamp(wm, lo_m, hi_m)
    if wn + wm > 0.6:
        wm = max(lo_m, 0.6 - wn)
    return round(wn, 4), round(wm, 4), "；".join(notes) or "无倾斜（静态）"


def llm_analyze(cfg, pack):
    """调用 OpenAI 兼容接口（默认 DeepSeek）生成研判。

    返回 dict {"score","title","text","provider"}；任何失败返回 None，
    由上层回退到内置引擎。
    """
    llm = cfg.get("llm") or {}
    if not llm.get("enabled") or not llm.get("api_key"):
        return None
    base = (llm.get("base_url") or "https://api.deepseek.com").rstrip("/")
    provider = llm_label(cfg)
    # 提示词里的本金/目标**不能写死**：本金可自定义、目标 = 本金 × 倍数（默认 1.3）
    _initial, _target, _mult = settings.account_plan(cfg)
    _pct = (_mult - 1.0) * 100
    system = ("你是一位严守纪律的量化基金经理，受托用 {:.0f} 元人民币通过场外普通基金"
              "（只能买基金、绝不直接买股票）在半年内尝试做到 {:.0f} 元"
              "（较本金 +{:.0f}%）。"
              "你每天收盘后对次日A股走势给出研判。要求：冷静、客观、给出可执行仓位结论；"
              "只输出JSON。").format(_initial, _target, _pct)
    # 注意：下面这段提示词**含 JSON 字面花括号**（{"score": ...}），绝不能对它用
    # `str.format()`——花括号会被当成格式字段，实测直接 KeyError: '"score"'。
    # 用 % 格式化（只有一个数字参数）即可，同时保留花括号原样。
    user = ("【今日数据包】\n" + json.dumps(pack, ensure_ascii=False) +
            "\n\n请输出 JSON：{\"score\": 整数 -100~100, \"title\": 不超过12字的观点短句, "
            "\"text\": 400字左右的中文研判，包含：盘面解读/技术信号/明日走势推演/"
            "对 %.0f 元账户的仓位安排}。只输出JSON本身。" % _initial)
    try:
        js = util.http_post_json(
            base + "/chat/completions",
            {
                "model": llm.get("model", "deepseek-chat"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": float(llm.get("temperature", 0.4)),
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": "Bearer " + llm.get("api_key", "")},
            timeout=90)
        content = (js["choices"][0]["message"]["content"] or "").strip()
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].lstrip()
        obj = json.loads(content)
        score = int(util.clamp(int(obj.get("score", 0)), -100, 100))
        title = str(obj.get("title", ""))[:20] or "AI研判"
        text = str(obj.get("text", "")).strip()
        if len(text) < 60:
            return None
        return {"score": score, "title": title, "text": text,
                "provider": provider}
    except Exception:
        return None
