/* fundai 前端：状态渲染 + 图表（ECharts） */
"use strict";

/* ---------- 工具 ---------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtMoney = (v) => "¥" + Number(v ?? 0).toLocaleString("zh-CN",
  { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtMoney0 = (v) => "¥" + Number(v ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
const fmtPct0 = (v) => (Number(v ?? 0) * 100).toFixed(0) + "%";
const fmtPct = (v) => (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%";
const fmtNum = (v, d = 2) => Number(v ?? 0).toFixed(d);
const clsGain = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

async function api(path, opts) {
  const r = await fetch(path, {
    method: opts?.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });
  const j = await r.json().catch(() => ({ ok: false, message: "响应解析失败" }));
  if (!j.ok && !j.state) throw new Error(j.message || ("HTTP " + r.status));
  return j;
}

function toast(msg, type = "info") {
  const box = $("toastBox");
  const el = document.createElement("div");
  el.className = "toast " + (type === "ok" ? "ok" : type === "err" ? "err" : "");
  el.innerHTML = esc(msg);
  box.appendChild(el);
  setTimeout(() => el.remove(), type === "err" ? 6000 : 3500);
}

/* ---------- 全局状态 ---------- */
let ST = null;         // /api/state
let HIST = [];         // snapshots
let RECS = [];         // records
let ORDERS = [];       // orders
let FUNDS = [];        // funds
let INDICES = [];      // 大盘指数实时行情
let DIRS = null;       // AI 方向判断命中统计（/api/news/dirstats）
let fillTarget = null; // 待录入订单

/* ---------- 加载 ---------- */
async function loadAll(quiet) {
  try {
    const [s, h, r, o, f] = await Promise.all([
      api("/api/state"), api("/api/history"), api("/api/records"),
      api("/api/orders"), api("/api/funds"),
    ]);
    ST = s.state; HIST = h.snapshots || []; RECS = r.records || [];
    ORDERS = o.orders || []; FUNDS = f.funds || [];
    renderHeader(); renderDash(); renderRecords(); renderOrders(); renderFunds();
    renderSettings(); tryRenderBt();
    // echarts 就绪后与布局稳定后各重绘一次（容器可见才真正绘制）
    whenEcharts(redrawAll);
    setTimeout(() => whenEcharts(redrawAll), 600);
  } catch (e) {
    if (!quiet) toast("加载失败：" + e.message, "err");
  }
  loadIndices();
  api("/api/news/dirstats").then(j => {
    DIRS = j.stats || null;
    if (document.querySelector("nav.tabs button.on")?.dataset.tab === "dash") {
      whenEcharts(drawAIDir);
    }
  }).catch(() => { DIRS = null; });
}

/* ---------- 头部 ---------- */
function renderHeader() {
  const mode = ST.demo ? ["badge demo", "演示数据（合成行情，只读）"]
    : ["badge live", "正式账户 · AI 给建议 / 你执行"];
  $("badgeMode").className = mode[0]; $("badgeMode").textContent = mode[1];
  const execTxt = ST.demo ? "演示（自动回放）"
    : (ST.exec_mode === "manual" ? "人工执行模式：AI 建议 → 你亲自执行 → 录入成交"
        : "模拟自动成交（仅演示流程）");
  $("badgeExec").textContent = "模式：" + execTxt;
  $("badgeSrc").textContent = "数据源：" + ST.data_source +
    (ST.api_usage ? " · 今日已用 " + ST.api_usage.calls_today + " 次" +
      (ST.api_usage.daily_limit != null ? "/" + ST.api_usage.daily_limit + " 次" : "（智兔不限/akshare免费）") : "");
  $("btnRun").style.display = ST.demo ? "none" : "";
}

/* ---------- 大盘指数条 ---------- */
async function loadIndices() {
  try {
    const j = await api("/api/indices");
    INDICES = j.indices || [];
  } catch (e) {
    INDICES = [];
  }
  renderIndices();
}

function renderIndices() {
  const box = $("idxStrip");
  if (!box) return;
  if (!INDICES.length) {
    box.innerHTML = `<div class="mut" style="padding:8px 4px">大盘指数：暂不可用（离线或接口未响应）</div>`;
    return;
  }
  box.innerHTML = INDICES.map(it => {
    const dir = it.chg_pct == null ? "flat" : (it.chg_pct > 0 ? "up" : it.chg_pct < 0 ? "down" : "flat");
    const chg = it.chg_pct == null ? "—" : fmtPct(it.chg_pct);
    const amt = it.chg_amt == null ? "" : ((it.chg_amt >= 0 ? "+" : "") + it.chg_amt.toFixed(2));
    const bench = it.benchmark ? `<span class="idx-bench" title="本策略的研判基准指数">基准</span>` : "";
    return `<div class="idx-item ${dir}">
      <div class="nm">${esc(it.name)}${bench}</div>
      <div class="px">${Number(it.price ?? 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
      <div class="chg">${chg}${amt ? " <span style='opacity:.7'>" + amt + "</span>" : ""}</div>
    </div>`;
  }).join("");
}

/* ---------- 仪表盘 ---------- */
function renderDash() {
  const t = ST.total, target = ST.target, init = ST.initial;
  $("cTargetLbl").textContent = fmtMoney0(target);
  $("cTargetTxt").textContent = fmtMoney0(target);
  $("cTotal").textContent = fmtMoney(t);
  $("cTotal").className = "v";
  const gain = ST.gain;
  const gainEl = $("cGain");
  gainEl.textContent = fmtMoney(gain) + " (" + fmtPct(ST.gain_pct) + ")";
  gainEl.className = clsGain(gain);
  $("cFees").textContent = fmtMoney(ST.fees);

  const pct = target > 0 ? Math.min(100, (t / target) * 100) : 0;
  $("pFill").style.width = pct + "%";
  $("pMark").style.left = "50%";
  $("pMarkPct").textContent = pct.toFixed(1) + "% 已达成";

  $("cGap").textContent = fmtMoney(ST.gap);
  $("cGap").className = "v " + (ST.gap <= 0 ? "down" : "up");
  const need = ST.need_daily_ret;
  if (ST.gap <= 0) {
    $("cNeedDaily").innerHTML = "🎉 已达成目标！";
  } else if (need == null) {
    $("cNeedDaily").textContent = "距截止日已无交易日";
  } else {
    const shortPct = (ST.gap / Math.max(ST.target, 1)) * 100;
    $("cNeedDaily").innerHTML = "还需日均收益 <b class='gold-txt'>" + fmtPct(need) +
      "</b>（当前缺口 " + shortPct.toFixed(1) + "% 的目标）";
  }

  $("cAlloc").textContent = [fmtMoney0(ST.cash), fmtMoney0(ST.mv_eq), fmtMoney0(ST.mv_bond)].join(" / ");
  $("cAllocPct").textContent = "现金 / 股票基金 / 债券基金";
  const nPos = ST.positions.length;
  const posBrief = nPos ? ST.positions.map(p => esc(p.name) + " " + p.shares + "份").join("、") : "暂无持仓";

  const days = ST.days_left, est = ST.est_trading_days;
  $("cPeriod").innerHTML = ST.start + " → " + ST.end + "<br><span class='mut small'>" + posBrief + "</span>";
  $("cDaysLeft").textContent = days > 0
    ? "剩余 " + days + " 天（约 " + est + " 个交易日）"
    : "实验期已结束";

  const lr = ST.last_record;
  if (lr) {
    $("cViewTitle").innerHTML = viewChip(lr.view_title, lr.market_score) + " " +
      esc(lr.view_title || "");
    $("cViewMeta").textContent = (lr.date || "") + " 评分" +
      (lr.market_score >= 0 ? "+" : "") + lr.market_score +
      " · " + (lr.source || "") + (lr.index_close ? " · 收盘 " + lr.index_close : "");
  } else {
    $("cViewTitle").textContent = "尚未运行";
    $("cViewMeta").textContent = "收盘后点击右上角按钮，AI 会给出今天的操作建议";
  }

  drawMain(); drawPie(); drawScore(); drawLatest(); drawHints(); renderPnL();
}

/* 当前持仓盈亏：汇总卡片 + 明细表格（成本 vs 市值） */
function renderPnL() {
  const summary = $("pnlSummary");
  const tb = $("pnlTable");
  if (!summary || !tb) return;
  const pos = ST.positions || [];
  if (!pos.length) {
    summary.innerHTML = "";
    tb.innerHTML = `<div class="mut">暂无持仓。运行「立即运行今日研判」后，AI 会给出发车/加仓建议。</div>`;
    return;
  }
  const totalValue = pos.reduce((s, p) => s + (p.value || 0), 0);
  const totalCost = pos.reduce((s, p) => s + (p.cost || 0), 0);
  const totalPnl = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? totalPnl / totalCost : 0;
  summary.innerHTML = `
    <div class="pnl-card"><div class="k">持仓总市值</div><div class="v">${fmtMoney(totalValue)}</div></div>
    <div class="pnl-card"><div class="k">持仓总成本</div><div class="v">${fmtMoney(totalCost)}</div></div>
    <div class="pnl-card"><div class="k">总浮盈亏</div><div class="v ${clsGain(totalPnl)}">${fmtMoney(totalPnl)}</div></div>
    <div class="pnl-card"><div class="k">总收益率</div><div class="v ${clsGain(totalPnl)}">${fmtPct(totalPnlPct)}</div></div>`;
  tb.innerHTML = `<table><thead><tr>
    <th>基金</th><th>类型</th><th>份额</th><th>成本</th><th>市值</th><th>浮盈亏</th><th>收益率</th>
  </tr></thead><tbody>` + pos.map(p => {
    const pnl = p.pnl ?? (p.value - p.cost);
    const pct = p.pnl_pct ?? (p.cost > 0 ? pnl / p.cost : null);
    const g = clsGain(pnl);
    return `<tr>
      <td><b>${esc(p.name)}</b> <span class="mut small mono">${esc(p.code)}</span></td>
      <td>${p.kind === "bond" ? '<span class="blue-txt">债基</span>' : '<span class="up">股基/指数</span>'}</td>
      <td class="mono">${fmtNum(p.shares, 2)}</td>
      <td class="mono">${fmtMoney(p.cost)}</td>
      <td class="mono">${fmtMoney(p.value)}</td>
      <td class="mono ${g}"><b>${fmtMoney(pnl)}</b></td>
      <td class="mono ${g}">${pct == null ? "—" : fmtPct(pct)}</td>
    </tr>`;
  }).join("") + `</tbody></table>`;
}

function viewChip(keyOrTitle, score) {
  let key = String(keyOrTitle || "");
  if (!["hot_bull", "bull", "neutral", "bear", "hot_bear"].includes(key)) {
    key = score >= 55 ? "hot_bull" : score >= 25 ? "bull" : score >= -25 ? "neutral"
      : score >= -55 ? "bear" : "hot_bear";
  }
  return "<span class='view-chip " + key + "'>" + esc(keyOrTitle) + "</span>";
}

function echartsOK() { return typeof echarts !== "undefined"; }

function elVisible(el) {
  return !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
}

/* 关键修复：容器不可见（隐藏页签/宽度为0）时不初始化，避免图表“挤成一小块”；
   由 redrawAll() 在页签切换、脚本就绪、窗口变化后重新绘制。 */
function chartInit(id) {
  if (!echartsOK()) return null;
  const el = $(id);
  if (!elVisible(el)) return null;
  const c = echarts.getInstanceByDom(el) || echarts.init(el);
  if (!elVisible(el)) return null; // 初始化后再次确认
  return c;
}

function whenEcharts(cb) {
  if (typeof echarts !== "undefined") { cb(); return; }
  if (window.__echartsFailed) { chartsUnavailable(); return; }
  let n = 0;
  const t = setInterval(() => {
    if (typeof echarts !== "undefined") { clearInterval(t); cb(); }
    else if (window.__echartsFailed || ++n > 100) { // 最多等 10s（原来 20s）
      clearInterval(t);
      chartsUnavailable();
    }
  }, 100);
}

/* ECharts 无法加载（本地无 vendor、CDN 又离线）时：不傻等，
   改为在图表容器里输出文字摘要，并提示如何恢复。 */
function chartsUnavailable() {
  if (window.__chartsFallbackShown) return;
  window.__chartsFallbackShown = true;
  const note = (id, txt) => { const el = $(id); if (el) el.innerHTML = txt; };
  const base = `<div class="mut" style="line-height:2">
      <div>⚠️ 图表组件未加载（离线且未内置 ECharts），已切换文字模式。</div>
      <div class="small">联网刷新页面，或运行 <code>python app.py vendor-echarts</code> 本地化一次即可恢复图表。</div></div>`;
  note("chMain", base + (ST ? `<div class="small">最新总资产 <b>${fmtMoney(ST.total)}</b>（起始 ${fmtMoney(ST.initial)}，${fmtPct(ST.gain_pct)}；历史快照 ${HIST.length} 个交易日）</div>` : ""));
  note("chPie", base + (ST ? `<div class="small">现金 ${fmtMoney0(ST.cash)} / 股基 ${fmtMoney0(ST.mv_eq)} / 债基 ${fmtMoney0(ST.mv_bond)}</div>` : ""));
  const lastRec = ST?.last_record;
  note("chScore", base + (lastRec ? `<div class="small">最近一次研判（${esc(lastRec.date || "")}）：评分 ${lastRec.market_score >= 0 ? "+" : ""}${lastRec.market_score} · ${esc(lastRec.view_title || "")}</div>` : ""));
  note("chAIDir", base + `<div class="small">AI 方向命中率：见「消息与进化」页统计</div>`);
  if (BT_LAST) {
    note("chBt", base + `<div class="small">回测 ${esc(BT_LAST.start)} → ${esc(BT_LAST.end)}：期末 ${fmtMoney(BT_LAST.end_value)}（${fmtPct(BT_LAST.ret_pct)}）· 回撤 ${(-(BT_LAST.max_dd_pct || 0) * 100).toFixed(1)}% · ${BT_LAST.trades} 笔</div>`);
  }
  if (!window.__chartsToastShown) {
    window.__chartsToastShown = true;
    toast("图表组件未加载（离线），已显示文字摘要；联网刷新或 python app.py vendor-echarts", "err");
  }
}

/* 当前可见页签对应的图表全部重绘（幂等：无实例则创建，已有则更新并 resize） */
function redrawAll() {
  if (!echartsOK()) return;
  const page = document.querySelector("nav.tabs button.on")?.dataset.tab;
  if (page === "dash") {
    drawMain(); drawPie(); drawScore(); drawAIDir();
  } else if (page === "backtest") {
    drawBtChart();
  }
  ["chMain", "chPie", "chScore", "chAIDir", "chBt"].forEach(id => {
    const el = $(id);
    if (el && elVisible(el)) {
      const c = echarts.getInstanceByDom(el);
      if (c) c.resize();
    }
  });
}

function axisCommon() {
  return {
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 24, top: 30, bottom: 36 },
    legend: { top: 4, textStyle: { color: "#8d99ae" } },
  };
}

function drawMain() {
  const c = chartInit("chMain");
  if (!c) return;
  const init = ST.initial;
  const xs = HIST.map(h => h.date);
  const tot = HIST.map(h => h.total);
  // 沪深300 同起点对比
  const firstIdx = HIST.find(h => h.index_close != null);
  const idxScale = firstIdx ? init / firstIdx.index_close : 0;
  const idxVals = HIST.map(h => (h.index_close != null ? h.index_close * idxScale : null));
  const lastSnap = HIST[HIST.length - 1];
  if (lastSnap && Math.abs(lastSnap.total - ST.total) > 0.005 && !ST.demo) {
    // 今日净值已出但快照未生成时，补一个今日点位
    xs.push(ST.today); tot.push(ST.total); idxVals.push(null);
  } else if (!HIST.length) {
    xs.push(ST.today); tot.push(ST.total); idxVals.push(null);
  }
  c.setOption({
    ...axisCommon(),
    color: ["#4e9cff", "#7d8597"],
    series: [
      {
        name: "账户总资产", type: "line", data: tot.map((v, i) => [xs[i], v]),
        smooth: true, showSymbol: false, lineStyle: { width: 2.5 },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: "rgba(78,156,255,.35)" },
                        { offset: 1, color: "rgba(78,156,255,0)" }] } },
        markLine: { symbol: "none", data: [{ yAxis: ST.target }],
          lineStyle: { color: "#f7b731", type: "dashed" },
          label: { formatter: "目标 ¥" + ST.target, color: "#f7b731" } },
      },
      {
        name: "沪深300(同起点)", type: "line",
        data: idxVals.map((v, i) => v == null ? null : [xs[i], v]),
        smooth: true, showSymbol: false, lineStyle: { width: 1.2, type: "dotted" },
      },
    ],
    xAxis: { type: "category", data: xs, axisLine: { lineStyle: { color: "#24304a" } },
      axisLabel: { color: "#8d99ae" } },
    yAxis: { type: "value", scale: true,
      axisLabel: { color: "#8d99ae", formatter: v => "¥" + v },
      splitLine: { lineStyle: { color: "#1c2740" } } },
  });
}

function drawPie() {
  const c = chartInit("chPie");
  if (!c) return;
  const data = [
    { name: "股票基金", value: ST.mv_eq },
    { name: "债券基金", value: ST.mv_bond },
    { name: "现金", value: ST.cash },
  ].filter(d => d.value > 0.001);
  c.setOption({
    tooltip: { trigger: "item", formatter: p => p.name + "：¥" + p.value.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) + "（" + p.percent + "%）" },
    color: ["#ff4d4f", "#4e9cff", "#f7b731"],
    series: [{
      type: "pie", radius: ["45%", "72%"], center: ["50%", "52%"],
      label: { color: "#dfe6f3", formatter: "{b}\n{d}%" },
      itemStyle: { borderColor: "#0b0f17", borderWidth: 3 },
      data,
    }],
  });
}

function drawScore() {
  const c = chartInit("chScore");
  if (!c) return;
  const recs = [...RECS].reverse();
  const color = v => v >= 55 ? "#ff4d4f" : v >= 25 ? "#ff8a8c" : v >= -25 ? "#8d99ae"
    : v >= -55 ? "#5fdba5" : "#21bf73";
  c.setOption({
    ...axisCommon(),
    tooltip: { trigger: "axis", formatter: ps => {
        const p = ps[0];
        return esc(String(p.name)) + "<br/>评分 " + p.value +
          (p.value >= 0 ? "（偏多）" : "（偏空）");
      } },
    series: [{
      type: "bar", data: recs.map(r => ({
        value: r.market_score,
        itemStyle: { color: color(r.market_score), borderRadius: [2, 2, 0, 0] },
      })),
      markLine: { symbol: "none", data: [{ yAxis: 0 }], lineStyle: { color: "#5b6b85" } },
    }],
    xAxis: { type: "category", data: recs.map(r => r.date),
      axisLabel: { color: "#8d99ae", showMaxLabel: false } },
    yAxis: { type: "value", min: -100, max: 100,
      axisLabel: { color: "#8d99ae" }, splitLine: { lineStyle: { color: "#1c2740" } } },
  });
}

function drawAIDir() {
  const c = chartInit("chAIDir");
  const hist = (DIRS?.history || []).slice().reverse(); // 时间升序
  const s = DIRS || {};
  const txt = (d) => d.dir === "bull" ? "看多" : d.dir === "bear" ? "看空" : "中性";
  // 底部说明文字
  const el = $("aiDirSummary");
  if (el) {
    const rate = s.hit_rate == null ? "—" : fmtPct0(s.hit_rate);
    el.innerHTML = (s.total
      ? `已结算 <b>${s.total}</b> 次 AI 方向判断 · 总命中率 <b>${rate}</b>` +
        `（看多 ${s.by_dir?.bull?.n || 0}次/中${s.by_dir?.bull?.hit || 0} · 看空 ${s.by_dir?.bear?.n || 0}次/中${s.by_dir?.bear?.hit || 0} · 中性 ${s.by_dir?.neutral?.n || 0}次/中${s.by_dir?.neutral?.hit || 0}）`
      : "运行几次「立即运行今日研判」后，AI 会在此自动积累并结算方向命中曲线。")
      + "｜<span style='opacity:.75'>柱=次日实际涨跌（红涨绿跌）· 紫线=近10次滚动命中率</span>";
  }
  if (!c) return;
  if (!hist.length) {
    c.clear();
    return;
  }
  const xs = hist.map(h => h.date);
  const chg = hist.map(h => (h.next_chg == null ? null : +(h.next_chg * 100).toFixed(2)));
  const W = 10;
  const rate10 = hist.map((_, i) => {
    if (i < W - 1) return null;
    const seg = hist.slice(i - W + 1, i + 1);
    return +(seg.reduce((a, x) => a + (x.hit ? 1 : 0), 0) / W * 100).toFixed(1);
  });
  const dirOf = { bull: "▲看多", bear: "▼看空", neutral: "◆中性" };
  c.setOption({
    ...axisCommon(),
    tooltip: { trigger: "axis", formatter: ps => {
        const i = ps[0].dataIndex, x = hist[i];
        if (!x) return "";
        const hitTxt = x.hit ? '<span style="color:#4adf9a">命中 ✓</span>'
          : '<span style="color:#ff7d80">未中 ✗</span>';
        return `${esc(x.date)} AI:${dirOf[x.dir] || x.dir}（信心 ${Math.round((x.confidence || 1) * 100)}%）<br/>` +
          `次日实际 ${fmtPct(x.next_chg)} → ${hitTxt}`;
      } },
    color: ["#4e9cff", "#a78bfa"],
    legend: { top: 4, textStyle: { color: "#8d99ae" } },
    grid: { left: 60, right: 54, top: 34, bottom: 36 },
    series: [
      {
        name: "次日实际涨跌", type: "bar", data: chg.map((v, i) => ({
          value: v, itemStyle: {
            color: v == null ? "transparent" : v > 0 ? "rgba(255,99,116,.85)"
              : v < 0 ? "rgba(51,209,132,.85)" : "rgba(141,153,174,.5)",
            borderRadius: [3, 3, 0, 0] },
        })), barWidth: "46%",
      },
      {
        name: "近10次滚动命中率", type: "line", yAxisIndex: 1, data: rate10,
        smooth: true, symbol: "none", lineStyle: { width: 2.4, color: "#a78bfa" },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: "rgba(167,139,250,.30)" },
                        { offset: 1, color: "rgba(167,139,250,0)" }] } },
        markLine: { symbol: "none", data: [{ yAxis: 50 }],
          lineStyle: { color: "#5b6b85", type: "dashed" },
          label: { formatter: "50% 基准", color: "#5b6b85" } },
      },
    ],
    xAxis: { type: "category", data: xs, axisLine: { lineStyle: { color: "#24304a" } },
      axisLabel: { color: "#8d99ae", showMaxLabel: false } },
    yAxis: [
      { type: "value", scale: true, axisLabel: { color: "#8d99ae", formatter: v => v + "%" },
        splitLine: { lineStyle: { color: "#1c2740" } } },
      { type: "value", min: 0, max: 100, axisLabel: { color: "#8d99ae", formatter: "{value}%" },
        splitLine: { show: false } },
    ],
  });
}

function drawLatest() {
  const lr = ST.last_record;
  const box = $("latestView");
  if (!lr) {
    box.innerHTML = `<div class="mut">还没有研判记录。每天 A 股收盘后（约 15:00 后，净值一般 20:00 后公布）点右上角「立即运行今日研判」。</div>`;
    return;
  }
  box.innerHTML = `
    <div style="margin-bottom:8px">
      ${viewChip(lr.view_title, lr.market_score)}
      <span class="mut small">${esc(lr.date)} · 评分 ${lr.market_score >= 0 ? "+" : ""}${lr.market_score}
      ${lr.index_close ? "· 指数收盘 " + lr.index_close + (lr.market_chg != null ? "（" + fmtPct(lr.market_chg) + "）" : "") : ""}
      · ${esc(lr.source)}</span>
    </div>
    <pre class="line">${esc(lr.analysis)}</pre>
    ${lr.decision ? `<div class="hint blue"><b>今日指令摘要：</b><br>${esc(lr.decision)}</div>` : ""}`;
}

function drawHints() {
  const box = $("hints");
  const lines = [];
  if (ST.demo) lines.push("当前打开的是【演示库】：行情为合成数据，仅供体验界面与流程。开始真实实验请用正式库（python app.py serve）。");
  else if (ST.exec_mode === "manual") lines.push("人工执行模式：每天收盘后运行一次，AI 输出“今日建议”（默认按此执行即可）；你在支付宝/天天基金操作后，到「指令与成交」页点“已执行”录入实际成交，账本才与真实资金一致。");
  else lines.push("模拟自动成交模式（仅演示）：昨日建议会自动按最新净值成交。真实资金请把 config.json 的 exec_mode 改为 manual。");
  const ap = ST.api_usage;
  lines.push("数据通道提示：" + (ap
    ? "今天已调用 " + ap.calls_today + " 次" + (ap.daily_limit != null ? "/" + ap.daily_limit + "（智兔）" : "（akshare/东财 免费不限）")
    : "？") + "；日常运行只需少量调用，界面轮询只读缓存，不会烧配额。");
  (ST.warnings || []).forEach(w => lines.push(w));
  if (!lines.length) lines.push("一切正常。收盘后运行今日研判即可。");
  box.innerHTML = lines.map(l => `<div class="mut" style="padding:3px 0">· ${esc(l)}</div>`).join("");
}

/* ---------- 每日研判 ---------- */
function renderRecords() {
  const box = $("recList");
  if (!RECS.length) { box.innerHTML = '<div class="mut">暂无记录</div>'; return; }
  box.innerHTML = `<div class="tl">` + RECS.map(r => {
    const key = viewKeyOf(r.market_score);
    const names = { hot_bull: "强烈看多", bull: "谨慎看多", neutral: "中性震荡", bear: "谨慎看空", hot_bear: "强烈看空" };
    const chipText = names[r.view_title] ? r.view_title : (r.view_title || names[key]);
    return `
    <div class="tl-item ${key}">
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
        <b>${esc(r.date)}</b> <span class="view-chip ${key}">${esc(chipText)}</span>
        <span class="mut small">评分 ${r.market_score >= 0 ? "+" : ""}${r.market_score}
        ${r.index_close ? "· 收盘 " + r.index_close + (r.market_chg != null ? "（" + fmtPct(r.market_chg) + "）" : "") : ""}
        · ${esc(r.source)}</span>
      </div>
      <details style="margin-top:6px">
        <summary class="mut small" style="cursor:pointer">查看全文</summary>
        <pre class="line">${esc(r.analysis)}</pre>
        ${r.decision ? `<div class="hint blue small">${esc(r.decision)}</div>` : ""}
      </details>
    </div>`;
  }).join("") + `</div>`;
}

function viewKeyOf(score) {
  return score >= 55 ? "hot_bull" : score >= 25 ? "bull" : score >= -25 ? "neutral"
    : score >= -55 ? "bear" : "hot_bear";
}

/* ---------- 指令与成交 ---------- */
function renderOrders() {
  const box = $("pendingBox");
  const pend = ST.pending_orders || [];
  const act = pend.filter(o => o.status === "pending");
  const subm = pend.filter(o => o.status === "submitted");
  let html = "";
  if (!act.length && !subm.length) {
    box.innerHTML = `<div class="mut">暂无待处理建议。每天收盘后运行「立即运行今日研判」，AI 会把要做的操作列在这里。</div>`;
  } else {
    if (act.length) {
      html += act.map(o => `
      <div style="border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:10px">
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
          <span class="pill" style="${o.action === 'buy' ? 'color:var(--red);border-color:rgba(255,77,79,.5)' : 'color:var(--green);border-color:rgba(33,191,115,.5)'}">
            ${o.action === "buy" ? "建议申购" : "建议赎回"}</span>
          <b>${esc(o.fund_name)} <span class="mut small mono">${esc(o.fund_code)}</span></b>
          <b class="${o.action === 'buy' ? 'up' : 'down'}">${fmtMoney(o.amount)}</b>
          <span class="mut small">建议 ${esc(o.order_date)}</span>
        </div>
        <div class="mut small" style="margin:6px 0">${esc(o.note || "")}</div>
        <div class="hint blue" style="margin:2px 0 8px">在支付宝/天天基金完成<strong>${o.action === "buy" ? "申购" : "赎回"}</strong>后点下方「确认操作」；
          系统会像支付宝一样，按你提交的时间自动推算 T+1 净值成交日，份额确认后自动入账，无需手动填净值。</div>
        <div style="display:flex; gap:8px">
          ${ST.exec_mode === "manual" && !ST.demo
            ? `<button class="btn small primary" data-confirm="${o.id}">✔ 我已在支付宝/天天基金提交</button>
               <button class="btn small" data-fill="${o.id}">录入实际成交</button>
               <button class="btn small" data-skip="${o.id}">放弃该建议</button>`
            : `<button class="btn small" data-skip="${o.id}">取消该建议</button>`}
        </div>
      </div>`).join("");
    }
    if (subm.length) {
      html += subm.map(o => `
      <div style="border:1px solid rgba(247,183,49,.5); border-radius:10px; padding:12px 14px; margin-bottom:10px; background:rgba(247,183,49,.05)">
        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
          <span class="pill" style="color:var(--gold);border-color:rgba(247,183,49,.6)">⏳ 已提交 · 待T+1确认</span>
          <b>${esc(o.fund_name)} <span class="mut small mono">${esc(o.fund_code)}</span></b>
          <b class="${o.action === 'buy' ? 'up' : 'down'}">${fmtMoney(o.amount)}</b>
        </div>
        <div class="mut small" style="margin:6px 0">
          ${esc(o.nav_value_date || "—")} 净值成交 → ${esc(o.confirm_date || "—")} 份额自动确认入账，
          到确认日后运行「立即运行今日研判」即自动完成（无需再操作）。
        </div>
        <div style="display:flex; gap:8px">
          ${!ST.demo ? `<button class="btn small" data-fill="${o.id}">录入实际成交</button>
               <button class="btn small" data-unsubmit="${o.id}">撤销提交</button>` : ""}
        </div>
      </div>`).join("");
    }
    box.innerHTML = html;
  }
  box.querySelectorAll("[data-confirm]").forEach(b => b.onclick = async () => {
    try {
      const r = await api("/api/orders/confirm", { method: "POST", body: { id: Number(b.dataset.confirm) } });
      if (!r.ok) throw new Error(r.message || "确认失败");
      toast(r.message || "已确认，等待 T+1 自动确认份额", "ok");
      loadAll(true);
    } catch (e) { toast(e.message, "err"); }
  });
  box.querySelectorAll("[data-skip]").forEach(b => b.onclick = async () => {
    try { await api("/api/orders/skip", { method: "POST", body: { id: Number(b.dataset.skip) } });
      toast("已跳过该指令", "ok"); loadAll(true);
    } catch (e) { toast(e.message, "err"); }
  });
  box.querySelectorAll("[data-fill]").forEach(b => b.onclick = () => openFill(Number(b.dataset.fill)));
  box.querySelectorAll("[data-unsubmit]").forEach(b => b.onclick = async () => {
    if (!confirm("确认撤销该笔提交？请以你在支付宝/天天基金的真实操作为准。")) return;
    try {
      const r = await api("/api/orders/skip", { method: "POST", body: { id: Number(b.dataset.unsubmit) } });
      toast(r.message || "已撤销提交", "ok"); loadAll(true);
    } catch (e) { toast(e.message, "err"); }
  });

  const rows = ORDERS;
  const tb = $("orderTable");
  if (!rows.length) { tb.innerHTML = '<div class="mut">暂无成交记录</div>'; return; }
  tb.innerHTML = `<table><thead><tr>
    <th>建议日</th><th>基金</th><th>动作</th><th>金额/份额</th><th>执行日</th><th>净值</th><th>费用</th><th>状态</th>
  </tr></thead><tbody>` + rows.map(o => {
    const fund = (FUNDS.find(f => f.code === o.fund_code) || {});
    return `<tr>
      <td>${esc(o.order_date)}</td>
      <td>${esc(o.fund_name || fund.name || o.fund_code)}</td>
      <td>${o.action === "buy" ? '<span class="up">申购</span>' : '<span class="down">赎回</span>'}</td>
      <td>${fmtMoney(o.amount)}${o.fill_shares ? " → " + fmtNum(o.fill_shares, 2) + " 份" : ""}</td>
      <td>${esc(o.fill_date || "—")}</td>
      <td>${o.fill_nav ? fmtNum(o.fill_nav, 4) : "—"}</td>
      <td>${o.fill_fee ? fmtMoney(o.fill_fee) : "—"}</td>
      <td>${o.status === "filled" ? '<span class="down">已成交</span>'
          : o.status === "submitted" ? '<span class="gold-txt">待确认</span>'
          : o.status === "skipped" ? '<span class="mut">已跳过</span>'
          : '<span class="gold-txt">待执行</span>'}</td>
    </tr>`;
  }).join("") + `</tbody></table>`;
}

function openFill(oid) {
  const o = (ST.pending_orders || []).find(x => x.id === oid) || ORDERS.find(x => x.id === oid);
  if (!o) return;
  fillTarget = o;
  $("fillTitle").textContent = (o.action === "buy" ? "录入申购成交" : "录入赎回成交") + " · " + o.fund_name + "（" + o.fund_code + "）";
  $("fAmtLabel").textContent = o.action === "buy" ? "扣款总额（元）" : "赎回金额（元，可不填）";
  $("fDate").value = ST.today;
  $("fAmount").value = o.amount;
  $("fShares").value = "";
  $("fNav").value = "";
  $("fFee").value = "";
  $("fillMask").classList.add("on");
}
$("fCancel").onclick = () => $("fillMask").classList.remove("on");
$("fOk").onclick = async () => {
  if (!fillTarget) return;
  const map = { fAmount: "fill_amount", fShares: "fill_shares", fNav: "fill_nav", fFee: "fill_fee" };
  const body = { id: fillTarget.id, fill_date: $("fDate").value.trim() || ST.today };
  Object.keys(map).forEach(id => {
    const v = $(id).value.trim();
    if (v !== "") body[map[id]] = parseFloat(v);
  });
  try {
    const r = await api("/api/orders/fill", { method: "POST", body });
    if (!r.ok) throw new Error(r.message || "录入失败");
    toast("成交已录入 ✓（净值 " + (r.order?.fill_nav ?? "—") + "）", "ok");
    if (r.nav_warn) toast(r.nav_warn, "err");
    $("fillMask").classList.remove("on");
    loadAll(true);
  } catch (e) { toast(e.message, "err"); }
};

/* ---------- 基金池 ---------- */
function renderFunds() {
  const pi = ST.pool_info || {};
  $("poolBadge").textContent = "当前 " + pi.count + " 只（权益 " + pi.equity + " + 债基 " + pi.bond + "）";
  $("poolNote").textContent =
    "来源：" + (pi.source === "dynamic"
      ? "动态筛选（按20日动量从候选中重建，更新于 " + esc(pi.updated || "—") + "）"
      : "内置配置（尚未执行动态筛选）") +
    " · 系统保证任何时候 ≥ " + (pi.min_total || 8) + " 只 · 当前持仓与债基永远保留" +
    " · 可点右上“立即重建备选池”手动刷新（约1分钟，消耗与候选数量相当的API配额）";
  const btn = $("btnPool");
  btn.style.display = ST.demo ? "none" : "";
  const tb = $("fundTable");
  tb.innerHTML = `<table><thead><tr>
    <th>代码</th><th>名称</th><th>类型/角色</th><th>20日动量</th><th>买入费率</th><th>赎回费(≥7天/&lt;7天)</th><th>最新净值</th><th>状态</th>
  </tr></thead><tbody>` + FUNDS.map(f => {
    const roleTxt = f.kind === "equity"
      ? (f.role === "attack" ? "进攻" : f.role === "benchmark" ? "基准" : f.role === "dynamic" ? "动态" : "权益")
      : "防御底仓";
    return `
    <tr>
      <td class="mono">${esc(f.code)}</td>
      <td>${esc(f.name || f.code)}</td>
      <td>${f.kind === "equity" ? '<span class="up">股基/指数</span>' : '<span class="blue-txt">债基</span>'}
        <span class="pill">${roleTxt}</span></td>
      <td class="mono">${f.mom20 != null ? '<span class="' + (f.mom20 >= 0 ? "up" : "down") + '">' + fmtPct(f.mom20) + "</span>" : "—"}</td>
      <td>${f.buy_rate ? (f.buy_rate * 100) + "%" : "0"}</td>
      <td>${(f.sell_rate_ge7d * 100) + "% / " + (f.sell_rate_lt7d * 100) + "%"}</td>
      <td class="mono">${f.nav ? fmtNum(f.nav, 4) + " <span class='mut small'>(" + esc(f.nav_date || "") + ")</span>" : "—"}</td>
      <td>${f.ok ? '<span class="down">正常</span>' : '<span class="mut">不可用</span>'}</td>
    </tr>`;
  }).join("") + `</tbody></table>`;
}

$("btnPool").onclick = async () => {
  const btn = $("btnPool");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "筛选中…（约1分钟，勿关页面）";
  try {
    const r = await api("/api/pool/refresh", { method: "POST", body: {} });
    if (!r.ok) throw new Error((r.result && r.result.message) || r.message || "重建失败");
    toast("✅ " + r.result.message, "ok");
    await loadAll(true);
  } catch (e) {
    toast("重建失败：" + e.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
};

/* ---------- 设置 ---------- */
function renderSettings() {
  const cfg = ST.config || {};
  const st = cfg.strategy || {};
  const idx = cfg.index || {};
  const llm = (cfg.llm_enabled ? "启用" : "关闭") + (cfg.llm_provider ? "（" + esc(cfg.llm_provider) + "）" : "");
  $("setAccount").innerHTML = `<table><tbody>
    <tr><td>账户名称</td><td>${esc(ST.name)}</td></tr>
    <tr><td>起始资金</td><td>${fmtMoney(ST.initial)}</td></tr>
    <tr><td>挑战目标</td><td class="gold-txt">${fmtMoney(ST.target)}（+${((ST.target / ST.initial - 1) * 100).toFixed(0)}%）</td></tr>
    <tr><td>实验周期</td><td>${esc(ST.start)} → ${esc(ST.end)}（${esc(ST.days_left)} 天剩余）</td></tr>
    <tr><td>执行模式</td><td>${ST.exec_mode === "manual" ? "人工执行：AI 出建议 → 你亲自执行 → 录入成交" : "模拟自动成交（演示）"}</td></tr>
    <tr><td>数据源与API配额</td><td>${esc(ST.data_source)}${ST.api_usage ? " · 今日已用 " + ST.api_usage.calls_today + " 次" + (ST.api_usage.daily_limit != null ? "/" + ST.api_usage.daily_limit + " 次" : "（智兔不限/akshare免费）") : ""}</td></tr>
    <tr><td>今日</td><td>${esc(ST.today)}</td></tr>
  </tbody></table>`;
  $("setStrategy").innerHTML = `<table><tbody>
    <tr><td>大盘研判基准</td><td>${esc(idx.name || "沪深300")}（智兔代码 ${esc(idx.zhitu_code || "—")} / 东财 secid ${esc(idx.eastmoney_secid || "—")}）</td></tr>
    <tr><td>总权益仓位公式</td><td>${fmtPct0(st.eq_base)} + ${(st.eq_slope || 0.005) * 1000}‰×评分（下限 ${Math.round((st.eq_floor || 0) * 100)}%，封顶 ${Math.round((st.eq_cap || 0.95) * 100)}%）</td></tr>
    <tr><td>进攻标的选择</td><td>动态备选池（≥15 只：14 进攻 + 1 沪深300基准 + 1 债基，每周重建）内按 ${st.mom_window || 20} 日动量 + 多因子选 top ${st.top_n ?? 3} 只、同主题 ≤ ${st.theme_max ?? 1}（配置见 config.json → screening）</td></tr>
    <tr><td>换基门槛</td><td>新标的动量领先 ≥ ${Math.round((st.rotate_gap || 0.05) * 100)} 个百分点才轮动；默认走<b>『基金转换』</b>：赎回+申购同日配对下单，平台一次转换完成，无“赎回到账再买”空窗（config：strategy.use_fund_convert=false 则退回两步走）</td></tr>
    <tr><td>调仓触发阈值</td><td>总仓位与目标相差 ≥ ${Math.round((st.regime_step || 0.12) * 100)} 个百分点才整体加减仓</td></tr>
    <tr><td>债基现金打理</td><td>现金 &gt; ${Math.round((st.bond_buy_floor || 0.2) * 100)}% 且观点不偏空时买债基；债基上限 ${Math.round((st.max_bond_weight || 0.45) * 100)}%</td></tr>
    <tr><td>7天锁定期</td><td>持有不足 ${st.min_hold_days || 7} 天的份额禁止赎回（避开 1.5% 惩罚性费率）</td></tr>
    <tr><td>消息面情绪</td><td>东财快讯利好/利空关键词情绪（相关性过滤 + 净情绪阻尼 ${st.news_amp ?? 8}），按 ${Math.round((st.news_weight ?? 0.35) * 100)}% 权重与量化分合成</td></tr>
    <tr><td>止损/止盈线（风控）</td><td>单只浮亏≤${fmtPct(st.risk?.fund_stop_loss_pct ?? -0.08)}清仓；浮盈≥${fmtPct(st.risk?.fund_take_profit_pct ?? 0.1)}止盈一半、≥${fmtPct(st.risk?.fund_take_profit_full_pct ?? 0.25)}全部落袋；总资产较本金亏≥${fmtPct(st.risk?.portfolio_stop_pct ?? -0.15)}→组合止损（权益≤25%）；自峰值回撤≥${fmtPct(st.risk?.peak_trailing_pct ?? 0.1)}且评分&lt;${st.risk?.reentry_score ?? 25}→跟踪止盈。触发后进入 ${st.risk?.rearm_days ?? 7} 个交易日防御冷却（只减不加）；冷却期内评分 &gt; ${st.risk?.rearm_bull_score ?? 50} 或再触发单只止损/止盈时例外放行、可直接调仓</td></tr>
    <tr><td>LLM 深度研判</td><td>${llm}（config.json 填入 api_key 并 enabled=true 后，每日研判全文由大模型撰写）</td></tr>
  </tbody></table>`;
  $("dailyGuide").textContent =
`1）每天 A 股收盘后（15:00 后；基金净值通常 20:00~24:00 公布），运行一次：
     python app.py run-daily
   或网页点右上角【立即运行今日研判】。
2）AI 会：复盘当日指数与技术面 → 打分 → 从进攻池（科创50/半导体/证券/沪深300 C类）
   选出当前 20 日动量最强的基金 → 输出“今日建议”。
3）【时间关键】建议在收盘后生成，对应的成交时点 = 下一交易日：
   - 申购：下一交易日 15:00 前在支付宝提交 → 按当日净值成交（T+1 确认），确认后持有
     ≥7 个自然日再赎回（否则 1.5% 惩罚费）；C 类第 8 天起赎回费为 0。
   - 赎回/转换：下一交易日 15:00 前提交 → 按当日净值确认；资金到账债基约 T+1、权益基金
     一般 T+1~T+3（以支付宝显示为准）。轮动默认走【基金转换】：赎回与申购同日配对，
     平台一次转换完成、无“等资金到账”空窗；不支持转换的配对才退回两步走。
   15:00 后提交会自动顺延到再下一交易日；法定节假日同样顺延。
4）每笔执行后回到网页【指令与成交】点“已执行”，录入实际扣款/份额/净值，
   账本才与真实资金一致（卖出单同理：资金到账后录入）。
5）查看进度：python app.py serve → http://127.0.0.1:8787
   想先离线看效果：python app.py demo && python app.py serve --demo（合成数据，只读）。
6）数据源默认 AKShare（基金净值，免费无配额；如未安装会自动降级）+ 智兔数服（指数，
   Token 已在 config.json）兜底东财；程序带缓存节流，日常每天只需数次调用。
7）可选自动提醒：Windows 任务计划每天 20:40 运行（示例，路径按实际改）：
     schtasks /Create /TN fundai_daily /TR "cmd /c cd /d F:\\DSHtemporary\\fund200ai ^&^& python app.py run-daily >> data\\daily.log" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:40`;
}

/* ---------- 回测 ---------- */
let btShown = false;
async function tryRenderBt() {
  if (btShown) return;
  try {
    const r = await api("/api/backtest");
    if (r.last && r.last.ok) { renderBt(r.last); btShown = true; }
  } catch (e) { /* 忽略 */ }
}

$("btnBt").onclick = async () => {
  const btn = $("btnBt");
  btn.disabled = true; $("btLoading").style.display = "";
  try {
    const r = await api("/api/backtest", {
      method: "POST",
      body: { months: Number($("btMonths").value), source: $("btSource").value },
    });
    renderBt(r.result);
    btShown = true;
    whenEcharts(redrawAll);
    toast("回测完成（数据源：" + (r.result.demo ? "演示合成数据" : "在线真实数据") + "）", "ok");
  } catch (e) {
    toast("回测失败：" + e.message, "err");
  } finally {
    btn.disabled = false; $("btLoading").style.display = "none";
  }
};

let BT_LAST = null;

function renderBt(res) {
  BT_LAST = res;
  $("btResult").style.display = "";
  const init = res.initial, end = res.end_value, ret = res.ret_pct;
  const dd = res.max_dd_pct;
  $("btCards").innerHTML = `
    <div class="card"><div class="k">回测区间</div><div class="v small">${esc(res.start)} → ${esc(res.end)}</div>
      <div class="sub">${res.days} 个交易日 · ${res.demo ? "演示合成数据" : "在线真实数据"}</div></div>
    <div class="card"><div class="k">期末总资产</div><div class="v ${end >= init ? "up" : "down"}">${fmtMoney(end)}</div>
      <div class="sub">起始 ${fmtMoney(init)} · 目标 ${fmtMoney(res.target)}</div></div>
    <div class="card"><div class="k">区间收益率</div><div class="v ${clsGain(ret)}">${fmtPct(ret)}</div>
      <div class="sub">${res.goal_hit ? "🎉 达成目标" : "未达成（还需 " + fmtMoney(Math.max(0, res.target - end)) + "）"}</div></div>
    <div class="card"><div class="k">最大回撤</div><div class="v down">${(-dd * 100).toFixed(1)}%</div>
      <div class="sub">成交 ${res.trades} 笔 · 手续费 ${fmtMoney(res.fees)}</div></div>`;
  $("btNote").innerHTML = "说明：回测与实盘共用同一套引擎与规则，手续费、7天锁定期、T+1 生效均与实盘一致。" +
    (res.demo ? "本次使用的是<b>合成演示数据</b>（本机无法访问在线行情时自动降级）。" : "本次使用在线真实净值数据。") +
    "<br>⚠️ 历史回测结果绝不代表未来收益。半年 +50% 需市场显著上涨，属于较高目标，请把它当视频实验看待。";
  // 图表交给 redrawAll 在“页签可见 + echarts 就绪”时绘制
  whenEcharts(redrawAll);
}

function drawBtChart() {
  const res = BT_LAST;
  if (!res) return;
  const c = chartInit("chBt");
  if (!c) return;
  const init = res.initial;
  const xs = res.series.map(s => s.date);
  const firstIdx = res.series.find(s => s.index_close != null);
  const scale = firstIdx ? init / firstIdx.index_close : 0;
  c.setOption({
    ...axisCommon(),
    color: ["#4e9cff", "#7d8597"],
    series: [
      { name: "回测资产", type: "line", data: res.series.map((s, i) => [xs[i], s.total]),
        smooth: true, showSymbol: false, areaStyle: { color: "rgba(78,156,255,.2)" },
        markLine: { symbol: "none", data: [{ yAxis: res.target }],
          lineStyle: { color: "#f7b731", type: "dashed" },
          label: { formatter: "目标 ¥" + res.target, color: "#f7b731" } } },
      { name: "沪深300(同起点)", type: "line",
        data: res.series.map((s, i) => s.index_close == null ? null : [xs[i], s.index_close * scale]),
        showSymbol: false, lineStyle: { width: 1.2, type: "dotted" } },
    ],
    xAxis: { type: "category", data: xs, axisLabel: { color: "#8d99ae" } },
    yAxis: { type: "value", scale: true, axisLabel: { color: "#8d99ae", formatter: v => "¥" + v },
      splitLine: { lineStyle: { color: "#1c2740" } } },
  });
  c.resize();
}

/* ---------- 事件 ---------- */
document.querySelectorAll("nav.tabs button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("nav.tabs button").forEach(x => x.classList.remove("on"));
    document.querySelectorAll(".page").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    $("tab-" + b.dataset.tab).classList.add("on");
    // 页签刚变为可见时重绘对应图表（修复隐藏容器导致图表被“挤成一小块”）
    whenEcharts(redrawAll);
    setTimeout(redrawAll, 120);
    if (b.dataset.tab === "backtest") tryRenderBt();
    if (b.dataset.tab === "news") loadNewsTab();
  };
});

$("btnRun").onclick = async () => {
  const btn = $("btnRun");
  btn.disabled = true; btn.textContent = "运行中…";
  try {
    const r = await api("/api/run-daily", { method: "POST", body: {} });
    if (r.status === "noop") toast(r.message || "今日已研判", "ok");
    else {
      const lines = ["研判完成：" + (r.view || "") + "（评分 " + r.score + "）"];
      (r.orders || []).forEach(o => lines.push((o.action === "buy" ? "申购" : "赎回") + "指令 " + fmtMoney(o.amount_yuan)));
      (r.fills || []).forEach(f => lines.push("成交 " + (f.action === "buy" ? "买入" : "卖出") + " " + f.date));
      (r.messages || []).slice(0, 3).forEach(m => lines.push(m));
      toast(lines.join("；"), r.status === "error" ? "err" : "ok");
      if (r.status === "error") toast(r.message, "err");
    }
    loadAll(true);
  } catch (e) {
    toast("运行失败：" + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "▶ 立即运行今日研判";
  }
};

let _rszT = null;
window.addEventListener("resize", () => {
  clearTimeout(_rszT);
  _rszT = setTimeout(() => whenEcharts(redrawAll), 150);
});

/* 每 60 秒自动刷新 */
setInterval(() => { if (!document.hidden) loadAll(true); }, 60000);

loadAll(false);

/* ============================================================
   消息与进化：可视化人工筛选(价值/过滤) → 算法学习词典 →
   AI 自动方向研判自检 → 搜索补全
   ============================================================ */
let NEWS = null;          // /api/news/screen 载荷
let NF = "all";           // feed 过滤

function todayStrLocal() {
  const d = new Date();
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
const LABEL_TXT = { big_bull: "重大利好", bull: "利好", neutral: "中性", bear: "利空", big_bear: "重大利空", irrelevant: "与市场无关" };

async function loadNewsTab(dateS) {
  const d = dateS || $("nDate").value || todayStrLocal();
  try {
    const j = await api("/api/news/screen?date=" + encodeURIComponent(d));
    NEWS = j.news;
    renderNews();
  } catch (e) { toast("消息数据加载失败：" + e.message, "err"); }
}

function renderNews() {
  if (!NEWS) return;
  $("nDate").value = NEWS.date;
  // 顶部分数卡片
  $("nAutoScore").textContent = (NEWS.auto_score >= 0 ? "+" : "") + NEWS.auto_score;
  $("nAutoScore").className = "v " + (NEWS.auto_score > 0 ? "up" : NEWS.auto_score < 0 ? "down" : "");
  $("nAutoSub").textContent = "净情绪 " + (NEWS.auto_net >= 0 ? "+" : "") + NEWS.auto_net +
    " × 振幅 " + NEWS.cfg.news_amp + "（词典自动，供参考）";
  const h = NEWS.human;
  if (h) {
    $("nHumanScore").textContent = (h.score >= 0 ? "+" : "") + h.score;
    $("nLabels").textContent = "人工消息分已生效：净情绪 " + (h.net >= 0 ? "+" : "") + h.net +
      "（方向打标 " + h.directional + " 条 / 共 " + h.labeled + " 条）";
  } else {
    $("nHumanScore").textContent = "未打标";
    $("nLabels").textContent = "逐条打标后，算法会优先采用你的“人工消息分”";
  }
  const by = NEWS.labels.by_label || {};
  const doneN = NEWS.labels.labeled || 0;
  $("nProgress").textContent = doneN + " / " + NEWS.feed_count + " 条已打标";
  const isToday = NEWS.date === todayStrLocal();
  $("nScoreNote").innerHTML = "消息权重 " + Math.round(NEWS.cfg.news_weight * 100) +
    "% · 学习词 " + NEWS.learned_total + " 个 · " +
    (NEWS.demo ? "" : NEWS.feed_count ? NEWS.feed_count + " 条已入库" : "尚未拉取") +
    (isToday ? "" : "（非今日，只读历史）");
  renderNewsFeed();
  renderNewsDir();
  renderNewsLearn();
  renderNewsQueue();
}

/* ---------- 消息列表 ---------- */
function renderNewsFeed() {
  const items = (NEWS.feed || []).filter(it => {
    if (NF === "all") return true;
    if (NF === "unrated") return !it.user_label;
    return it.user_label ? it.user_label === NF : it.auto_label === NF;
  });
  $("nFeedCount").textContent = "共 " + NEWS.feed_count + " 条，当前显示 " + items.length + " 条";
  const box = $("nFeedList");
  if (!items.length) {
    box.innerHTML = `<div class="mut">暂无消息。先点上方【拉取今日消息】；若在线源失败，会登记到右侧“搜索补全”清单。</div>`;
    return;
  }
  box.innerHTML = items.map(it => {
    const lab = it.user_label || it.auto_label || "neutral";
    const autoTxt = it.auto_label === "neutral" ? "中性" :
      (it.auto_label === "irrelevant" ? "无关" :
        (it.auto_label === "bull" ? "利好" : "利空"));
    const autoSign = it.auto_label === "bull" ? "+" : it.auto_label === "bear" ? "−" : "";
    const sectors = (it.sectors || []).map(s => `<span class="pill">板块·${esc(s)}</span>`).join("");
    const funds = (it.funds_links || []).map(f => `<span class="pill">→ ${esc(f.name)}</span>`).join("");
    const buttons = Object.keys(LABEL_TXT).map(k =>
      `<button class="ratebtn ${it.user_label === k ? (k.includes("bull") ? "on-bull" : k.includes("bear") ? "on-bear" : k === "neutral" ? "on-neutral" : "") : ""}"
        data-rate="${it.id}" data-label="${k}">${LABEL_TXT[k]}</button>`).join("");
    return `<div class="feed-item lab-${lab === "irrelevant" ? "neutral" : lab}">
      <div style="display:flex; gap:8px; align-items:flex-start; flex-wrap:wrap">
        <span class="pill">${esc(it.time || "")} ${esc(it.source || "")}</span>
        <span class="pill" style="${it.auto_label === "bull" ? "color:#ff7d80;border-color:rgba(255,77,79,.5)" :
            it.auto_label === "bear" ? "color:#4adf9a;border-color:rgba(33,191,115,.5)" : ""}">
          自动:${autoTxt}${autoSign}</span>
        ${sectors}${funds}
      </div>
      <div style="margin:4px 0; font-weight:600; line-height:1.5">${esc(it.title || "")}</div>
      ${it.text && it.text !== it.title ? `<div class="mut small clamp2" style="margin-bottom:4px">${esc(it.text)}</div>` : ""}
      <div style="display:flex; gap:4px; flex-wrap:wrap; align-items:center">
        <span class="mut small">我的判断：</span>${buttons}
      </div>
    </div>`;
  }).join("");
}

document.querySelectorAll("[data-nf]").forEach(b => b.onclick = () => {
  NF = b.dataset.nf;
  document.querySelectorAll("[data-nf]").forEach(x => x.classList.remove("on"));
  b.classList.add("on");
  renderNewsFeed();
});

$("nFeedList").addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-rate]");
  if (!b) return;
  try {
    const r = await api("/api/news/rate", { method: "POST",
      body: { date: NEWS.date, item_id: b.dataset.rate, label: b.dataset.label } });
    NEWS = r.payload;
    toast("已打标：" + LABEL_TXT[b.dataset.label] + "；学习词典已更新（当前 " + (r.learned_total ?? NEWS.learned_total) + " 个新词）", "ok");
    renderNews();
  } catch (e) { toast("打标失败：" + e.message, "err"); }
});

$("btnNewsGo").onclick = () => loadNewsTab($("nDate").value || todayStrLocal());
$("btnNewsPull").onclick = async () => {
  const btn = $("btnNewsPull");
  btn.disabled = true; btn.textContent = "拉取中（东财+新浪）…";
  try {
    const r = await api("/api/news/pull", { method: "POST", body: { date: $("nDate").value || todayStrLocal() } });
    NEWS = r.news;
    if (r.pull_error) toast("在线源失败：" + r.pull_error + "（已可登记搜索补全）", "err");
    else toast("已拉取 " + r.fetched + " 条并自动打标，自动消息分 " + (r.auto_score >= 0 ? "+" : "") + r.auto_score, "ok");
    renderNews();
  } catch (e) { toast("拉取失败：" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = "📥 拉取今日消息"; }
};

/* ---------- AI 自动方向研判 & 命中率（只读，不需要用户输入） ---------- */
function renderNewsDir() {
  const d = NEWS.direction;
  const dirTxt = (dd) => dd.dir === "bull" ? "看多" : dd.dir === "bear" ? "看空" : "中性";
  $("nDirCur").innerHTML = d
    ? (d.resolved_date
        ? `今日 AI 判断：<b>${dirTxt(d)}</b>（信心 ${Math.round((d.confidence || 1) * 100)}%）——已于 ${esc(d.resolved_date)} 结算：${d.hit ? '<span class="up">命中 ✓</span>' : '<span class="down">未中 ✗</span>'}（当日 ${fmtPct(d.next_chg)}）`
        : `今日 AI 判断：<b>${dirTxt(d)}</b>（信心 ${Math.round((d.confidence || 1) * 100)}%，评分自动生成），等待下一交易日收盘后自动结算`)
    : "今日尚未运行研判（运行“立即运行今日研判”后自动记录 AI 方向）";
  const s = NEWS.stats || {};
  const rate = s.hit_rate == null ? "—" : fmtPct0(s.hit_rate);
  $("nDirStats").innerHTML = `AI 方向判断命中率：<b>${rate}</b>（命中 ${s.hit || 0} / 结算 ${s.total || 0}）` +
    (s.total
      ? `；看多 ${(s.by_dir?.bull?.n || 0)} 次命中 ${s.by_dir?.bull?.hit || 0}，看空 ${(s.by_dir?.bear?.n || 0)} 次命中 ${s.by_dir?.bear?.hit || 0}，中性 ${(s.by_dir?.neutral?.n || 0)} 次命中 ${s.by_dir?.neutral?.hit || 0}。`
      : "（尚无结算记录，运行几次研判后自动统计）");
  const hist = (s.history || []).slice(0, 30);
  $("nDirHist").innerHTML = hist.length ? `<table><thead><tr><th>判断日</th><th>AI方向</th><th>信心</th><th>结算日</th><th>当日涨跌</th><th>结果</th></tr></thead><tbody>` +
    hist.map(x => `<tr><td>${esc(x.date)}</td><td>${dirTxt(x)}</td><td>${Math.round((x.confidence || 1) * 100)}%</td>
      <td>${esc(x.resolved_date || "—")}</td><td>${x.next_chg == null ? "—" : fmtPct(x.next_chg)}</td>
      <td>${x.hit ? '<span class="up">命中</span>' : '<span class="down">未中</span>'}</td></tr>`).join("") + `</tbody></table>`
    : `<div class="mut">暂无结算记录</div>`;
}

/* ---------- 学习词典 & 搜索补全 ---------- */
function renderNewsLearn() {
  $("nLearnSub").textContent = `（${NEWS.learned_total} 个新词生效）`;
  $("nBaseLex").textContent = `${NEWS.base_lexicon?.bull || 0} 利好 / ${NEWS.base_lexicon?.bear || 0} 利空`;
  const w = NEWS.learned || [];
  $("nLearned").innerHTML = w.length ? w.map(x => {
    const dir = x.bull > x.bear ? "up" : "down";
    const txt = x.bull > x.bear ? "利多" : "利空";
    const hit = x.bull + x.bear;
    return `<span class="wlearn">${esc(x.word)}<span class="dir ${dir}">${txt} ${hit}次</span></span>`;
  }).join("") : `<div class="mut">还没有你教会的新词。到上方给消息打标（利好/利空），几小时后自动打分就会带上这些词。</div>`;
}
function renderNewsQueue() {
  const open = NEWS.queue_open || [];
  const all = NEWS.queue || [];
  const last = NEWS.last_merge || {};
  $("nQueueHint").textContent =
    "机制：在线消息不足/缺失时自动写 data/search_queue.json → 让 DSH 助手用本地搜索把结果放回 data/search_results.json → 点上方【合并搜索回填结果】（或下次运行研判自动合并）。" +
    (NEWS.results_ready ? " 【检测到 data/search_results.json 有待合并内容】" : "");
  $("nQueue").innerHTML =
    `<div class="mut small">待补清单 ${open.length} 条 / 累计 ${all.length} 条` +
    (last.run_at ? ` · 最近合并 ${esc(last.run_at || "")}：${esc(last.note || "")}` : "") + `</div>` +
    (open.length ? open.map(x => `<div class="feed-item"><b>${esc(x.kind)}</b> · ${esc(x.date)}
        <div class="mut small">${esc(x.query)}</div>
        <div class="mut small">理由：${esc(x.reason)}</div></div>`).join("")
      : `<div class="mut">当前无信息缺口；如仍想补充，可点【登记搜索补全清单】。</div>`);
}
$("btnSearchExport").onclick = async () => {
  try {
    const r = await api("/api/news/search/export", { method: "POST", body: { date: $("nDate").value || todayStrLocal() } });
    NEWS = r.payload;
    toast("已登记 " + r.queue_open.length + " 条补全清单 → data/search_queue.json；告诉 DSH 助手即可搜索回填", "ok");
    renderNewsQueue();
  } catch (e) { toast("失败：" + e.message, "err"); }
};
$("btnSearchImport").onclick = async () => {
  try {
    const r = await api("/api/news/search/import", { method: "POST", body: { date: $("nDate").value || todayStrLocal() } });
    NEWS = r.payload;
    toast(r.merge.note, r.merge.added ? "ok" : "err");
    renderNews();
  } catch (e) { toast("合并失败：" + e.message, "err"); }
};
