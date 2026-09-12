# -*- coding: utf-8 -*-
r"""参数说明元数据：给 Web「参数开关面板」提供 key → 中文名/说明/类型/范围/分组。

用途
----
前端（`web/static/*`）不直接解析 `config.json` 的语义，而是消费本模块：

    from fundai import param_docs
    param_docs.PARAMS      # 每个可调参数一条 metadata（含 default = config.json 实际值）
    param_docs.GROUPS      # 面板分组显示顺序（中文）
    param_docs.TOGGLES     # 只含 kind == "bool" 的 key，按重要性排序（前端做一排开关）
    param_docs.by_key()    # key -> param dict
    param_docs.coverage()  # 覆盖率报告：missing / extra / excluded / total

纪律（新增参数时必须遵守）
--------------------------
1. **desc 必须来自代码**：写说明前先 grep 读取点，例如

       grep -n "news_weight" fundai/*.py

   确认「这个键在哪个模块被读、读出来怎么用、边界/默认值是什么」，再写一句话说明
   「控制什么 / 调大调小会怎样 / 什么时候该改」。**禁止凭参数名猜语义**（本项目的
   默认值在 settings.DEFAULT_CFG 与 config.json 里并不总是一致，猜必错）。
2. **找不到任何读取点就是 dead**：此时必须 dead=True，并在 desc 末尾附证据，
   格式为「已废弃，未在代码中读取（grep -n "<key>" fundai/*.py 无命中）」。
   只被前端 web/static/app.js 读取的键不算 dead，但要在 desc 里写明口径。
3. **新增键**：在 PARAMS 里追加一条 dict，字段一个都不能少（无内容填 None/空）；
   group 必须已存在于 GROUPS，再用 order 排组内次序（同组内不要重复 order）。
4. **不在 config.json 里的键**（代码里有默认值、可手工加进配置）也要收录，
   default 用 None 表示「配置里没有」，代码默认值写在 desc 里；
   coverage() 会把它归入 extra，这是预期行为。
5. **明确不收录的键**（展示用文本、内部状态、敏感值、以及确定不会被读取的死键）
   必须登记在 EXCLUDED 里并写明原因，否则 coverage()["missing"] 会报出来。

维护提示：本文件只读 config.json，不写任何配置；纯新增文件，不修改其他模块。
"""
import json

from . import settings

# 分组显示顺序（前端按此顺序渲染分栏）
GROUPS = [
    "仓位与上限",
    "加减仓与轮动",
    "消息面与动态权重",
    "市场情绪微观",
    "风控止损与冷却",
    "预测型风险过滤器",
    "选基与动态筛选",
    "数据源",
    "LLM 与凭证",
    "运行与服务",
]

# ---------------------------------------------------------------------------
# PARAMS：每个可调参数一条 metadata
#   key / label / desc / kind / options / min / max / step / unit /
#   group / order / advanced / default / effect / dead  —— 字段全部必需
#   default 一律为 None，运行时由 _defaults_from_config() 用 config.json 实际值覆盖
# ---------------------------------------------------------------------------
PARAMS = [
    # ================= 仓位与上限 =================
    {
        "key": "strategy.equity_base_mode", "label": "仓位基准模式",
        "desc": "决定权益目标仓位怎么算：fixed=固定用 equity_base_fixed；score=eq_base+eq_slope×市场评分（夹在 eq_floor~eq_cap）。代码注释写明十年组合试错结论是「评分驱动不如固定适度仓位」，所以生产默认 fixed。只有在想重新试验「评分驱动仓位」时才改成 score，改后 eq_base/eq_slope/eq_floor/eq_cap 才会生效。",
        "kind": "enum",
        "options": [{"value": "fixed", "label": "固定仓位（默认）"},
                    {"value": "score", "label": "评分驱动"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "仓位与上限", "order": 10, "advanced": False,
        "default": None,
        "effect": "切到 score 后仓位随每日评分上下浮动，波动与择时风险都变大；fixed 下仓位恒定、更可控。",
        "dead": False},
    {
        "key": "strategy.equity_base_fixed", "label": "固定权益基准",
        "desc": "equity_base_mode=fixed 时的权益目标仓位（代码里再夹到 0~1）。默认 0.70 = 七成仓。调大→仓位更满、收益弹性与回撤同时放大；调小→更保守但牛市涨得少。注意它还会被 equity_hard_cap（0.80）与风控路径二次压制。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "仓位与上限", "order": 20, "advanced": False,
        "default": None,
        "effect": "调高会让仓位更满、回撤更大；超过 equity_hard_cap 的部分会被硬上限吃掉。",
        "dead": False},
    {
        "key": "strategy.eq_base", "label": "评分模式基准",
        "desc": "equity_base_mode=score 时仓位公式的常数项（仓位=eq_base+eq_slope×评分）。评级中性（评分 0）时就持这个比例。fixed 模式下完全不生效（代码在 fixed 分支直接 return）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "仓位与上限", "order": 30, "advanced": True,
        "default": None,
        "effect": "只在 score 模式下生效：调高整体仓位水平，调低整体更防守。",
        "dead": False},
    {
        "key": "strategy.eq_slope", "label": "评分斜率",
        "desc": "score 模式下每 1 分市场评分换取多少权益仓位（默认 0.005 → 评分 ±100 对应 ±50pp）。settings 校验禁止负值。fixed 模式下不生效。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.02, "step": 0.001, "unit": "比例/分",
        "group": "仓位与上限", "order": 40, "advanced": True,
        "default": None,
        "effect": "调大→评分的高低会更剧烈地放大/收缩仓位，择时更激进。",
        "dead": False},
    {
        "key": "strategy.eq_floor", "label": "仓位下限",
        "desc": "score 模式下权益仓位的下夹逼值（不能大于 eq_cap，settings 会在配置写反时直接报错）。fixed 模式下不生效。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "仓位与上限", "order": 50, "advanced": True,
        "default": None,
        "effect": "调高会抬高最悲观时的最低仓位，极端行情下也留有底仓暴露。",
        "dead": False},
    {
        "key": "strategy.eq_cap", "label": "仓位上夹逼",
        "desc": "score 模式下权益仓位的上夹逼值（默认 0.95）。它只管评分公式的结果，最终仍会被 equity_hard_cap 再压一次。fixed 模式下不生效。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "仓位与上限", "order": 60, "advanced": True,
        "default": None,
        "effect": "调高允许评分极高时更满仓；超过 equity_hard_cap 的部分无效。",
        "dead": False},
    {
        "key": "strategy.equity_hard_cap", "label": "权益硬上限",
        "desc": "全局权益仓位天花板：所有路径（固定/评分/调整带/风险过滤/牛市覆盖）算完后都会再 min 一次它，防御冷却与组合风控则压得更低。默认 0.80 = 永不满仓。调高→仓位上限更松，调低→系统性更保守。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "仓位与上限", "order": 70, "advanced": False,
        "default": None,
        "effect": "这是最后一道仓位闸门：调高会同时抬高所有加仓路径的上限，回撤风险随之上升。",
        "dead": False},
    {
        "key": "strategy.position_band_max", "label": "动态调整带",
        "desc": "由「制度状态＋情绪位置」驱动的仓位加减小带，上限 ±该值。≤0 直接关闭（返回 0 并记「调整带已关闭」），当前生产默认 0（关闭）。开启后会按 均线多头 +50%带宽、低波动 +30%、情绪偏热 +20%、极端波动 −60%、情绪偏冷 −20% 组合。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.30, "step": 0.05, "unit": "比例",
        "group": "仓位与上限", "order": 80, "advanced": False,
        "default": None,
        "effect": "开启并调大→仓位会随制度/情绪摆动，温和上行时更满（并放宽轮动门槛）、极端波动时更空。",
        "dead": False},
    {
        "key": "strategy.band_vol_low", "label": "低波动分位线",
        "desc": "position_band_max>0 时，大盘波动率分位低于此值视为「低波动」，调整带 +30% 带宽。只在调整带开启后有实际作用。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "仓位与上限", "order": 90, "advanced": True,
        "default": None,
        "effect": "调高→更多交易日被判定为低波动而加仓；调低→此加分项更难触发。",
        "dead": False},
    {
        "key": "strategy.band_vol_extreme", "label": "极端波动分位线",
        "desc": "波动率分位 ≥ 此值视为「极端波动」，调整带 −60% 带宽（是带内最大的一次减项）。只在调整带开启后有实际作用。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "仓位与上限", "order": 100, "advanced": True,
        "default": None,
        "effect": "调低→更频繁触发减仓带，极端行情下仓位收得更快、也更容易误伤。",
        "dead": False},
    {
        "key": "strategy.band_micro_hot", "label": "情绪偏热分位线",
        "desc": "微观情绪分位 > 此值视为「偏热」，调整带 +20% 带宽（在低波动/均线多头之外再加一点）。只在调整带开启后有实际作用。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "仓位与上限", "order": 110, "advanced": True,
        "default": None,
        "effect": "调低→更多日子被判为偏热而加仓，追高风险上升。",
        "dead": False},
    {
        "key": "strategy.band_micro_cold", "label": "情绪偏冷分位线",
        "desc": "微观情绪分位 < 此值视为「偏冷」，调整带 −20% 带宽。只在调整带开启后有实际作用。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "仓位与上限", "order": 120, "advanced": True,
        "default": None,
        "effect": "调高→更多日子被判为偏冷而减仓，可能错过冰点反转。",
        "dead": False},

    # ================= 加减仓与轮动 =================
    {
        "key": "strategy.regime_step", "label": "调仓触发步长",
        "desc": "目标仓位与当前仓位之差 ≥ 此值才真正加减仓；差得不够就只观察不动（代码里 diff>=regime 加仓、diff<=-regime 减仓）。调大→调仓更迟钝、交易更少；调小→更频繁地微调仓位。",
        "kind": "number",
        "options": None, "min": 0.01, "max": 0.5, "step": 0.01, "unit": "比例",
        "group": "加减仓与轮动", "order": 10, "advanced": False,
        "default": None,
        "effect": "调小会增加下单频次与摩擦成本（场外基金 T+1），调大会让仓位长期偏离目标。",
        "dead": False},
    {
        "key": "strategy.max_bond_weight", "label": "债基仓位上限",
        "desc": "债基底仓的市值占比上限（相对总资产）。现金超过 bond_buy_floor 且债基还有容量时才买债基；达到上限则提示「暂不追加」。settings 校验要求 (0,1]。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "加减仓与轮动", "order": 20, "advanced": False,
        "default": None,
        "effect": "调高→闲置现金更多沉淀在债基（收益略增、但赎回 T+1 会拖慢再进攻）；调低→现金留得更多。",
        "dead": False},
    {
        "key": "strategy.bond_buy_floor", "label": "现金保留下限",
        "desc": "现金占比较此值高出的部分才会转入债基生息，同时保证至少留下这么多现金做机动。settings 校验要求 0 ≤ 该值 ≤ max_bond_weight。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "加减仓与轮动", "order": 30, "advanced": False,
        "default": None,
        "effect": "调高→留在现金的钱更多、买债更少（更灵活但少赚利息）；调低→更多现金变债基。",
        "dead": False},
    {
        "key": "strategy.min_hold_days", "label": "最短持有天数",
        "desc": "持有不足该天数的份额不可赎回（引擎按它给 ledger.sellable 判定，避开 1.5% 短期赎回费）；风控清仓、减仓、轮动遇到锁定期都会提示「解锁后再执行」。同时决定卖出费率切换点（<该值按 lt7d 费率）。",
        "kind": "int",
        "options": None, "min": 0, "max": 30, "step": 1, "unit": "天",
        "group": "加减仓与轮动", "order": 40, "advanced": False,
        "default": None,
        "effect": "调大→锁定期更长、止损/减仓会被延迟到解锁后；调小→能更快卖出（若赎回费规则不匹配会多吃惩罚费）。",
        "dead": False},
    {
        "key": "strategy.min_order_yuan", "label": "最小下单金额",
        "desc": "单笔买卖金额下限：代码取「该值」与「该基金 min_buy」的较大者，低于起购点的单子会被跳过并记一条提示。默认 10 元（与场外基金起购点一致）。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 1000.0, "step": 1.0, "unit": "元",
        "group": "加减仓与轮动", "order": 50, "advanced": False,
        "default": None,
        "effect": "调高→小额调仓被丢弃，组合更不容易偏离目标；调低→可能生成平台无法受理的碎单。",
        "dead": False},
    {
        "key": "strategy.use_fund_convert", "label": "基金转换一步走",
        "desc": "true：赎回与申购同日配对下单，交给平台『转换/超级转换』一次完成，客户资金无空窗期（止损赎回的资金也视为当日可用）；false：退回「先赎回→等资金到账→下一交易日再买」的两步走。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "加减仓与轮动", "order": 60, "advanced": False,
        "default": None,
        "effect": "关闭会显著拖慢换基/加仓速度（可能踏空），但不需要平台支持转换功能。",
        "dead": False},
    {
        "key": "strategy.rotate_gap", "label": "换基动量门槛",
        "desc": "轮动防抖：持仓动量比组合内最弱标的还低超过该值才换基，否则提示「动量接近组合最弱，暂不换仓」。温和上行（调整带 >0）时代码会把门槛最多收窄一半，让轮动更灵活。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.30, "step": 0.01, "unit": "比例",
        "group": "加减仓与轮动", "order": 70, "advanced": False,
        "default": None,
        "effect": "调小→更容易换基、交易更频繁、摩擦成本更高；调大→更钝、可能长期拿着落后板块。",
        "dead": False},
    {
        "key": "strategy.rotate_winner_gain", "label": "锁定期放行盈利线",
        "desc": "持仓浮盈 ≥ 此值（且市场处温和上行，即调整带 >0）时，允许在 7 天锁定期内做「转换」换基——这是持仓级规则，不是账户级。亏损持仓在锁定期内仍然只能等解锁。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.5, "step": 0.01, "unit": "比例",
        "group": "加减仓与轮动", "order": 80, "advanced": True,
        "default": None,
        "effect": "调低→更多锁定期持仓能被换掉（更灵活，但可能提前卖掉赢家）；调高→几乎只有大赚的持仓才放行。",
        "dead": False},
    {
        "key": "strategy.momentum_weight", "label": "动量加权混合",
        "desc": "把加仓金额切给多只备选时，权重 = 该值×动量占比 + (1−该值)×等权。代码把它夹到 0~1；全零动量时自动退化为等权。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "加减仓与轮动", "order": 90, "advanced": True,
        "default": None,
        "effect": "调高→钱更集中在动量最强的标的上（收益弹性与集中度风险同时上升）；调低→更接近等权分散。",
        "dead": False},
    {
        "key": "strategy.min_momentum", "label": "绝对动量门槛",
        "desc": "相对动量关闭（relative_momentum=false）时使用：备选基金 20 日动量低于此值就不进入候选（默认 0 = 只买正动量，避免追跌）。打开相对动量后该门槛被大盘动量取代。",
        "kind": "number",
        "options": None, "min": -0.5, "max": 0.5, "step": 0.01, "unit": "比例",
        "group": "加减仓与轮动", "order": 100, "advanced": False,
        "default": None,
        "effect": "调高→只买更强势的板块（可能空仓等机会）；调低甚至为负→会买下跌中的板块，接刀风险上升。",
        "dead": False},
    {
        "key": "strategy.relative_momentum", "label": "相对动量门槛",
        "desc": "true：用沪深300 的 20 日动量做门槛，只配置「跑赢大盘」的板块（门槛取值优先于 min_momentum）；false：改用绝对门槛 min_momentum。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "加减仓与轮动", "order": 110, "advanced": False,
        "default": None,
        "effect": "开启→普涨行情里买相对更强的板块，普跌时可能几乎没有可选标的（空仓）。",
        "dead": False},
    {
        "key": "strategy.risk_adjusted", "label": "动量除以波动",
        "desc": "仅在 factor_weights 为空（走纯动量排序分支）时生效：true 用 动量/波动率 排序，偏好「单位风险收益高」的标的；false 直接按动量排序。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "加减仓与轮动", "order": 120, "advanced": True,
        "default": None,
        "effect": "开启→选基更偏稳健、牛市爆发力略降；注意 factor_weights 非空时它被忽略。",
        "dead": False},
    {
        "key": "strategy.factor_weights", "label": "多因子权重表",
        "desc": "非空的 dict 时启用多因子截面打分排序（否则走纯动量）；各因子先做截面百分位，再按权重线性合成。子键：mom=20日动量、mom5=近5日加速、vol=低波动（权重越大越偏好低波动）、bias=相对20日线乖离（趋势确认）、heat=RSI>75 的追高惩罚、theme_heat=短线题材热度（依赖当日涨停概念热度表，拿不到热度时为 +0）。",
        "kind": "map",
        "options": [{"value": "mom", "label": "20日动量"},
                    {"value": "mom5", "label": "近5日加速"},
                    {"value": "vol", "label": "低波动偏好"},
                    {"value": "bias", "label": "20日线乖离"},
                    {"value": "heat", "label": "追高惩罚"},
                    {"value": "theme_heat", "label": "题材热度"}],
        "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 130, "advanced": True,
        "default": None,
        "effect": "权重之和不必为 1（只影响相对次序）；把 heat 调高会明显回避高位题材，theme_heat 调高则更追当日最强风口。",
        "dead": False},
    {
        "key": "strategy.factor_weights.mom", "label": "· 20日动量",
        "desc": "多因子中的 20 日动量权重（截面百分位，正向）。调高→更强者恒强；设为 0 则该因子不参与打分。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 140, "advanced": True,
        "default": None,
        "effect": "调高会强化动量风格，追涨与回撤风险同步上升。",
        "dead": False},
    {
        "key": "strategy.factor_weights.mom5", "label": "· 近5日加速",
        "desc": "近 5 日涨幅权重（截面百分位，正向），用来捕捉短期加速的板块。调高→更偏短线强势股/板块。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 150, "advanced": True,
        "default": None,
        "effect": "调高会提高短周期换手与噪声敏感度，容易在反弹尾部追进去。",
        "dead": False},
    {
        "key": "strategy.factor_weights.vol", "label": "· 低波动偏好",
        "desc": "20 日波动率权重，代码对波动率做「反向」百分位（低波动得高分）。调高→同等动量下更偏好平稳标的。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 160, "advanced": True,
        "default": None,
        "effect": "调高→组合更稳、爆发力下降；调低→选中的多是高弹性板块。",
        "dead": False},
    {
        "key": "strategy.factor_weights.bias", "label": "· 20日线乖离",
        "desc": "相对 20 日均线的乖离率权重（正向，趋势确认）。调高→更偏好已站稳均线、趋势明确的标的。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 170, "advanced": True,
        "default": None,
        "effect": "调高→顺势成分更强，但在高位乖离极大时也可能追高。",
        "dead": False},
    {
        "key": "strategy.factor_weights.heat", "label": "· 追高惩罚",
        "desc": "RSI>75 时的扣分力度（最多扣 heat×1.0）。调高→过热标的被明显压制。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 180, "advanced": True,
        "default": None,
        "effect": "调高→避开高位过热板块（可能错过最强主升段）；为 0 时不做追高惩罚。",
        "dead": False},
    {
        "key": "strategy.factor_weights.theme_heat", "label": "· 题材热度",
        "desc": "短线题材热度权重（同花顺涨停概念 + 最强风口，近 3 日时间衰减，加到该主题得分上）。热度表缺失（回测/演示）时代码恒为 +0，口径与旧版逐位一致。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重",
        "group": "加减仓与轮动", "order": 190, "advanced": True,
        "default": None,
        "effect": "调高→更追当日最强风口（实盘更活跃、回测口径不变）；调 0 则完全不看题材热度。",
        "dead": False},
    {
        "key": "strategy.score_ema_enabled", "label": "评分平滑",
        "desc": "开启后用指数移动平均平滑每日综合评分（引擎把昨日 EMA 与今日评分按 alpha 混合后写回账户 meta），目的是减少评分抖动带来的仓位噪声。默认关闭。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "加减仓与轮动", "order": 200, "advanced": True,
        "default": None,
        "effect": "开启→评分与仓位变化更平滑、调仓更少，但对突变的反应变慢（信号被稀释）。",
        "dead": False},
    {
        "key": "strategy.score_ema_alpha", "label": "平滑系数",
        "desc": "评分平滑的新值权重，代码夹到 0.05~1.0。alpha 越大越贴近当日评分（1.0 = 不平滑）；越小越钝。只在 score_ema_enabled=true 时生效。",
        "kind": "number",
        "options": None, "min": 0.05, "max": 1.0, "step": 0.05, "unit": "系数",
        "group": "加减仓与轮动", "order": 210, "advanced": True,
        "default": None,
        "effect": "调低→评分更稳但更滞后；调高→接近不平滑。",
        "dead": False},
    {
        "key": "strategy.mom_window", "label": "动量回看天数",
        "desc": "已废弃（就引擎而言），未在 fundai/*.py 中读取：选基/筛选/板块动量的 20 日窗口在 engine.py 里是硬编码的（closes[-22] 计算 20 日动量）。唯一读取点是前端 web/static/app.js 的说明表格（用 st.mom_window || 20 显示文案）。改它不会改变任何选基或筛选行为。",
        "kind": "int",
        "options": None, "min": 5, "max": 120, "step": 1, "unit": "天",
        "group": "加减仓与轮动", "order": 220, "advanced": True,
        "default": None,
        "effect": "无实际影响：仅改变网页说明页显示的天数文本；要真正改窗口必须改代码。",
        "dead": False},

    # ================= 消息面与动态权重 =================
    {
        "key": "strategy.news_weight", "label": "消息面权重",
        "desc": "综合评分里消息分的权重：最终分 = 量化分×(1−w−wm) + 消息分×w + 情绪分×wm（engine.combine_score）。消息分先按 news_score_cap 限幅。若开启 dynamic_weights，每天会在 news_weight_min~max 之间小幅浮动，此处只是基准值。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.6, "step": 0.05, "unit": "权重",
        "group": "消息面与动态权重", "order": 10, "advanced": False,
        "default": None,
        "effect": "调高→评分更受当日新闻驱动（事件日更敏感，也更容易被噪声带偏）；调低→更依赖量化技术分。",
        "dead": False},
    {
        "key": "strategy.micro_weight", "label": "情绪分权重",
        "desc": "综合评分里微观情绪分（涨停/炸板/晋级率等）的权重，引擎会把它夹到 0~0.30；micro_score=None（关闭或数据缺失）时口径自动退回旧的二方加权。开启 dynamic_weights 时随 micro_weight_min~max 浮动。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.30, "step": 0.01, "unit": "权重",
        "group": "市场情绪微观", "order": 10, "advanced": False,
        "default": None,
        "effect": "调高→评分更跟随短线情绪（追涨杀跌更明显）；调 0 等于停用情绪分。",
        "dead": False},
    {
        "key": "strategy.micro_score_cap", "label": "情绪分限幅",
        "desc": "微观情绪分进入合成前的绝对值上限（默认 ±30）；≤0 表示不限幅。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 100.0, "step": 5.0, "unit": "分",
        "group": "市场情绪微观", "order": 20, "advanced": True,
        "default": None,
        "effect": "调高→极端情绪日能更大程度撬动综合评分与仓位；调低→情绪面影响被压平。",
        "dead": False},
    {
        "key": "strategy.micro_enable", "label": "情绪面总开关",
        "desc": "关闭后快照抓取、历史回放、网页情绪卡片全部直接返回「已关闭」，情绪分不参与综合评分。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "市场情绪微观", "order": 30, "advanced": False,
        "default": None,
        "effect": "关闭→评分只由量化技术分与消息分构成，盘面情绪不再影响仓位。",
        "dead": False},
    {
        "key": "strategy.micro_hist_days", "label": "情绪图天数",
        "desc": "网页「市场情绪」卡片回放的情绪脉冲天数（web.py 传给 history_pulses，默认 7）。只影响展示与近 N 日走势图的计算范围，不参与评分。",
        "kind": "int",
        "options": None, "min": 1, "max": 60, "step": 1, "unit": "天",
        "group": "市场情绪微观", "order": 40, "advanced": False,
        "default": None,
        "effect": "调大→情绪走势图更长、每次打开网页要多算几天历史（无交易影响）。",
        "dead": False},
    {
        "key": "strategy.micro_context_days", "label": "情绪上下文天数",
        "desc": "计算情绪分位（micro_pct，用于动态权重倾斜与动态调整带）时回看的交易日天数（默认 30）。分位越低说明当前情绪在近期越冷。",
        "kind": "int",
        "options": None, "min": 5, "max": 120, "step": 5, "unit": "天",
        "group": "市场情绪微观", "order": 50, "advanced": True,
        "default": None,
        "effect": "调大→分位更平滑迟钝；调小→对近期情绪变化更敏感（更容易触发动态权重/仓位带调整）。",
        "dead": False},
    {
        "key": "strategy.micro_cache_days", "label": "情绪快照保留天数",
        "desc": "情绪快照的保留窗口，代码取 max(30, 该值)（默认 260 个交易日）。同时决定网页提示语里「保留最近 N 个交易日」的文案。",
        "kind": "int",
        "options": None, "min": 30, "max": 1000, "step": 10, "unit": "天",
        "group": "市场情绪微观", "order": 60, "advanced": True,
        "default": None,
        "effect": "调小→历史快照被裁掉、更省磁盘但丢历史统计；调到 30 以下无效（代码强制 ≥30）。",
        "dead": False},
    {
        "key": "strategy.news_amp", "label": "消息分放大倍数",
        "desc": "把当日净情绪（限幅在 ±8）放大成消息分：消息分 = clamp(净情绪 × news_amp, ±100)。默认 8 → 净情绪打满时消息分约 ±64。调大→同样的新闻情绪能给出更极端的消息分，再经 news_score_cap 限幅后仍可能顶格。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 30.0, "step": 1.0, "unit": "倍",
        "group": "消息面与动态权重", "order": 15, "advanced": False,
        "default": None,
        "effect": "调高会让消息面在评分里更强势（事件日仓位摆动更大）；调低到 1~2 则消息分几乎无足轻重。",
        "dead": False},
    {
        "key": "strategy.news_score_cap", "label": "消息分限幅",
        "desc": "消息分进入综合评分前的绝对值上限（默认 ±25）：词典/人工打分最远可到 ±100，不限幅时会用几十分的消息面单方向撬动 11pp+ 的权益仓位。注意该键目前不在 config.json 里，代码用默认 25。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 100.0, "step": 5.0, "unit": "分",
        "group": "消息面与动态权重", "order": 20, "advanced": True,
        "default": None,
        "effect": "调高→极端消息日仓位被撬动得更狠；设为 0 表示不限幅（不建议）。",
        "dead": False},
    {
        "key": "strategy.dynamic_weights", "label": "动态权重微调",
        "desc": "开关：按当日消息净情绪、情绪分位、波动分位在基准权重上做小幅倾斜——|净情绪|≥4 时消息权重 +step；情绪分位 ≤0.2 或 ≥0.8 时情绪权重 −step；波动分位 ≥0.9 时两者各 −step；全部结果被 min/max 夹住，且 w_news+w_micro>0.6 时压回。关闭则用静态权重。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 30, "advanced": False,
        "default": None,
        "effect": "开启→权重随市场状态小幅摆动（更有信息量也更难复现）；关闭→权重恒定、行为可预测。",
        "dead": False},
    {
        "key": "strategy.dynamic_step", "label": "动态微调步长",
        "desc": "动态权重的单次调整幅度（默认 0.03），一天内最多叠加几次（消息 +、情绪 −、波动 −）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.20, "step": 0.01, "unit": "权重",
        "group": "消息面与动态权重", "order": 40, "advanced": True,
        "default": None,
        "effect": "调大→权重摆动更剧烈（极端日更容易被单一信号主导）；调 0 等价于静态权重。",
        "dead": False},
    {
        "key": "strategy.news_weight_min", "label": "消息权重下限",
        "desc": "动态微调后消息权重的下夹逼值（默认 0.25）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "权重",
        "group": "消息面与动态权重", "order": 50, "advanced": True,
        "default": None,
        "effect": "调高→消息面永远占有一定话语权，消息噪声无法被完全压低。",
        "dead": False},
    {
        "key": "strategy.news_weight_max", "label": "消息权重上限",
        "desc": "动态微调后消息权重的上夹逼值（默认 0.40）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "权重",
        "group": "消息面与动态权重", "order": 60, "advanced": True,
        "default": None,
        "effect": "调高→事件驱动日消息面能占更大权重（更敏感也更容易被单一新闻带偏）。",
        "dead": False},
    {
        "key": "strategy.micro_weight_min", "label": "情绪权重下限",
        "desc": "动态微调后情绪权重的下夹逼值（默认 0.05）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.30, "step": 0.01, "unit": "权重",
        "group": "市场情绪微观", "order": 70, "advanced": True,
        "default": None,
        "effect": "调高→情绪分始终保留一定权重，无法被完全降噪。",
        "dead": False},
    {
        "key": "strategy.micro_weight_max", "label": "情绪权重上限",
        "desc": "动态微调后情绪权重的上夹逼值（默认 0.15）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.30, "step": 0.01, "unit": "权重",
        "group": "市场情绪微观", "order": 80, "advanced": True,
        "default": None,
        "effect": "调高→情绪分在评分中占比更大，追涨杀跌倾向增强。",
        "dead": False},
    {
        "key": "strategy.news_net_mode", "label": "净情绪口径",
        "desc": "当日消息净情绪的换算方式：scaled=原始强度和 ÷ news_net_scale 再夹到 ±8（修掉旧口径天天顶格导致消息分恒定无区分度的问题）；sum=旧口径，直接把强度和夹到 ±8。",
        "kind": "enum",
        "options": [{"value": "scaled", "label": "刻度换算（默认，推荐）"},
                    {"value": "sum", "label": "旧口径求和"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 70, "advanced": True,
        "default": None,
        "effect": "改回 sum 会让消息分几乎每天顶格、失去区分度（并且与历史回测口径不一致）。",
        "dead": False},
    {
        "key": "strategy.news_net_scale", "label": "净情绪刻度",
        "desc": "scaled 口径下的除数（默认 70）：净情绪 = 在口径内的强度和 ÷ 该值，再夹到 ±8。调大→同样消息量得到的净情绪更小、消息分更钝。",
        "kind": "number",
        "options": None, "min": 5.0, "max": 500.0, "step": 5.0, "unit": "分/单位",
        "group": "消息面与动态权重", "order": 80, "advanced": True,
        "default": None,
        "effect": "调小→净情绪更容易打满 ±8、消息分变得极端；调大→消息面趋于温和。",
        "dead": False},
    {
        "key": "strategy.human_net_scale", "label": "人工评分刻度",
        "desc": "人工打标消息（消息与进化页）算净情绪时单独使用的刻度，默认 10：人工分本身是 ±8 量级，所以刻度比自动口径小得多。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 100.0, "step": 1.0, "unit": "分/单位",
        "group": "消息面与动态权重", "order": 90, "advanced": True,
        "default": None,
        "effect": "调小→人工复核过的消息会更强烈地影响当日消息分。",
        "dead": False},
    {
        "key": "strategy.news_focus_events", "label": "重点事件口径",
        "desc": "计入当日净情绪的事件类型白名单（news.net_scope）：不在白名单、又不是 LLM 逐条定级的条目一律不计入。这四类是 352 个交易日实测里仍有边际的事件类型；词典兜底（event_type=dict）默认被排除。选项来自 semantics.classify_event 的真实 event 名。",
        "kind": "list",
        "options": [{"value": "cbank_ease", "label": "央行宽松"},
                    {"value": "cbank_tight", "label": "央行收紧"},
                    {"value": "market_policy", "label": "市场政策"},
                    {"value": "industry_policy", "label": "产业政策"},
                    {"value": "eco_data", "label": "经济数据"},
                    {"value": "earnings", "label": "业绩"},
                    {"value": "holder_flow", "label": "股东增减持"},
                    {"value": "geo_conflict", "label": "地缘冲突"},
                    {"value": "fund_premium_warning", "label": "产品风险提示"},
                    {"value": "holdings_disclosure", "label": "权益披露"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 100, "advanced": False,
        "default": None,
        "effect": "加进更多事件类型→更多消息参与打分（覆盖更广但稀释效应/噪声上升）；删空→消息分基本不再变化（除非开 LLM 参与）。",
        "dead": False},
    {
        "key": "strategy.news_dict_mode", "label": "词典兜底口径",
        "desc": "关键词词典兜底（event_type=dict）是否计入净情绪：off=完全不计（默认，实测词典层日级 IC 为 −0.070，去掉后转正）；important_only=只计🔴重要电报；full=全部计入。",
        "kind": "enum",
        "options": [{"value": "off", "label": "不计入（默认，推荐）"},
                    {"value": "important_only", "label": "只计重要电报"},
                    {"value": "full", "label": "全部计入"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 110, "advanced": False,
        "default": None,
        "effect": "改回 full 会让被实测判定为负边际的词典层重新影响仓位（同一批新闻的得分会明显变化）。",
        "dead": False},
    {
        "key": "strategy.news_include_other_events", "label": "计入其他事件",
        "desc": "把 focus 以外、且不是 dict 的事件类型也计入净情绪（net_scope 的最后兜底分支）。默认 false，即只信重点事件 + LLM 定级。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 120, "advanced": True,
        "default": None,
        "effect": "开启→净情绪的样本来源骤然变多，消息分波动与噪声同时上升。",
        "dead": False},
    {
        "key": "strategy.news_llm_enable", "label": "LLM 逐条定级",
        "desc": "用大模型对「词典兜底桶」的消息逐条判定利好/利空/中性（只处理这部分、带磁盘缓存、失败回退词典标签），判为中性/无关的消息会被一票否决（llm_dropped）。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 130, "advanced": False,
        "default": None,
        "effect": "关闭→降噪能力下降、词典兜底噪声（或按 news_dict_mode 完全排除）；开启会消耗 LLM 调用配额。",
        "dead": False},
    {
        "key": "strategy.news_llm_cap", "label": "LLM 每日上限",
        "desc": "每天最多送去逐条定级的消息条数（默认 120；消息更少时以实际条数为准，llm_news 内部还有单批最大 30 条的约束）。",
        "kind": "int",
        "options": None, "min": 0, "max": 1000, "step": 10, "unit": "条",
        "group": "消息面与动态权重", "order": 140, "advanced": True,
        "default": None,
        "effect": "调大→更多消息被 AI 定级（更准但 token 成本/耗时上升）；调小到 0 → LLM 定级实际上不处理任何消息。",
        "dead": False},
    {
        "key": "strategy.news_llm_contribute", "label": "LLM 影响净情绪",
        "desc": "默认 false：LLM 只做降噪（否决不相关消息），其方向标签不计入净情绪；设为 true 后 LLM 定级过的消息会按其方向参与净情绪（apply_labels 的 contribute 参数）。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 150, "advanced": True,
        "default": None,
        "effect": "开启→消息分几乎完全由模型口径决定（与现有实证口径不一致，慎开）。",
        "dead": False},
    {
        "key": "strategy.news_llm_batch", "label": "LLM 单批条数",
        "desc": "LLM 逐条定级时每次请求携带的消息条数（默认 20，代码再夹到 1~30）。注意 news.py 调用 label_items 时没有传 batch，函数默认值 20 先生效，因此该键只在调用方显式传 0/None 时才会被读到——改它通常看不到行为变化。",
        "kind": "int",
        "options": None, "min": 1, "max": 30, "step": 1, "unit": "条",
        "group": "消息面与动态权重", "order": 145, "advanced": True,
        "default": None,
        "effect": "正常调用路径下无实际影响；若改代码让它生效，调大批次会减少请求次数但单次 token 更多。",
        "dead": False},
    {
        "key": "strategy.event_calibration", "label": "事件命中率校准",
        "desc": "可选：按历史实测命中率微调各事件类型的强度（建议倍数来自 calib_history，经 n/(n+20) 收缩，强度再夹到 ±8）。默认不生效，只报告；设为 true 才应用。该键不在 config.json 里。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 160, "advanced": True,
        "default": None,
        "effect": "开启→同一批消息的强度会按历史命中率被放大或缩小（口径不再与旧版逐位一致）。",
        "dead": False},
    {
        "key": "strategy.snap_backfill_days", "label": "快照回填天数",
        "desc": "每日运行末尾的「资产曲线自愈」回看天数（默认 7）：补齐此前因当晚没跑、净值未出齐而缺失的近日快照，避免收益曲线断点。该键不在 config.json 里。",
        "kind": "int",
        "options": None, "min": 0, "max": 60, "step": 1, "unit": "交易日",
        "group": "消息面与动态权重", "order": 170, "advanced": True,
        "default": None,
        "effect": "调大→回填更多历史快照（首次运行会多几次净值查询）；调 0→不做自愈，曲线可能长期缺日。",
        "dead": False},
    {
        "key": "strategy.replay_news", "label": "回放接入消息面",
        "desc": "回测/回放时是否按日还原线上同口径的「当日消息分」（取十年消息库 cls_history.db 聚合，走 news.daily_score_map 并带磁盘缓存）。默认 true（缺缓存时该维度自动为 0）；关闭则退回纯量化口径回测。只影响回测结果，不影响实盘每日决策。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "消息面与动态权重", "order": 180, "advanced": False,
        "default": None,
        "effect": "关闭→回测只验证量化分与风控（结果与线上口径不一致）；开启→回测耗时更长但消息面子系统才真正被覆盖。",
        "dead": False},

    # ================= 风控止损与冷却 =================
    {
        "key": "strategy.risk.fund_stop_loss_pct", "label": "单只止损线",
        "desc": "单只基金浮亏 ≤ 该值（默认 −8%，settings 校验必须为负）→ 清仓止损；持仓不足 min_hold_days 且不允许提前卖时，只提示「解锁后立即执行」。",
        "kind": "number",
        "options": None, "min": -0.5, "max": 0.0, "step": 0.01, "unit": "比例",
        "group": "风控止损与冷却", "order": 10, "advanced": False,
        "default": None,
        "effect": "调浅（如 −5%）→更早止损但更容易被震荡扫出；调深→少被噪声打掉但单只亏损更大。",
        "dead": False},
    {
        "key": "strategy.risk.fund_take_profit_pct", "label": "止盈一半线",
        "desc": "单只浮盈 ≥ 该值（默认 10%）→ 赎回一半锁定利润。settings 校验它不能大于 fund_take_profit_full_pct。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "风控止损与冷却", "order": 20, "advanced": False,
        "default": None,
        "effect": "调低→更早落袋一半（降低回撤，也可能削掉趋势收益）；调高→更晚止盈。",
        "dead": False},
    {
        "key": "strategy.risk.fund_take_profit_full_pct", "label": "止盈清仓线",
        "desc": "单只浮盈 ≥ 该值（默认 25%）→ 全部赎回落袋为安。代码是单遍判断（每只当天只执行一种止盈动作）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 2.0, "step": 0.01, "unit": "比例",
        "group": "风控止损与冷却", "order": 30, "advanced": False,
        "default": None,
        "effect": "调低→大赚的板块被更快清掉（可能错过主升浪）；调高→只在小概率大行情才清仓。",
        "dead": False},
    {
        "key": "strategy.risk.portfolio_stop_pct", "label": "组合止损线",
        "desc": "总资产 ≤ 起始资金×(1+该值)（默认 −15%）→ 组合止损：强制把权益降到 25%，当日只防守不进攻，并进入 rearm_days 防御冷却。",
        "kind": "number",
        "options": None, "min": -0.5, "max": 0.0, "step": 0.01, "unit": "比例",
        "group": "风控止损与冷却", "order": 40, "advanced": False,
        "default": None,
        "effect": "调浅→更早进入防守（资金保护更好，也可能在底部割肉）；调深→容忍更大账户回撤。",
        "dead": False},
    {
        "key": "strategy.risk.peak_trailing_pct", "label": "峰值回撤线",
        "desc": "总资产自历史峰值回撤 ≥ 该值 且当日评分 < reentry_score → 跟踪止盈，把权益强行降到 25%（默认 1.0 = 实际上关闭：只有账户腰斩才触发）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例",
        "group": "风控止损与冷却", "order": 50, "advanced": False,
        "default": None,
        "effect": "调到 0.10~0.20 → 会真实启动峰值回撤保护（回撤更小，但反弹初期可能被锁在场外）。",
        "dead": False},
    {
        "key": "strategy.risk.reentry_score", "label": "回撤再入评分",
        "desc": "峰值回撤触发的附加条件：评分低于该值才减仓（评分高说明市场转强，不减）。默认 25。",
        "kind": "int",
        "options": None, "min": -100, "max": 100, "step": 5, "unit": "分",
        "group": "风控止损与冷却", "order": 60, "advanced": True,
        "default": None,
        "effect": "调高→回撤时更容易触发强制减仓（更保守）；调低→回撤保护更难生效。",
        "dead": False},
    {
        "key": "strategy.risk.rearm_days", "label": "防御冷却天数",
        "desc": "组合级风控（组合止损/峰值回撤）触发后锁定的防御期交易日数（默认 5），期间目标仓位被压到 ≤25%，到期后按新评分重新决策（避免到期瞬间满仓追涨）。",
        "kind": "int",
        "options": None, "min": 0, "max": 60, "step": 1, "unit": "交易日",
        "group": "风控止损与冷却", "order": 70, "advanced": False,
        "default": None,
        "effect": "调大→风控后更久保持低仓（错过反转的风险变大）；调小→冷却几乎立即解除。",
        "dead": False},
    {
        "key": "strategy.risk.allow_rearm_trade", "label": "冷却期例外放行",
        "desc": "冷却期内是否允许「例外放行日」：当日再触发单只止损/止盈，或市场评分 > rearm_bull_score 时，解除「只防守」限制、按常规规则调仓（可经基金转换加仓/换基）。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "风控止损与冷却", "order": 80, "advanced": False,
        "default": None,
        "effect": "关闭→冷却期内一律不加仓，防守更纯粹但反弹第一天铁定缺席。",
        "dead": False},
    {
        "key": "strategy.risk.rearm_bull_score", "label": "放行评分阈值",
        "desc": "冷却期内市场评分高于此值（默认 50）即视为市场转强、允许直接调仓。仅在 allow_rearm_trade=true 时有用。",
        "kind": "int",
        "options": None, "min": -100, "max": 100, "step": 5, "unit": "分",
        "group": "风控止损与冷却", "order": 90, "advanced": True,
        "default": None,
        "effect": "调低→冷却期更容易被评分放行（恢复进攻更快）；调高→几乎只在极端强势时才放行。",
        "dead": False},
    {
        "key": "strategy.risk.allow_early_exit_fee", "label": "允许承担惩罚费",
        "desc": "true：不足 min_hold_days 的份额也照常卖出并计费（止损/止盈清仓改按全部市值计算）；false（默认）：只卖可卖部分，其余提示等解锁后再执行。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "风控止损与冷却", "order": 100, "advanced": False,
        "default": None,
        "effect": "开启→止损更及时、但要实打实付 1.5% 短期赎回费；关闭→风控会被锁定期推迟到解锁日。",
        "dead": False},
    {
        "key": "strategy.risk.ma_break_step", "label": "破均线减仓步长",
        "desc": "反应型风控（只用已实现收盘价，不预测）：收盘跌破 MA60/MA120 每中一条减仓该比例，累计封顶 ma_break_max。默认 0.0 = 关闭该机制（代码里 step<=0 直接返回不减仓）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.5, "step": 0.05, "unit": "比例",
        "group": "风控止损与冷却", "order": 110, "advanced": False,
        "default": None,
        "effect": "设成 0.10~0.20 才会真正启动：跌破均线自动降仓、站回均线自动恢复（能减小回撤，也会在震荡市反复减仓）。",
        "dead": False},
    {
        "key": "strategy.risk.ma_break_max", "label": "破均线减仓上限",
        "desc": "反应型风控累计减仓的比例上限（默认 0.4 = 40%），即两条均线都破也只减到这里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "风控止损与冷却", "order": 120, "advanced": True,
        "default": None,
        "effect": "调高→破位时撤得更彻底（熊市保护更强、牛市更容易被甩下车）。",
        "dead": False},
    {
        "key": "strategy.risk.ma_break_skip_ma20", "label": "忽略 MA20 破位",
        "desc": "true（默认）：只统计 MA60/MA120 破位，跳过 MA20——十年实测 MA20 破位噪声大（易 whipsaw）。false：MA20 也算一条。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "风控止损与冷却", "order": 130, "advanced": True,
        "default": None,
        "effect": "关闭→减仓信号更频繁（单次减仓比例更容易翻倍），震荡市里更容易被打脸。",
        "dead": False},
    {
        "key": "strategy.risk.ma_break_floor", "label": "破均线底仓",
        "desc": "反应型风控减仓后保留的权益底仓比例（默认 0.4）：计算为 max(min(floor, 减仓前目标), 目标−减仓量)，即减仓不会把目标压到底仓以下（十年实测「减太狠」会砍掉牛市收益）。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "风控止损与冷却", "order": 140, "advanced": True,
        "default": None,
        "effect": "调低→破位时能降到更空（保护更强、也更容易在底部丢掉筹码）；调高→几乎减不动仓位。",
        "dead": False},

    # ================= 预测型风险过滤器 =================
    {
        "key": "strategy.risk_overlay_enable", "label": "风险过滤器开关",
        "desc": "开启「事件+行情」模型输出的仓位乘数过滤器（默认关闭，属于已降级为参考的预测型风控）：信号只下调权益目标仓位、永不上调，且有 floor 保底；模型不可用/数据缺失时一律 scale=1.0 不干预。引擎里该开关为 false 时根本不去取信号。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "预测型风险过滤器", "order": 10, "advanced": False,
        "default": None,
        "effect": "开启→模型认为次日易跌时会主动降仓（能躲坏日子，也可能在回升日被反复打脸）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_mapping", "label": "概率→仓位映射",
        "desc": "把 P(次日沪深300上涨) 映射成仓位乘数的方案（选项即 risk_overlay.MAPPINGS 的全部键）。名字里带 on/off 阈值：flat_0_1_p52 = P≥0.52 满仓否则空仓；floor3_flat = P≥0.5 满仓否则 0.3；graded_* 与 flat3_* 是三档。填了映射表以外的名字才会退到 risk_overlay_on_th/off_th/neutral/off 这四个自定义阈值。",
        "kind": "enum",
        "options": [{"value": "flat_0_1_p52", "label": "P≥0.52 满仓否则空仓（默认）"},
                    {"value": "flat_0_1_p50", "label": "P≥0.50 满仓否则空仓"},
                    {"value": "flat3_0_0.5_1", "label": "三档 0 / 0.5 / 1"},
                    {"value": "floor3_flat", "label": "P≥0.5 满仓否则 0.3"},
                    {"value": "graded_0.4_0.7_1", "label": "三档 0.4 / 0.7 / 1"},
                    {"value": "graded_0.3_0.7_1", "label": "三档 0.3 / 0.7 / 1"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "预测型风险过滤器", "order": 20, "advanced": False,
        "default": None,
        "effect": "越保守的映射（如 flat_0_1_*）空仓日越多：回撤更小、也更容易踏空。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_smooth", "label": "预测平滑天数",
        "desc": "取近 N 日 walk-forward 预测的均值作为当日 P(涨)，用来抗抖动（默认 5）。",
        "kind": "int",
        "options": None, "min": 1, "max": 30, "step": 1, "unit": "天",
        "group": "预测型风险过滤器", "order": 30, "advanced": True,
        "default": None,
        "effect": "调大→信号更稳但更滞后；调小→信号更灵敏、切换更频繁。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_min_hold", "label": "过滤器最小间隔",
        "desc": "两次由叠加驱动型调仓之间至少间隔的交易日数（默认 10）。场景：当日信号触发降仓即记为一次叠加，随后该天数内不因叠加重复降仓。",
        "kind": "int",
        "options": None, "min": 0, "max": 60, "step": 1, "unit": "交易日",
        "group": "预测型风险过滤器", "order": 40, "advanced": True,
        "default": None,
        "effect": "调大→叠加降仓更少（更省交易成本，反应更慢）；调小→频繁叠加、赎回费损耗上升。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_vol_pause", "label": "极端波动暂停",
        "desc": "波动率分位 ≥ 该值（默认 0.90）时暂停过滤器、保持满仓——极端情绪下择时容易被反复打脸。设 0 表示永不暂停。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "预测型风险过滤器", "order": 50, "advanced": True,
        "default": None,
        "effect": "调低→更多日子不降仓（V 型反转里少吃打脸，单边下跌里保护变弱）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_bull_vol_max", "label": "牛市覆盖波动上限",
        "desc": "波动率分位低于该值（默认 0.0 = 关闭牛市覆盖）且均线多头排列时，判定为「牛市覆盖」：不降仓并请求把权益目标抬到 risk_overlay_bull_floor。设 0 或负值即完全停用该覆盖。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "分位",
        "group": "预测型风险过滤器", "order": 60, "advanced": True,
        "default": None,
        "effect": "设成 0.30 才会启动：低波动上涨行情不再被降仓（避免让出涨幅），但需要模型识别出低波动+均线多头。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_bull_floor", "label": "牛市覆盖地板",
        "desc": "牛市覆盖生效时要求的最低权益目标仓位（默认 0.80），代码取 max(当前目标, min(1.0, 该值))，仍会被硬上限与风控 w_cap 压回。只在覆盖真正触发时起作用。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "预测型风险过滤器", "order": 70, "advanced": True,
        "default": None,
        "effect": "调高→牛市覆盖时仓位更满（但被 equity_hard_cap 0.80 顶住，一般看不出差别）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_floor", "label": "过滤器仓位地板",
        "desc": "过滤降仓后的最低权益仓位保护（默认 0.0）：目标 = max(目标×scale, floor)，且仅当原目标本来高于 floor 时才抬回 floor。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "比例",
        "group": "预测型风险过滤器", "order": 80, "advanced": True,
        "default": None,
        "effect": "调高→过滤器不能把仓位压到该值以下（避免全空仓踏空，也削弱了保护力度）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_on_th", "label": "自定义满仓阈值",
        "desc": "只有当 risk_overlay_mapping 写了 MAPPINGS 里不存在的名字时才会用到：P ≥ 该值 → 满仓（scale=1.0）。该键不在 config.json 里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "概率",
        "group": "预测型风险过滤器", "order": 90, "advanced": True,
        "default": None,
        "effect": "调低→更容易满仓（更激进）；名字非法时也要注意 off_th ≤ on_th 才有意义。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_off_th", "label": "自定义减仓阈值",
        "desc": "同上的自定义路径：P ≥ off_th（且 < on_th）→ 中性仓（risk_overlay_neutral）；P < off_th → 低仓（risk_overlay_off）。该键不在 config.json 里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.01, "unit": "概率",
        "group": "预测型风险过滤器", "order": 100, "advanced": True,
        "default": None,
        "effect": "调高→更多日子被判为低仓（更保守）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_neutral", "label": "自定义中性乘数",
        "desc": "自定义路径的中间档仓位乘数（默认 0.7）。该键不在 config.json 里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "乘数",
        "group": "预测型风险过滤器", "order": 110, "advanced": True,
        "default": None,
        "effect": "调低→中间档也明显降仓（更保守）。",
        "dead": False},
    {
        "key": "strategy.risk_overlay_off", "label": "自定义低仓乘数",
        "desc": "自定义路径的最低档仓位乘数（默认 0.4）。该键不在 config.json 里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "乘数",
        "group": "预测型风险过滤器", "order": 120, "advanced": True,
        "default": None,
        "effect": "调 0 → 看空时目标仓位归零（最保守，等于空仓）。",
        "dead": False},
    {
        "key": "strategy.micro_fee_proxy", "label": "换手费率假设",
        "desc": "风险过滤器/事件模型离线评估时使用的单次换手费率假设（默认取 event_model.FEE）。只影响研究脚本与评估结论，不影响实盘下单。该键不在 config.json 里。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.05, "step": 0.0005, "unit": "比例",
        "group": "预测型风险过滤器", "order": 130, "advanced": True,
        "default": None,
        "effect": "调高→评估里频繁切换的策略会被更严厉地扣分（只影响评估口径）。",
        "dead": False},

    # ================= 选基与动态筛选 =================
    {
        "key": "strategy.top_n", "label": "组合持仓只数",
        "desc": "最多选几只进攻基金组成组合（代码用 max(1,int(...)) 保证至少 1 只）；这 N 只也是「组合内最弱」轮动比较的参照集合。",
        "kind": "int",
        "options": None, "min": 1, "max": 10, "step": 1, "unit": "只",
        "group": "选基与动态筛选", "order": 10, "advanced": False,
        "default": None,
        "effect": "调大→更分散（单只风险小、动量弹性被摊薄）；调小→更集中（波动更大）。",
        "dead": False},
    {
        "key": "strategy.theme_max", "label": "同主题上限",
        "desc": "同一主题（strategy.THEME_GROUPS 归一化，如半导体/芯片算一组）最多选几只，代码保证至少 1。",
        "kind": "int",
        "options": None, "min": 1, "max": 5, "step": 1, "unit": "只",
        "group": "选基与动态筛选", "order": 20, "advanced": False,
        "default": None,
        "effect": "调大到 2~3 → 可以重仓押一个板块（进攻性强、板块回撤时更痛）；设 1 最分散。",
        "dead": False},
    {
        "key": "strategy.confidence_shrink", "label": "分歧收缩系数",
        "desc": "仅 equity_base_mode=score 时生效：多信号共识度 <1 时把仓位往中性拉回（raw = base + (raw−base)×(shrink+(1−shrink)×conf)）；默认 1.0 = 不收缩。代码夹到 0~1。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 1.0, "step": 0.05, "unit": "系数",
        "group": "选基与动态筛选", "order": 30, "advanced": True,
        "default": None,
        "effect": "调小→信号分歧时更靠中性仓位（更保守）；fixed 模式下无论怎么调都无效。",
        "dead": False},
    {
        "key": "screening.enabled", "label": "动态筛选开关",
        "desc": "开启后每日运行前会自动检查备选池是否过期（≥refresh_days）或不足 min_total 只，过期就按 20 日动量从 universe 重建；关闭则只用 config.json 的 pool（也可手动真值传入 force 强制筛选）。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "选基与动态筛选", "order": 40, "advanced": False,
        "default": None,
        "effect": "关闭→候选池固定，选基不再自适应市场；开启会消耗数据源配额（每只候选一次历史请求）。",
        "dead": False},
    {
        "key": "screening.refresh_days", "label": "备选池刷新周期",
        "desc": "备选池的过期天数：池上次更新时间距今 ≥ 该值就自动重建（默认 7 天 ≈ 每周一次）。",
        "kind": "int",
        "options": None, "min": 1, "max": 90, "step": 1, "unit": "天",
        "group": "选基与动态筛选", "order": 50, "advanced": False,
        "default": None,
        "effect": "调小→更紧跟热点但数据配额与请求量上升；调大→池子更稳定、可能长期持有走弱的候选。",
        "dead": False},
    {
        "key": "screening.min_total", "label": "备选池最小只数",
        "desc": "重建后池子总量不得低于该值（默认 15）：代码会用动量顺延、持仓保护、原 config 池依次补齐到该数。",
        "kind": "int",
        "options": None, "min": 2, "max": 100, "step": 1, "unit": "只",
        "group": "选基与动态筛选", "order": 60, "advanced": False,
        "default": None,
        "effect": "调高→池子更宽（选基空间大、但若数据不足会退化成保留更多旧候选）；调低→更容易只留下最强的少数。",
        "dead": False},
    {
        "key": "screening.top_equity", "label": "权益候选只数",
        "desc": "每轮重建时按 20 日动量取前多少只权益基金进池（默认 14），过程中同主题最多 2 只、不足再放宽补足。",
        "kind": "int",
        "options": None, "min": 1, "max": 60, "step": 1, "unit": "只",
        "group": "选基与动态筛选", "order": 70, "advanced": False,
        "default": None,
        "effect": "调大→候选更多（主题更分散、覆盖面广）；调小→池子更集中，选基自由度降低。",
        "dead": False},
    {
        "key": "screening.min_history_days", "label": "最短历史天数",
        "desc": "候选基金的历史净值跨度不足该天数（默认 120 天）就被剔除，避免新成立基金因样本太短上榜；另外最后净值距今超过 20 天的疑似停更也会被跳过。",
        "kind": "int",
        "options": None, "min": 20, "max": 1000, "step": 10, "unit": "天",
        "group": "选基与动态筛选", "order": 80, "advanced": False,
        "default": None,
        "effect": "调小→新基金也能入选（样本噪声大）；调大→只留老基金，可能漏掉新发的强势品种。",
        "dead": False},
    {
        "key": "screening.universe", "label": "候选基金名单",
        "desc": "动态筛选的候选全集（每项 code + name）：重建时逐个拉历史净值、算 20 日动量后排序取 top_equity。当前持仓基金与债基永远保留，不受名单变动影响。改它等于改「可选范围」，条目越多越吃数据配额。",
        "kind": "map",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "选基与动态筛选", "order": 90, "advanced": True,
        "default": None,
        "effect": "名单越大→筛选覆盖越广、单次重建请求越多（智兔配额不足时会顺延到次日）；名单过小会让池子长期同质。",
        "dead": False},
    {
        "key": "pool", "label": "基金池",
        "desc": "正式持仓/候选基金列表（每项 code/name/kind/role/buy_rate/sell_rate_lt7d/sell_rate_ge7d/min_buy）。settings 要求至少一只 equity 与一只 bond、代码不能重复；kind=equity 的是进攻候选池，kind=bond 的是防御债基（取第一个债基做现金打理）。",
        "kind": "map",
        "options": [{"value": "equity", "label": "权益（进攻）"},
                    {"value": "bond", "label": "债券（防御）"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "选基与动态筛选", "order": 100, "advanced": True,
        "default": None,
        "effect": "改动属于「换标的」级别：删掉债基会导致配置校验直接报错、程序拒绝启动。",
        "dead": False},
    {
        "key": "fees.buy_rate_default", "label": "默认申购费率",
        "desc": "基金条目没写 buy_rate 时的默认申购费率（默认 0 = C 类免申购费），用于下单金额与费用估算。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.05, "step": 0.0005, "unit": "比例",
        "group": "选基与动态筛选", "order": 110, "advanced": True,
        "default": None,
        "effect": "调高→预估成本增加、可动用金额略减（不影响真实费率，真实费率在平台）。",
        "dead": False},
    {
        "key": "fees.sell_lt7d_default", "label": "短期赎回费率",
        "desc": "持有不足 min_hold_days（7 天）的默认赎回费率（默认 1.5%），基金条目没写 sell_rate_lt7d 时用它；也是「不足 7 天不卖」这条纪律的理由。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.05, "step": 0.0005, "unit": "比例",
        "group": "选基与动态筛选", "order": 120, "advanced": True,
        "default": None,
        "effect": "调高→提前赎回的成本假设更贵（策略会更不愿意承担惩罚费）；它只影响估算与提示。",
        "dead": False},
    {
        "key": "fees.sell_ge7d_default", "label": "满7天赎回费率",
        "desc": "持满 min_hold_days 之后的默认赎回费率（默认 0），基金条目没写 sell_rate_ge7d 时用它。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 0.05, "step": 0.0005, "unit": "比例",
        "group": "选基与动态筛选", "order": 130, "advanced": True,
        "default": None,
        "effect": "调高→正常赎回的成本假设上升，策略在估算净收益时更保守。",
        "dead": False},

    # ================= 数据源 =================
    {
        "key": "data.provider", "label": "行情数据源",
        "desc": "主数据通道选择，代码按值决定通道链：akshare→基金净值走 AKShare（免费无配额），指数仍走智兔（有 Token 时）或东财，AKShare 不可用会自动降级；zhitu→净值与指数都以智兔为主；eastmoney→全部走东财/天天基金公开接口。",
        "kind": "enum",
        "options": [{"value": "akshare", "label": "AKShare（默认，免费）"},
                    {"value": "zhitu", "label": "智兔数服"},
                    {"value": "eastmoney", "label": "东方财富直连"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 10, "advanced": False,
        "default": None,
        "effect": "换源会改变数据来源与限频行为：zhitu 受每日配额限制、akshare 依赖本地库是否可用、eastmoney 全走公开接口（可能被限流但无配额）。",
        "dead": False},
    {
        "key": "data.zhitu_daily_limit", "label": "智兔每日配额",
        "desc": "智兔接口每日允许的调用次数上限（默认 200），用来做配额节流：配额不足时动态筛选与备选池刷新会直接跳过并提示「明天再筛选」。",
        "kind": "int",
        "options": None, "min": 1, "max": 5000, "step": 10, "unit": "次/日",
        "group": "数据源", "order": 20, "advanced": True,
        "default": None,
        "effect": "填得比套餐实际配额高→会真的把接口打到限频报错；填低→更容易出现「配额不足、今日不筛选」。",
        "dead": False},
    {
        "key": "data.zhitu_token", "label": "智兔 Token",
        "desc": "智兔数服的访问 Token（敏感凭证）。填了它且 provider 为 zhitu/akshare 时才启用智兔通道（指数/溢价等）；留空则退化到东财公开接口。网页状态接口只返回 *** 不回显明文。修改需谨慎：填错会导致数据源报错与降级。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 30, "advanced": True,
        "default": None,
        "effect": "改错→智兔通道报错并自动降级到东财/本地缓存，当日行情可能缺失或滞后。",
        "dead": False},
    {
        "key": "market.index", "label": "主指数",
        "desc": "基准指数（默认沪深300）：既是大盘动量/市场评分的来源，也是「相对动量门槛」的比较基准与网页标题里的指数名。子键 name（显示名）、zhitu_code（智兔代码，如 000300.SH）、eastmoney_secid（东财 secid，如 1.000300）。代码按是否启用智兔通道在其中二选一。",
        "kind": "map",
        "options": [{"value": "name", "label": "指数名称"},
                    {"value": "zhitu_code", "label": "智兔代码"},
                    {"value": "eastmoney_secid", "label": "东财 secid"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 40, "advanced": True,
        "default": None,
        "effect": "换成别的指数会连带改变市场评分、相对动量门槛与板块相对强弱的基准（影响很大，慎改）。",
        "dead": False},
    {
        "key": "market.index.name", "label": "· 指数名称",
        "desc": "主指数显示名（用于界面与文案）。纯标注，不参与代码判断。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 50, "advanced": True,
        "default": None,
        "effect": "只影响显示文案。",
        "dead": False},
    {
        "key": "market.index.zhitu_code", "label": "· 智兔代码",
        "desc": "主指数在智兔接口的代码（默认 000300.SH）。只有启用智兔通道（有 Token 且 provider 为 zhitu/akshare）时才被选来取数。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 60, "advanced": True,
        "default": None,
        "effect": "写错会取不到指数数据，行情与评分可能整体失效。",
        "dead": False},
    {
        "key": "market.index.eastmoney_secid", "label": "· 东财 secid",
        "desc": "主指数在东财接口的 secid（默认 1.000300，1=沪市 / 0=深市）。未启用智兔通道时用它取指数行情与 K 线，也是 event_model 取基准收盘的入口。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 70, "advanced": True,
        "default": None,
        "effect": "写错会导致指数行情/K 线抓取失败并回退本地缓存（数据过期）。",
        "dead": False},
    {
        "key": "market.indices", "label": "指数看板清单",
        "desc": "网页行情看板与盘中复核用的指数列表（每项 name + secid，可加 benchmark:true 标记主基准）。代码会跳过没有 secid 的条目，benchmark 那条用于盘中相对强弱对比。",
        "kind": "list",
        "options": [{"value": "1.000001", "label": "上证指数"},
                    {"value": "0.399001", "label": "深证成指"},
                    {"value": "0.399006", "label": "创业板指"},
                    {"value": "1.000300", "label": "沪深300（基准）"},
                    {"value": "1.000688", "label": "科创50"},
                    {"value": "1.000905", "label": "中证500"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 80, "advanced": True,
        "default": None,
        "effect": "增删条目只改变看板与盘中对比范围（不改变选基与仓位逻辑）；条目越多盘中请求越多。",
        "dead": False},
    {
        "key": "market.benchmarks", "label": "跨市场基准",
        "desc": "已废弃，未在代码中读取（grep -n '\"benchmarks\"' fundai/*.py 只命中 datasource.py 里 res.setdefault(\"benchmarks\", {}) 这个结果字典字段，无人读配置的 market.benchmarks）：event_model 的跨市场风格特征用的是代码内常量 BENCH 字典（写死指数代码），不读配置。",
        "kind": "map",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "数据源", "order": 90, "advanced": True,
        "default": None,
        "effect": "无任何影响；要改跨市场基准请改 event_model.BENCH 常量。",
        "dead": True},
    {
        "key": "account.exec_mode", "label": "账户执行模式",
        "desc": "manual（默认）：AI 只产出「今日建议」，订单挂起等人工执行后录入；auto：按 T+1 净值模拟自动成交。引擎把它写进账户 meta 并据此决定建议文案与是否自动成单。",
        "kind": "enum",
        "options": [{"value": "manual", "label": "人工执行（默认）"},
                    {"value": "auto", "label": "模拟自动成交"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "运行与服务", "order": 10, "advanced": False,
        "default": None,
        "effect": "改成 auto 会让系统按其模拟净值自行记账成交（适合回放/演示，不适合真实手动下单）。",
        "dead": False},
    {
        "key": "account.name", "label": "账户名称",
        "desc": "账户显示名（写入账户 meta，用于界面标题与状态输出）。纯展示字段。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "运行与服务", "order": 20, "advanced": False,
        "default": None,
        "effect": "只影响显示，不影响任何计算。",
        "dead": False},
    {
        "key": "account.initial_cash", "label": "起始资金",
        "desc": "**起始资金（可自定义，默认 1000）**。它决定三件事：初始化/重置账户时写入账本的起始现金；目标线的基数（目标线 = 起始资金 × target_multiple）；组合止损与收益率的基准。settings 校验必须 >0。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 1000000.0, "step": 100.0, "unit": "元",
        "group": "运行与服务", "order": 30, "advanced": False,
        "default": None,
        "effect": "改它**不会**改变已有账本里的历史投入（历史是事实），但会重算目标线；要让新起始资金真正生效，用 `python app.py init --cash N`（或加 --reset 清账重开）。",
        "dead": False},
    {
        "key": "account.target_multiple", "label": "目标倍数",
        "desc": "**目标线的唯一来源**：目标线 = 起始资金 × 该倍数（默认 1.3 = +30%）。界面目标进度、还差多少、回测的目标达成判定、LLM 提示词里的目标都从它推导；用它就不需要再手改 target_value。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 10.0, "step": 0.05, "unit": "倍",
        "group": "运行与服务", "order": 35, "advanced": False,
        "default": None,
        "effect": "调大→目标更高、进度条更难走（不改变任何交易规则）；调到 1.0 表示「只要保本」。",
        "dead": False},
    {
        "key": "account.target_value", "label": "目标金额（派生）",
        "desc": "**派生值**：由「起始资金 × 目标倍数」自动算出（默认 1000 × 1.3 = 1300 元）。面板里改起始资金或倍数时会自动重算并写回；手改它只能在没有 target_multiple 的旧配置里生效。",
        "kind": "number",
        "options": None, "min": 1.0, "max": 10000000.0, "step": 100.0, "unit": "元",
        "group": "运行与服务", "order": 40, "advanced": True,
        "default": None,
        "effect": "只影响目标进度与达成标记，不改变交易规则；建议改 target_multiple 而不是它。",
        "dead": False},
    {
        "key": "account.start_date", "label": "开始日期",
        "desc": "已废弃，未在代码中读取（grep -n '\"start_date\"' fundai/*.py 与 app.py 只命中账本 meta：engine.ensure_account 写 meta[\"start_date\"] = util.today_str()，app.py 只打印 meta 的值，没有任何地方读 config 的 account.start_date）：配置里的这个键不会写进账户，也不影响期限计算。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "运行与服务", "order": 50, "advanced": True,
        "default": None,
        "effect": "无任何影响（账户起始日由初始化当天决定）。",
        "dead": True},
    {
        "key": "account.end_date", "label": "结束日期",
        "desc": "已废弃，未在代码中读取（grep -n '\"end_date\"' fundai/*.py 与 app.py 只命中账本 meta / 回放参数：账户期限由 engine.ensure_account 与回放路径用 util.add_days(today, HALF_YEAR_DAYS) 推导，无人读 config 的 account.end_date）。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "运行与服务", "order": 60, "advanced": True,
        "default": None,
        "effect": "无任何影响（期限由代码按半年推算）。",
        "dead": True},
    {
        "key": "server.host", "label": "监听地址",
        "desc": "网页服务绑定地址，默认 127.0.0.1（只本机可访问）。改成 0.0.0.0 等于把带账户数据的界面暴露到局域网，需自担风险。启动参数 --host 可临时覆盖。",
        "kind": "enum",
        "options": [{"value": "127.0.0.1", "label": "仅本机（默认，安全）"},
                    {"value": "0.0.0.0", "label": "局域网可访问（有风险）"}],
        "min": None, "max": None, "step": None, "unit": None,
        "group": "运行与服务", "order": 70, "advanced": False,
        "default": None,
        "effect": "改成 0.0.0.0 后同网段设备可打开你的持仓界面（无鉴权），只有明确需要时才改。",
        "dead": False},
    {
        "key": "server.port", "label": "服务端口",
        "desc": "网页服务端口（默认 8787），web.serve 用它启动 HTTP 服务；启动参数 --port 可临时覆盖。改端口不会热生效，需重启服务。",
        "kind": "int",
        "options": None, "min": 1024, "max": 65535, "step": 1, "unit": "端口",
        "group": "运行与服务", "order": 80, "advanced": False,
        "default": None,
        "effect": "端口被占用会导致服务起不来；本会话中的 GUI 地址不会自动跟着变。",
        "dead": False},
    {
        "key": "web.port", "label": "网页端口（别名）",
        "desc": "已废弃，未在代码中读取（grep -n '\"port\"' fundai/*.py 只命中 server.port：settings.py 的 DEFAULT_CFG[server][port] 与 web.py 的 cfg.get(\"server\", {}).get(\"port\", 8787)，没有任何地方读 web.port）：web 这个顶层键在 config.json 与 settings.DEFAULT_CFG 里都不存在，属于历史别名。请改 server.port。",
        "kind": "int",
        "options": None, "min": 1024, "max": 65535, "step": 1, "unit": "端口",
        "group": "运行与服务", "order": 90, "advanced": True,
        "default": None,
        "effect": "改它没有任何效果，请使用 server.port。",
        "dead": True},
    {
        "key": "llm.enabled", "label": "LLM 总开关",
        "desc": "大模型研判开关：引擎调用 analysis.llm_analyze 需要 enabled=true 且 api_key 非空，否则直接返回 None 并回退内置评分引擎；网页状态接口也用同一个口径给出「是否需要填 Key」。",
        "kind": "bool",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "LLM 与凭证", "order": 10, "advanced": False,
        "default": None,
        "effect": "关闭→每日研判完全由内置规则生成（无外部调用、无 token 成本）；开启后研判文案与评分来自模型，需注意 Key 与额度。",
        "dead": False},
    {
        "key": "llm.provider", "label": "服务商标注",
        "desc": "服务商名称，只用于显示与 llm_label 的家族识别（代码不据此切换协议，请求统一走 base_url 的 OpenAI 兼容 /chat/completions）；若 model 名能识别出家族（如 qwen）会优先显示家族名。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "LLM 与凭证", "order": 20, "advanced": False,
        "default": None,
        "effect": "只影响界面文案，不影响请求地址与模型选择。",
        "dead": False},
    {
        "key": "llm.api_key", "label": "API Key",
        "desc": "大模型访问密钥（敏感）：以 Bearer 方式放在请求头里。留空时即便 enabled=true 也不会发起调用；网页状态接口只回显是否已配置、不回显明文。修改需谨慎。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "LLM 与凭证", "order": 30, "advanced": True,
        "default": None,
        "effect": "填错→每日研判与消息定级都会静默回退到内置规则（不会报错，只是没有 AI 文案）。",
        "dead": False},
    {
        "key": "llm.base_url", "label": "接口地址",
        "desc": "OpenAI 兼容接口的根地址（默认 DeepSeek），代码会 rstrip(\"/\") 后拼 /chat/completions。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "LLM 与凭证", "order": 40, "advanced": True,
        "default": None,
        "effect": "改错→请求 404/连接失败，LLM 文案与消息定级自动回退，功能静默降级。",
        "dead": False},
    {
        "key": "llm.model", "label": "模型名",
        "desc": "调用的模型名（默认 deepseek-chat），也是 llm_label 识别模型家族的依据。注意 llm_news 在模型名为空时有自己的兜底名 qwen-flash。",
        "kind": "text",
        "options": None, "min": None, "max": None, "step": None, "unit": None,
        "group": "LLM 与凭证", "order": 50, "advanced": False,
        "default": None,
        "effect": "改错（如模型名不存在）→调用失败并静默回退内置引擎，界面会显示为「无 AI 研判」。",
        "dead": False},
    {
        "key": "llm.temperature", "label": "采样温度",
        "desc": "研判调用的采样温度（默认 0.4），代码 float(...) 后直传。注意：消息逐条定级（llm_news）里温度是写死的 0.1，不受这个键影响。",
        "kind": "number",
        "options": None, "min": 0.0, "max": 2.0, "step": 0.1, "unit": "温度",
        "group": "LLM 与凭证", "order": 60, "advanced": True,
        "default": None,
        "effect": "调高→研判文案更发散（同一数据可能给出不同结论）；调低→更稳定、更刻板。",
        "dead": False},
]

# ---------------------------------------------------------------------------
# 明确不收录的键 → 原因（coverage() 会据此把它们从 missing 里排除）
# ---------------------------------------------------------------------------
EXCLUDED = {
    "data.note": "纯说明文本，代码中没有任何读取点（grep -n '\"note\"' fundai/*.py 只命中同名局部变量）",
    "screening.note": "纯说明文本，代码不读取（screening.enabled/refresh_days 等才是生效键）",
}

# 前端开关面板里那一排开关的排序（只含 kind == "bool" 的 key，按重要性从高到低）
TOGGLE_ORDER = [
    "strategy.risk_overlay_enable",
    "strategy.dynamic_weights",
    "strategy.use_fund_convert",
    "strategy.micro_enable",
    "screening.enabled",
    "strategy.relative_momentum",
    "strategy.risk.allow_early_exit_fee",
    "strategy.risk.allow_rearm_trade",
    "strategy.news_llm_enable",
    "strategy.news_llm_contribute",
    "strategy.score_ema_enabled",
    "strategy.news_include_other_events",
    "strategy.event_calibration",
    "strategy.replay_news",
    "strategy.risk_adjusted",
    "strategy.risk.ma_break_skip_ma20",
    "llm.enabled",
]

_REQUIRED_FIELDS = ("key", "label", "desc", "kind", "options", "min", "max",
                    "step", "unit", "group", "order", "advanced", "default",
                    "effect", "dead")
_KINDS = ("bool", "number", "int", "enum", "list", "map", "text")


def load_config(path=None):
    """读 config.json（走 settings 的缓存/校验逻辑，保证与运行期同一份配置）。"""
    return settings.load_config(path)


def _defaults_from_config(cfg=None):
    """把 config.json 的**实际值**抽成 {点分路径: 值}。"""
    cfg = load_config() if cfg is None else cfg
    out = {}

    def walk(node, prefix):
        if isinstance(node, dict):
            if prefix:
                out[prefix] = json.loads(json.dumps(node, ensure_ascii=False))
            for k, v in node.items():
                walk(v, "{}.{}".format(prefix, k) if prefix else str(k))
        else:
            out[prefix] = node

    walk(cfg, "")
    return out


def by_key():
    """key -> param dict（返回的是 PARAMS 里的同一批 dict，不要就地修改）。"""
    return {p["key"]: p for p in PARAMS}


def toggles():
    """只含 kind == "bool" 的 key，按 TOGGLE_ORDER 排序（其余按 PARAMS 顺序）。"""
    bools = [p["key"] for p in PARAMS if p["kind"] == "bool"]
    rank = {k: i for i, k in enumerate(TOGGLE_ORDER)}
    return sorted(bools, key=lambda k: (rank.get(k, len(rank)), bools.index(k)))


def _leaf_keys(cfg):
    """config.json 里的叶子键：dict 继续下钻、不计自身（键自身只作路径前缀）。"""
    out = set()

    def walk(node, prefix):
        if isinstance(node, dict):
            if not node and prefix:
                out.add(prefix)          # 空 dict 也算一个叶子
            for k, v in node.items():
                walk(v, "{}.{}".format(prefix, k) if prefix else str(k))
        elif prefix:
            out.add(prefix)
    walk(cfg, "")
    return out


def coverage(cfg=None):
    """覆盖率报告。

    - missing：config.json 里有、PARAMS 没收录、也没登记在 EXCLUDED 的键（应为空）
    - extra：PARAMS 收录了、但 config.json 里没有的键（代码有默认值、可手工加进配置）；
      纯 dict 容器键（如 strategy.factor_weights / market.index）不算 extra ——
      它们只是子键的路径前缀，配置里当然不会单独出现。
    - excluded：登记在 EXCLUDED 里而配置确实存在的键（附原因）
    - total：config.json 叶子键总数；params：PARAMS 条数
    """
    cfg = load_config() if cfg is None else cfg
    have = _leaf_keys(cfg)
    known = by_key()
    missing = sorted(k for k in have if k not in known and k not in EXCLUDED)

    def _present(key):
        node = cfg
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    extra = sorted(k for k in known if k not in have and not _present(k))
    excluded = [{"key": k, "reason": EXCLUDED[k]}
                for k in sorted(EXCLUDED) if k in have]
    return {
        "missing": missing,
        "extra": extra,
        "excluded": excluded,
        "total": len(have),
        "params": len(PARAMS),
        "unwired": len(extra),            # = 配置里还没有的键数
    }


def validate_metadata():
    """自检：字段齐全、kind 合法、group 在 GROUPS 里、order 组内不重复、TOGGLES 一致。"""
    errs = []
    seen = set()
    orders = {}
    for p in PARAMS:
        key = p.get("key")
        if not key:
            errs.append("存在没有 key 的条目")
            continue
        if key in seen:
            errs.append("key 重复：{}".format(key))
        seen.add(key)
        for f in _REQUIRED_FIELDS:
            if f not in p:
                errs.append("{} 缺字段 {}".format(key, f))
        if p.get("kind") not in _KINDS:
            errs.append("{} 的 kind 非法：{}".format(key, p.get("kind")))
        if p.get("group") not in GROUPS:
            errs.append("{} 的 group 不在 GROUPS：{}".format(key, p.get("group")))
        if p.get("label") and len(str(p["label"])) > 12:
            errs.append("{} 的 label 超过 12 字：{}".format(key, p["label"]))
        if p.get("dead") and "未在代码中读取" not in str(p.get("desc", "")):
            errs.append("{} 标了 dead 但 desc 未写证据".format(key))
        if p.get("kind") in ("enum", "list") and not p.get("options"):
            errs.append("{} 是 {} 但没有 options".format(key, p.get("kind")))
        slot = (p.get("group"), p.get("order"))
        if slot in orders and orders[slot] != key:
            errs.append("组内 order 冲突：{} 与 {}".format(key, orders[slot]))
        orders[slot] = key
    if sorted(toggles()) != sorted(p["key"] for p in PARAMS if p["kind"] == "bool"):
        errs.append("TOGGLES 与 PARAMS 里的 bool 集合不一致")
    return errs


def _sync_defaults(cfg=None):
    """把 config.json 的实际值写进每个 param 的 default（None = 配置里没有该键）。"""
    dft = _defaults_from_config(cfg)
    for p in PARAMS:
        p["default"] = dft.get(p["key"], None)
    return dft


# 导入即完成一次默认值同步，方便前端直接读 PARAMS[i]["default"]
_sync_defaults()

# TOGGLES：前端那一排开关（所有 kind == "bool" 的 key，按重要性排序）
TOGGLES = toggles()


def _main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=== fundai.param_docs 覆盖率报告 ===")
    cov = coverage()
    print("PARAMS 条数        : {}".format(len(PARAMS)))
    print("config.json 叶子键 : {}".format(cov["total"]))
    print("missing（应=[]）    : {}".format(cov["missing"] or "无"))
    print("extra（配置里暂无）: {}".format(cov["extra"] or "无"))
    for e in cov["excluded"]:
        print("  · 不收录 {} ：{}".format(e["key"], e["reason"]))
    errs = validate_metadata()
    print("元数据自检         : {}".format("OK" if not errs else errs))
    print("\n--- 分组参数个数（按 GROUPS 顺序）---")
    for g in GROUPS:
        keys = [p["key"] for p in PARAMS if p["group"] == g]
        print("[{}] ".format(g) + "{} 个".format(len(keys)) +
              ("：" + "，".join(keys) if keys else "（空）"))
    print("\n--- TOGGLES（bool 开关，共 {} 个）---".format(len(TOGGLES)))
    for i, k in enumerate(TOGGLES, 1):
        p = by_key()[k]
        print("{:>2}. {:<40} {} = {}".format(i, k, p["label"], p["default"]))
    deads = [p["key"] for p in PARAMS if p["dead"]]
    print("\n--- dead（无读取点，共 {} 个）---".format(len(deads)))
    for k in deads:
        print("  · {}".format(k))
    print("\n--- 汇总：PARAMS {} 条 / 分组 {} 个 / TOGGLES {} 个 / dead {} 个 ---".format(
        len(PARAMS), len(GROUPS), len(TOGGLES), len(deads)))


if __name__ == "__main__":
    _main()
