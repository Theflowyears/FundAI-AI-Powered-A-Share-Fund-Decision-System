# -*- coding: utf-8 -*-
"""随包起始基金池（通用宽基/行业指数 + 债基，**不含任何密钥**）。

用途：全新环境（没有 `config.json`）第一次运行时，`settings.DEFAULT_CFG`
直接引用这里，避免"pool 为空 → 校验失败 → 服务根本起不来"。
用户自己的 `config.json` 永远优先；这里只是一份能立刻跑通的起点。

本文件由 `tools/gen_starter.py` 从当时的 `config.json` 生成，
可以随时重新生成，也可以手工编辑（都是普通的基金元数据）。
"""
POOL = [
    {"code": "011609", "name": "易方达上证科创50ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "008888", "name": "华夏国证半导体芯片ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "012700", "name": "易方达中证全指证券公司ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "011613", "name": "华夏上证科创板50成份ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "008282", "name": "国泰CES半导体芯片行业ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "008087", "name": "华夏中证5G通信主题ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "008586", "name": "华夏人工智能ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "001618", "name": "天弘中证电子ETF联接C", "kind": "equity", "role": "attack", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "005918", "name": "天弘沪深300ETF联接C", "kind": "equity", "role": "benchmark", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
    {"code": "270049", "name": "广发纯债债券C", "kind": "bond", "role": "primary", "buy_rate": 0.0, "sell_rate_lt7d": 0.015, "sell_rate_ge7d": 0.0, "min_buy": 10.0},
]

# 动态筛选候选池（refresh_pool 从这里按 20 日动量重建 ≥15 只备选池）
UNIVERSE = [
    {"code": "011609", "name": "易方达上证科创50ETF联接C"},
    {"code": "008888", "name": "华夏国证半导体芯片ETF联接C"},
    {"code": "012700", "name": "易方达中证全指证券公司ETF联接C"},
    {"code": "011613", "name": "华夏上证科创板50成份ETF联接C"},
    {"code": "008282", "name": "国泰CES半导体芯片行业ETF联接C"},
    {"code": "008087", "name": "华夏中证5G通信主题ETF联接C"},
    {"code": "008586", "name": "华夏人工智能ETF联接C"},
    {"code": "001618", "name": "天弘中证电子ETF联接C"},
    {"code": "005918", "name": "天弘沪深300ETF联接C"},
    {"code": "013894", "name": "国联安上证科创50ETF联接C"},
    {"code": "019386", "name": "东财上证科创50指数发起式C"},
    {"code": "013811", "name": "广发科创50ETF发起式联接C"},
    {"code": "007301", "name": "国联安中证全指半导体产品与设备ETF联接C"},
    {"code": "012553", "name": "天弘中证芯片产业ETF发起联接C"},
    {"code": "015337", "name": "嘉实中证芯片产业指数发起式C"},
    {"code": "020483", "name": "中欧中证芯片产业指数发起C"},
    {"code": "013446", "name": "东财中证芯片ETF发起式联接C"},
    {"code": "020840", "name": "南方中证半导体产业指数发起C"},
    {"code": "014777", "name": "富国中证芯片产业ETF发起式联接C"},
    {"code": "012630", "name": "广发国证半导体芯片ETF联接C"},
    {"code": "020671", "name": "易方达上证科创板芯片ETF联接发起式C"},
    {"code": "007993", "name": "华夏中证全指证券公司ETF联接C"},
    {"code": "008591", "name": "天弘中证全指证券公司ETF发起式联接C"},
    {"code": "013597", "name": "招商中证全指证券公司指数C"},
    {"code": "012363", "name": "国泰中证全指证券公司ETF联接C"},
    {"code": "013035", "name": "富国中证军工指数C"},
    {"code": "005693", "name": "广发中证军工ETF联接C"},
    {"code": "002199", "name": "前海开源中证军工指数C"},
    {"code": "010236", "name": "广发电子信息传媒股票C"},
    {"code": "022831", "name": "华商电子行业量化股票发起式C"},
    {"code": "017628", "name": "华商计算机行业量化股票发起式C"},
    {"code": "010210", "name": "国泰中证计算机主题ETF联接C"},
    {"code": "001630", "name": "天弘中证计算机主题ETF联接C"},
    {"code": "012620", "name": "嘉实中证软件服务ETF联接C"},
    {"code": "007818", "name": "国泰中证全指通信设备ETF联接C"},
    {"code": "008327", "name": "东财通信C"},
    {"code": "012734", "name": "易方达中证人工智能主题ETF联接C"},
    {"code": "011840", "name": "天弘中证人工智能主题ETF发起联接C"},
    {"code": "005963", "name": "宝盈人工智能股票C"},
    {"code": "005763", "name": "中欧电子信息产业沪港深股票C"},
    {"code": "014162", "name": "万家人工智能混合C"},
    {"code": "006697", "name": "华宝中证银行ETF联接C"},
    {"code": "002611", "name": "博时黄金ETF联接C"},
    {"code": "000217", "name": "华安黄金ETF联接C"},
    {"code": "007077", "name": "汇添富中证医药ETF联接C"},
    {"code": "007874", "name": "华宝科技ETF联接C"},
    {"code": "012323", "name": "华宝医疗ETF联接C"},
    {"code": "004643", "name": "南方中证房地产ETF发起联接C"},
    {"code": "019405", "name": "华夏中证全指运输ETF发起式联接C"},
    {"code": "027738", "name": "银华中证全指电力公用事业ETF发起式联接C"},
    {"code": "024193", "name": "国投瑞银中证全指公用事业ETF发起式联接C"},
    {"code": "024195", "name": "永赢国证商用卫星通信产业ETF发起联接C"},
    {"code": "025491", "name": "平安中证卫星产业指数C"},
]
