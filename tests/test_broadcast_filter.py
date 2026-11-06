# -*- coding: utf-8 -*-
"""行情播报识别回归（2026-09-16 用户反馈）。

用户原话：
> 「消息界面重复的消息还是太多了，比如那种财联社报黄金啥的涨跌之类的，还有类似这种的
>   深证成指涨1.01%，现报13422.186点；上证指数涨0.30%，现报3875.776点；
>   创业板指涨1.82%，现报3307.017点。，全部都让AI默认读取就行了，
>   只是单纯的消息面，根本不构成利空利好」

为什么这不是"去重"能解决的：
  * 这类内容不是新闻而是**报价**，对次日方向不含增量信息（市场已实时定价）；
  * 标题里的数字每次都在变（涨1.01% → 涨0.98%）→ 标题归一化去重**必然失效**；
  * 旧逻辑把它们标成"重复但照样显示"，用户看到的仍是满屏重复。

本测试锁死两件事：
  ① 该归档的**必须**归档（用户举的两个例子在内）；
  ② **绝不能误归档真消息**（政策、公司事件、板块行情、人写的盘面复盘）。
第 ② 条同样重要：把"央行降准"或"黄金股涨停"归档掉，就是把真信号丢了。
"""
import unittest

from fundai import screening


class BroadcastDetectionTest(unittest.TestCase):
    # ---- 应归档：纯报价播报（用户举的两种 + 常见同类）----
    SHOULD_ARCHIVE = [
        # 用户原例 1：三大指数播报（数字每次都在变）
        "深证成指涨1.01%，现报13422.186点；上证指数涨0.30%，现报3875.776点；"
        "创业板指涨1.82%，现报3307.017点。",
        "创业板指涨2.00%，现报3312.955点；上证指数涨0.39%，现报3879.192点；"
        "深证成指涨1.14%，现报13439.030点。",
        "创业板指跌1.00%，现报3288.766点；上证指数跌0.10%，现报3884.309点",
        # 用户原例 2：财联社报黄金涨跌
        "财联社9月16日电，现货黄金涨0.8%，现报每盎司2650美元。",
        "财联社9月11日电，现货黄金上涨1%至每盎司4,357.6美元。",
        "财联社9月11日电，现货黄金向上触及4350美元，日内上涨0.79%。",
        # 商品/汇率/期货报价
        "COMEX黄金期货日内涨幅扩大至1.2%，现报2680美元/盎司。",
        "布伦特原油涨1.5%，报每桶72.3美元。",
        "纽约原油暗盘跌破96美元，日内跌超3.2%。",
        "沪金主力合约涨1.2%，报每克612元。",
        "离岸人民币兑美元涨0.3%，现报7.12。",
        "国内期货主力合约多数下跌 SC原油涨超6%",
        "财联社9月15日电，澳大利亚标普/澳证200指数下跌1%，报8662.10点。",
        # 实测漏归档过：带"突破"的报价仍是报价（曾把"突破"误当事件词排除）
        "现货黄金突破4340美元/盎司，日内涨1.08%。",
        "财联社9月16日电，泰国综合指数跌幅扩大至1%，报1,562.43点。",
    ]

    # ---- 绝不能归档：真消息 / 人写的复盘 / 事件性表述 ----
    SHOULD_KEEP = [
        "央行宣布全面降准0.5个百分点，释放长期流动性",
        "证监会发布新规，规范上市公司信息披露",
        "公司签约4.03亿元订单，产能利用率提升",
        "股东减持2%股份",
        "两市成交额1.2万亿，板块分化明显",
        # 板块/个股行情（真信号，不是商品报价）
        "黄金股集体涨停，山东黄金涨9.98%",
        "现货黄金价格上涨带动黄金股走强",
        "创业板指领涨，新能源板块走强",
        # 研判性表述（有人在解读，不是裸报价）
        "上证指数跌破3000点，市场恐慌情绪升温",
        # 人写的盘面复盘栏目
        "【股指期货早盘收盘】 中证1000指数期货连续涨2.53%，"
        " 沪深300指数期货连续涨0.86%",
        "【9月15日午间涨停分析】科创50半日上涨1.79%，半导体、PCB产业链全线走强",
        "【港股午评：恒生指数跌0.23%，恒生科技指数涨0.72%】腾讯控股涨2.93%",
        "【A股收评】三大指数集体收涨，两市超4000只个股上涨",
        # 历史性/事件性表述
        "上期所原油期货主力合约历史首次站上900元/桶",
        "黄金价格突破前高，创历史新高",
    ]

    def test_user_examples_are_archived(self):
        for txt in self.SHOULD_ARCHIVE:
            kind = screening.detect_broadcast(txt)
            self.assertTrue(kind, "应当识别为行情播报：{}".format(txt[:50]))
            self.assertIn(kind, ("index_broadcast", "commodity_broadcast"))

    def test_real_news_is_never_archived(self):
        for txt in self.SHOULD_KEEP:
            self.assertIsNone(
                screening.detect_broadcast(txt),
                "**误归档真消息**（会丢信号）：{}".format(txt[:50]))

    def test_grouping_key_ignores_changing_numbers(self):
        """数字变化不该影响分组：同模板报价必须归到同一组。"""
        a = screening.broadcast_key("深证成指涨1.01%，现报13422.186点")
        b = screening.broadcast_key("深证成指涨0.98%，现报13380.500点")
        self.assertEqual(a, b, "数字不同但模板相同 → 分组键必须一致")
        c = screening.broadcast_key("财联社9月16日电，现货黄金涨0.8%")
        d = screening.broadcast_key("财联社9月17日电，现货黄金涨1.2%")
        self.assertEqual(c, d)

    def test_event_type_is_in_auto_neutral_events(self):
        """归档类必须登记进 AUTO_NEUTRAL_EVENTS，否则前端/净情绪不会把它排除。"""
        self.assertIn(screening.BROADCAST_EVENT, screening.AUTO_NEUTRAL_EVENTS)

    def test_broadcast_summary_keeps_trace_of_count(self):
        rows = [{"title": "深证成指涨1.01%，现报13422.186点"},
                {"title": "深证成指涨0.98%，现报13380.500点"},
                {"title": "上证指数涨0.30%，现报3875.776点"}]
        rep = screening.broadcast_summary(rows)
        self.assertEqual(rep["event_type"], screening.BROADCAST_EVENT)
        self.assertEqual(rep["auto_label"], "irrelevant")
        self.assertEqual(rep["_broadcast_n"], 3, "要保留合并条数供追溯")
        self.assertIn("行情播报", rep["title"])


if __name__ == "__main__":
    unittest.main()
