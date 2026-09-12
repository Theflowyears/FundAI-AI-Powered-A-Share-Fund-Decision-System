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
let loadSeq = 0; // 请求序号：慢响应返回时若已有更新的轮询，丢弃旧数据防止覆盖
async function loadAll(quiet) {
  const seq = ++loadSeq;
  try {
    const [s, h, r, o, f] = await Promise.all([
      api("/api/state"), api("/api/history"), api("/api/records"),
      api("/api/orders"), api("/api/funds"),
    ]);
    if (seq !== loadSeq) return; // 已有更新的加载结果，丢弃本次
    ST = s.state; HIST = h.snapshots || []; RECS = r.records || [];
    ORDERS = o.orders || []; FUNDS = f.funds || [];
    renderHeader(); renderDash(); renderRecords(); renderOrders(); renderFunds();
    renderSettings(); renderSrcAlert(); tryRenderBt();
    // echarts 就绪后与布局稳定后各重绘一次（容器可见才真正绘制）
    whenEcharts(redrawAll);
    setTimeout(() => { if (seq === loadSeq) whenEcharts(redrawAll); }, 600);
  } catch (e) {
    if (!quiet) toast("加载失败：" + e.message, "err");
  }
  loadIndices();
  api("/api/news/dirstats").then(j => {
    if (seq !== loadSeq) return;
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
  const dLeft = ST.days_left || 0, tdLeft = ST.est_trading_days || 0;
  if (ST.gap <= 0) {
    $("cNeedDaily").innerHTML = "🎉 已达成目标！";
  } else if (dLeft <= 0) {
    $("cNeedDaily").textContent = "实验期已结束，未达成（差 " + fmtMoney(ST.gap) + "）";
  } else if (need == null || tdLeft <= 0) {
    $("cNeedDaily").textContent = "剩余 " + dLeft + " 个自然日暂无交易日（假期段，待节后自动重算）";
  } else {
    $("cNeedDaily").innerHTML = "剩余 " + dLeft + " 天（约 " + tdLeft + " 个交易日），" +
      "需日均收益 <b class='gold-txt'>" + fmtPct(need) + "</b>（当前缺口 " + fmtMoney(ST.gap) + "）";
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
    $("cViewCalc").innerHTML = lr.calc
      ? `<details><summary class="small" style="cursor:pointer;color:#4e9cff">🧮 评分怎么算的</summary><div class="small" style="margin-top:4px">${calcBlock(lr.calc)}</div></details>`
      : "";
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

/* 持仓盈亏独立刷新：绕过 60s 轮询 / 服务端 75s 状态快照，
   立即在线重取持仓基金最新净值并局部重渲染（尽快核对收益） */
let _refreshingPos = false;
$("btnRefreshPos").onclick = async () => {
  if (_refreshingPos) return;
  _refreshingPos = true;
  const btn = $("btnRefreshPos");
  btn.disabled = true;
  const oldTxt = btn.textContent;
  btn.textContent = "刷新中…";
  try {
    const r = await api("/api/positions/refresh", { method: "POST", body: {} });
    if (!r.ok) throw new Error(r.message || "刷新失败");
    if (ST) {
      ST.positions = r.positions || [];
      ST.cash = r.cash; ST.mv_eq = r.mv_eq; ST.mv_bond = r.mv_bond;
      ST.total = r.total; ST.fees = r.fees;
    }
    renderPnL();
    if (ST) renderDash();
    const navDates = [...new Set(Object.values(r.nav_dates || {}).filter(Boolean))];
    const staleN = (r.stale || []).length;
    toast("持仓净值已刷新：" + (r.positions || []).length + " 只持仓" +
      (navDates.length ? "（净值日 " + navDates.join("/") + "）" : "") +
      (staleN ? "；" + staleN + " 只暂无最新净值（取到的是缓存）" : ""),
      staleN ? "err" : "ok");
  } catch (e) { toast("持仓刷新失败：" + e.message, "err"); }
  finally { _refreshingPos = false; btn.disabled = false; btn.textContent = oldTxt; }
};

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
  } else if (page === "micro") {
    drawMicroCharts();
  }
  ["chMain", "chPie", "chScore", "chAIDir", "chBt",
   "chMicroTrend", "chMicroHeat"].forEach(id => {
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
  // 最近连续 7 个交易日（服务端已自愈回填缺失快照；connectNulls 兜底补线）
  const H = HIST.slice(-7);
  const xs = H.map(h => h.date);
  const tot = H.map(h => h.total);
  // 沪深300 同起点：按本窗口首日的总资产缩放（同窗公平对比）
  const firstIdx = H.find(h => h.index_close != null);
  const anchor = tot.length ? tot[0] : ST.initial;
  const idxScale = firstIdx ? anchor / firstIdx.index_close : 0;
  const idxVals = H.map(h => (h.index_close != null ? h.index_close * idxScale : null));
  if (xs.length && xs[xs.length - 1] === ST.today) {
    // 今天已有快照：用实时估值覆盖末点（净值/仓位刚变动过）
    if (!ST.demo && Math.abs(tot[tot.length - 1] - ST.total) > 0.005) {
      tot[tot.length - 1] = ST.total;
    }
  } else {
    // 今日净值已出但快照未生成时，叠加今日实时总资产点位
    xs.push(ST.today); tot.push(ST.total); idxVals.push(null);
  }
  c.setOption({
    ...axisCommon(),
    color: ["#4e9cff", "#7d8597"],
    series: [
      {
        name: "账户总资产", type: "line", data: tot.map((v, i) => [xs[i], v]),
        smooth: true, showSymbol: false, connectNulls: true,
        lineStyle: { width: 2.5 },
        areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: "rgba(78,156,255,.35)" },
                        { offset: 1, color: "rgba(78,156,255,0)" }] } },
        markLine: { symbol: "none", data: [{ yAxis: ST.target }],
          lineStyle: { color: "#f7b731", type: "dashed" },
          label: { formatter: "目标 ¥" + ST.target, color: "#f7b731" } },
      },
      {
        name: "沪深300(同起点)", type: "line", connectNulls: true,
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
  const itd = ST.intraday || {};
  if (itd.summary) {
    lines.push("午间诊断（" + esc(itd.time || "") + "）：" + esc(itd.summary) +
      (itd.hit == null ? "（收盘后自动结算）" : (itd.hit ? "｜已结算：命中 ✓" : "｜已结算：未中 ✗")));
  }
  const ro = ST.risk_overlay || {};
  if (ro && ro.action && ro.action !== "none") {
    const cls = ro.scale < 1 ? "down" : "up";
    lines.push("风险过滤器（事件+行情模型，只降不升）：" +
      (ro.p_up == null ? "" : "P(次日上涨) " + Math.round(ro.p_up * 100) + "% → ") +
      "权益目标仓位×" + Math.round((ro.scale ?? 1) * 100) + "%" +
      (ro.mapping ? "（映射 " + ro.mapping + "）" : "") +
      "；训练样本 " + (ro.days || "—") + " 个交易日。");
  }
  (ST.warnings || []).forEach(w => lines.push(w));
  if (!lines.length) lines.push("一切正常。收盘后运行今日研判即可。");
  box.innerHTML = lines.map(l => `<div class="mut" style="padding:3px 0">· ${esc(l)}</div>`).join("");
}

/* ---------- 每日研判 ---------- */
/* ---------- 评分计算明细（records.calc：这一天的分到底怎么算的） ---------- */
function calcBlock(raw) {
  let c;
  try { c = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (e) { return ""; }
  if (!c || !Array.isArray(c.parts)) return "";
  const s = v => (v >= 0 ? "+" : "") + (Math.round(v * 10) / 10);
  const row = t => `<div class="mut small" style="margin-left:16px">${t}</div>`;
  let h = `<div style="line-height:1.8">`;
  for (const p of c.parts) {
    let head = `<b>${esc(p.label)}</b> ${s(p.score)}`
      + (p.capped != null ? ` →限幅 ${s(p.capped)}` : "")
      + ` × ${Math.round((p.weight || 0) * 100)}% = <b>${s(p.contrib)}</b>`;
    h += `<div>${head}</div>`;
    const d = p.detail || {};
    if (p.key === "quant" && d.factors && d.factors.length) {
      h += `<details style="margin-left:16px"><summary class="mut small" style="cursor:pointer">因子明细（Σ=${s(d.factors.reduce((a, f) => a + (f.impact || 0), 0))}）</summary>`
        + d.factors.map(f => row(`${esc(f.name)} <b>${s(f.impact)}</b>　${esc(f.note || "")}`)).join("")
        + `</details>`;
    }
    if (p.key === "news") {
      h += row(`口径：${esc(d.mode || "")}，逐条强度合计 ${s(d.net)}${d.net_mode === "sum"
        ? "（旧口径：直接限幅 ±8）"
        : ` → 按刻度换算 ÷${d.net_scale} = 净情绪 ${s(d.net_scaled)}`} → ×振幅 ${d.amplitude}（合成前再按 ±25 限幅）`);
      if (d.counts) h += row("标签分布：" + Object.keys(d.counts).map(k => `${LABEL_TXT[k] || k}×${d.counts[k]}`).join("、"));
      if ((d.items || []).length) {
        h += `<details style="margin-left:16px"><summary class="mut small" style="cursor:pointer">主要贡献条目（按|强度| top）</summary>`
          + d.items.map(i => row(`[<b>${s(i.strength)}</b>] ${LABEL_TXT[i.label] || i.label}　${esc(i.title)}`)).join("")
          + `</details>`;
      }
    }
    if (p.key === "micro") {
      (d.subs || []).forEach(x => { h += row(`${x.name} <b>${s(x.score)}</b>　${esc(x.formula)}（子权重 ${x.weight}，缺维度自动重归一）`); });
      const dd = d.data || {};
      const pct = v => v == null ? "—" : Math.round(v * 100) + "%";
      h += row(`原始数据：涨 ${dd.rise ?? "—"} / 平 ${dd.deuce ?? "—"} / 跌 ${dd.fall ?? "—"}；涨停 ${dd.zt ?? "—"}、炸板 ${dd.zb ?? "—"}、跌停 ${dd.dt ?? "—"}；晋级率 ${pct(dd.promotion_rate)}（对 ${esc(dd.prev_date || "—")}）、炸板率 ${pct(dd.zb_rate)}`);
      if ((d.flags || []).length) h += row(`⚠ 极值阻尼：${d.flags.includes("overheat") ? "情绪过热（>70 半衰减，防沸点追高）" : ""}${d.flags.includes("freezing") ? "情绪冰点（<-70 半衰减，防割在冰点）" : ""}`);
    }
  }
  const sm = c.smooth || {};
  h += `<div>规则合成原始分 <b>${s(c.raw)}</b>`
    + (sm.enabled
      ? ` → EMA 平滑（α=${sm.alpha}，昨日 ${sm.prev != null ? s(Math.round(sm.prev * 10) / 10) : "无"}）→ <b>${s(c.smoothed)}</b>`
      : "（EMA 平滑未启用）")
    + `</div>`;
  if (c.llm) h += row(`🤖 ${esc(c.llm.provider || "LLM")} 独立研判 ${s(c.llm.score)}（展示/记录用；分仓执行按规则分 ${s(c.llm.rule_score)}）`);
  h += `<div>当日记录分：<b>${s(c.final_recorded)}</b></div></div>`;
  return h;
}

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
      ${r.calc ? `<details style="margin-top:6px"><summary class="small" style="cursor:pointer;color:#4e9cff">🧮 这一天分数是怎么算的</summary><div class="small" style="margin-top:4px">${calcBlock(r.calc)}</div></details>`
        : `<div class="mut small" style="margin-top:4px">（旧版记录，未存计算明细；新版起每条自动生成）</div>`}
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
/* 目标与本金都是可配置的：这里统一算"目标较本金增长多少"，避免各处置灰字。
   目标 = 投入 × account.target_multiple（默认 1.3），由后端 state 下发。 */
function targetPct() {
  const init = Number(ST?.initial || 0);
  const tgt = Number(ST?.target || 0);
  if (init > 0 && tgt > 0) return Math.round((tgt / init - 1) * 100);
  const mult = Number(ST?.target_multiple);
  return Number.isFinite(mult) && mult > 0 ? Math.round((mult - 1) * 100) : 30;
}
function pctOfTarget() {
  const p = targetPct();
  return p >= 0 ? "+" + p + "%" : p + "%";
}
/* ===== 参数开关面板（后端 fundai/param_docs.py 提供说明，configedit.py 负责写入） ===== */
let PDOC = null;      // /api/config 载荷：params / values / defaults / toggles / groups
let PEDIT = {};       // 未保存的改动 {dottedKey: value}
let PRESET = [];      // 待"重置为代码默认"的键
let PADV = false;     // 是否显示进阶参数

const KINDTXT = { bool: "开关", number: "数值", int: "整数", enum: "枚举",
  list: "多选", text: "文本", map: "结构" };

async function loadParamPanel(force) {
  if (PDOC && !force) { renderParamPanel(); return; }
  try {
    const j = await api("/api/config?params=1");
    PDOC = j;
    PEDIT = {}; PRESET = [];
    renderParamPanel();
  } catch (e) {
    $("paramBadge").textContent = "加载失败";
    $("paramNote").innerHTML = `<span class="mut">${esc(e.message)}</span>`;
  }
}

function pCur(key) {
  if (key in PEDIT) return PEDIT[key];
  return (PDOC && PDOC.values) ? PDOC.values[key] : undefined;
}
function pDirtyCount() { return Object.keys(PEDIT).length + PRESET.length; }

function pFmt(key, meta, v) {
  if (v === undefined || v === null) return "—";
  if (meta.kind === "bool") return v ? "开" : "关";
  if (meta.kind === "list") return (v || []).join(" / ") || "（空）";
  if (meta.unit === "比例") return Number(v).toFixed(3);
  return String(v);
}

function pCtl(key, meta) {
  const v = pCur(key);
  const id = "p_" + key.replace(/\./g, "_");
  if (meta.kind === "number" || meta.kind === "int") {
    const mn = meta.min ?? 0, mx = meta.max ?? Math.max(1, Number(v) * 2 || 1);
    const st = meta.step ?? (meta.kind === "int" ? 1 : 0.01);
    const rng = (meta.kind === "number")
      ? `<input type="range" data-pk="${esc(key)}" min="${mn}" max="${mx}" step="${st}" value="${Number(v ?? 0)}">` : "";
    return `${rng}<input id="${id}" type="number" data-pk="${esc(key)}" min="${mn}" max="${mx}" step="${st}" value="${Number(v ?? 0)}">
      <span class="mut small">${esc(meta.unit || "")} ${mn}–${mx}</span>`;
  }
  if (meta.kind === "enum") {
    const opts = (meta.options || []).map(o =>
      `<option value="${esc(o.value)}"${String(v) === String(o.value) ? " selected" : ""}>${esc(o.label || o.value)}</option>`).join("");
    return `<select data-pk="${esc(key)}">${opts}</select>`;
  }
  if (meta.kind === "list") {
    const cur = new Set((v || []).map(String));
    return `<div class="opts">` + (meta.options || []).map(o =>
      `<label><input type="checkbox" data-pk="${esc(key)}" data-multi="1" value="${esc(o.value)}"
        ${cur.has(String(o.value)) ? "checked" : ""}> ${esc(o.label || o.value)}</label>`).join("") + `</div>`;
  }
  if (meta.kind === "text") {
    const tp = (PDOC.sensitive || []).includes(key) ? "password" : "text";
    return `<input id="${id}" type="${tp}" data-pk="${esc(key)}" value="${esc(v ?? "")}"
      style="min-width:260px" autocomplete="off">`;
  }
  return `<span class="mut small">${esc(KINDTXT[meta.kind] || meta.kind)}：请直接编辑 config.json</span>`;
}

function renderParamPanel() {
  if (!PDOC) return;
  const groups = PDOC.groups || [];
  const toggles = new Set(PDOC.toggles || []);
  const dirty = pDirtyCount();
  $("paramBadge").textContent = `${(PDOC.params || []).length} 个参数 · ${toggles.size} 个开关`
    + (dirty ? ` · 待保存 ${dirty}` : "");
  $("paramBadge").className = "badge" + (dirty ? " warn" : "");
  $("paramNote").innerHTML = `每个参数都带「作用 / 调大调小会怎样」说明；开关点一下即改。`
    + `保存时会先做类型/范围/跨字段一致性校验，并自动备份 config.json（可一键回滚）。`;

  // 一排开关（bool 参数）
  const tgl = (PDOC.params || []).filter(p => toggles.has(p.key));
  $("paramToggles").innerHTML = tgl.map(p => {
    const on = !!pCur(p.key);
    const dead = p.dead ? "（dead：无读取点）" : "";
    return `<div class="sw ${on ? "on" : ""}" data-pk="${esc(p.key)}" data-toggle="1">
      <div class="track"></div>
      <div class="txt"><b>${esc(p.label)}</b>
        <span>${esc((p.desc || "").slice(0, 96))}${dead}</span></div></div>`;
  }).join("");

  // 分组参数表
  const byGroup = {};
  (PDOC.params || []).forEach(p => {
    if (toggles.has(p.key)) return;               // 已在上方开关区
    (byGroup[p.group] = byGroup[p.group] || []).push(p);
  });
  const sect = groups.filter(g => byGroup[g]).map(g => {
    const rows = byGroup[g].slice().sort((a, b) => (a.order || 0) - (b.order || 0));
    const shown = rows.filter(p => PADV || !p.advanced);
    const hidden = rows.length - shown.length;
    const body = shown.map(p => {
      const d = p.dead ? " dead" : "";
      const df = (p.key in PEDIT) ? " dirty" : "";
      return `<div class="prow${d}${df}" data-row="${esc(p.key)}">
        <div class="pname">${esc(p.label)} <span class="mut small">[${esc(KINDTXT[p.kind] || p.kind)}]</span>
          ${p.advanced ? '<span class="mut small">·进阶</span>' : ""}
          <code>${esc(p.key)}</code></div>
        <div class="pctl">${pCtl(p.key, p)}
          <span class="rst" data-reset="${esc(p.key)}">重置默认</span></div>
        <div class="pdesc">${esc(p.desc || "")}
          ${p.effect ? `<span class="eff">影响：${esc(p.effect)}</span>` : ""}
          <span class="mut">当前：${esc(pFmt(p.key, p, pCur(p.key)))}</span></div>
      </div>`;
    }).join("");
    return `<details class="pgroup" ${g === "仓位与上限" ? "open" : ""}>
      <summary>${esc(g)} <span class="mut small">${rows.length} 项${hidden ? `（${hidden} 项进阶隐藏）` : ""}</span></summary>
      ${body}</details>`;
  }).join("");
  $("paramGroups").innerHTML = sect;
  bindParamEvents();
  renderSrcDetail();
}

function bindParamEvents() {
  document.querySelectorAll("[data-toggle]").forEach(el => {
    el.onclick = () => {
      const k = el.dataset.pk;
      const now = !pCur(k);
      PEDIT[k] = now;
      PRESET = PRESET.filter(x => x !== k);
      renderParamPanel();
    };
  });
  document.querySelectorAll("[data-pk]:not([data-toggle])").forEach(el => {
    const k = el.dataset.pk;
    const meta = (PDOC.params || []).find(p => p.key === k) || {};
    const handler = () => {
      if (el.dataset.multi) {
        const cur = new Set((PEDIT[k] !== undefined ? PEDIT[k] : (PDOC.values[k] || [])).map(String));
        if (el.checked) cur.add(el.value); else cur.delete(el.value);
        PEDIT[k] = Array.from(cur);
      } else if (meta.kind === "int" || meta.kind === "number") {
        PEDIT[k] = Number(el.value);
      } else {
        PEDIT[k] = el.value;
      }
      PRESET = PRESET.filter(x => x !== k);
      // 同组联动：range 与 number 同步显示
      document.querySelectorAll(`[data-pk="${CSS.escape ? CSS.escape(k) : k}"]`).forEach(o => {
        if (o !== el && !o.dataset.multi) o.value = el.value;
      });
      const row = document.querySelector(`[data-row="${CSS.escape ? CSS.escape(k) : k}"]`);
      if (row) row.classList.add("dirty");
      const n = pDirtyCount();
      $("paramBadge").textContent = `${(PDOC.params || []).length} 个参数 · ${(PDOC.toggles || []).length} 个开关 · 待保存 ${n}`;
      $("paramBadge").className = "badge" + (n ? " warn" : "");
    };
    el.oninput = handler;   // range 拖动实时
    el.onchange = handler;
  });
  document.querySelectorAll("[data-reset]").forEach(el => {
    el.onclick = () => {
      const k = el.dataset.reset;
      delete PEDIT[k];
      if (!PRESET.includes(k)) PRESET.push(k);
      renderParamPanel();
      toast("已标记重置：" + k + "（点『保存改动』生效）");
    };
  });
}

async function saveParamPanel() {
  const n = pDirtyCount();
  if (!n) { toast("没有待保存的改动"); return; }
  const sen = Object.keys(PEDIT).filter(k => (PDOC.sensitive || []).includes(k));
  let confirmSensitive = false;
  if (sen.length) {
    confirmSensitive = window.confirm("即将修改敏感键：\n" + sen.join("\n") +
      "\n\n已自动备份 config.json，可回滚。确认继续？");
    if (!confirmSensitive) return;
  }
  const btn = $("btnParamSave");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const r = await api("/api/config/set", { method: "POST",
      body: { set: PEDIT, reset: PRESET, confirm_sensitive: confirmSensitive } });
    const changed = Object.keys(r.applied || {}).length;
    toast(`已保存 ${changed} 项` + (r.restart_required?.length
      ? `；需重启服务生效：${r.restart_required.join(", ")}` : ""), "ok");
    (r.warnings || []).forEach(w => toast(w, "err"));
    PEDIT = {}; PRESET = [];
    await loadParamPanel(true);
    await loadAll(true);
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  } finally {
    btn.disabled = false; btn.textContent = "💾 保存改动";
  }
}

const SRC_LABEL = { zhitu: "智兔数服", akshare: "AKShare", eastmoney: "东财/天天基金",
  sina: "新浪财经", tencent: "腾讯行情", ths: "同花顺(涨停池)" };
const PURPOSE_LABEL = { nav: "基金净值", index_kline: "指数K线", index_bars: "跨市场指数",
  quote: "实时行情", index_profile: "基金概况" };

function srcSnapshot() {
  const u = (ST && ST.api_usage) || {};
  return u.snapshot || u;
}

function renderSrcAlert() {
  const s = srcSnapshot();
  const panel = $("srcAlertPanel");
  if (!panel) return;
  const today = s.today || {};
  const deg = !!s.degraded;
  const exhausted = !!today.exhausted;
  const warn = deg || exhausted || (today.fail || 0) > 0;
  panel.style.display = warn ? "" : "none";
  if (!warn) return;
  const act = Object.entries(s.active || {})
    .map(([k, v]) => `${PURPOSE_LABEL[k] || k}→${SRC_LABEL[v] || v}`).join("、") || "—";
  $("srcAlertBadge").textContent = exhausted ? "配额耗尽" : (deg ? "已降级" : "有失败");
  $("srcAlertBadge").className = "badge warn";
  $("srcAlertNote").innerHTML = `今日调用 <b>${today.calls ?? 0}</b> 次`
    + (today.limit != null ? `（上限 ${today.limit}，剩余 ${today.remaining}）` : "（无配额上限）")
    + ` · 失败 ${today.fail ?? 0} 次 · 当前供货：${esc(act)}`
    + (s.degraded_reason ? `<br>原因：${esc(s.degraded_reason)}` : "");
  const fo = (s.failover || []).slice(0, 3);
  $("srcAlertDetail").innerHTML = fo.length ? fo.map(f =>
    `<div class="src-row"><span class="nm">${esc(f.ts?.slice(11) || "")}</span>
      <span>${esc(PURPOSE_LABEL[f.purpose] || f.purpose || "")}：${esc(SRC_LABEL[f.from] || f.from || "?")}
      → ${esc(SRC_LABEL[f.to] || f.to || "?")}</span>
      <span class="mut small">${esc((f.reason || "").slice(0, 90))}</span></div>`).join("")
    : `<div class="mut small">今日暂无通道切换记录。</div>`;
}

function renderSrcDetail() {
  if (!$("srcDetail")) return;
  const s = srcSnapshot();
  const today = s.today || {}, yest = s.yesterday || {};
  const srcs = today.sources || {};
  const rows = Object.keys(srcs).filter(k => (srcs[k].calls || 0) > 0 ||
    (SRC_LABEL[k] && (srcs[k].limit || 0) > 0));
  const badge = $("srcBadge");
  if (badge) {
    badge.textContent = `${s.provider || ST?.data_source || ""} · 今日 ${today.calls ?? 0}`
      + (today.limit != null ? `/${today.limit}` : "");
    badge.className = "badge" + (s.degraded || today.exhausted ? " warn" : " live");
  }
  const note = $("srcNote");
  if (note) {
    const act = Object.entries(s.active || {})
      .map(([k, v]) => `${PURPOSE_LABEL[k] || k} → ${SRC_LABEL[v] || v}`).join("；") || "（今日尚无在线调用）";
    note.innerHTML = `计数已**持久化到磁盘**（data/api_usage.json），改代码/重启服务都不会清零；`
      + `按来源分别统计成功与失败。<br>当前供货：${esc(act)}`
      + (s.degraded ? ` · <span class="gold-txt">已降级：${esc(s.degraded_reason || "")}</span>` : "");
  }
  const bar = (v, mx) => {
    const p = mx ? Math.max(2, Math.min(100, v / mx * 100)) : 0;
    return `<span class="bar"><i style="width:${p}%"></i></span>`;
  };
  let html = rows.map(k => {
    const x = srcs[k] || {};
    const lim = x.limit;
    const bad = (x.fail || 0) > 0;
    return `<div class="src-row"><span class="nm">${esc(SRC_LABEL[k] || k)}</span>
      <span>调用 <b>${x.calls ?? 0}</b> · 成功 ${x.ok ?? 0} · <span class="${bad ? "gold-txt" : "mut"}">失败 ${x.fail ?? 0}</span></span>
      ${lim ? `${bar(x.calls || 0, lim)}<span class="mut small">剩余 ${Math.max(0, lim - (x.calls || 0))}/${lim}</span>` : '<span class="mut small">无配额上限</span>'}
      <span class="mut small">${x.last_ok ? "最近成功 " + esc(x.last_ok.slice(11)) : ""}
        ${x.last_error ? " · 最近错误：" + esc(String(x.last_error).slice(0, 60)) : ""}</span></div>`;
  }).join("");
  if (!rows.length) html = `<div class="mut small">今日暂无在线调用（全部走本地缓存）。</div>`;
  html += `<div class="src-row"><span class="nm">昨日</span><span>调用 ${yest.calls ?? 0} 次 · 失败 ${yest.fail ?? 0} 次</span>
    <span class="mut small">（计数按日期分桶，保留 30 天）</span></div>`;
  const days = (s.days || []).slice(0, 14).reverse();
  if (days.length) {
    html += `<div class="src-row"><span class="nm">近 14 天</span><span class="mut small">` +
      days.map(d => `${esc(String(d.date).slice(5))}:${d.calls}${d.fail ? `(败${d.fail})` : ""}`).join("　") +
      `</span></div>`;
  }
  const fo = (s.failover || []).slice(0, 5);
  if (fo.length) {
    html += `<div class="src-row"><span class="nm">通道切换</span><span class="mut small">` +
      fo.map(f => `${esc(String(f.ts || "").slice(5, 16))} ${esc(PURPOSE_LABEL[f.purpose] || f.purpose || "")}:${esc(SRC_LABEL[f.from] || f.from || "?")}→${esc(SRC_LABEL[f.to] || f.to || "?")}`).join("<br>") +
      `</span></div>`;
  }
  $("srcDetail").innerHTML = html;
}

$("btnParamSave").onclick = saveParamPanel;
$("btnParamUndo").onclick = () => { PEDIT = {}; PRESET = []; renderParamPanel(); toast("已撤销未保存的改动"); };
$("btnParamRollback").onclick = async () => {
  if (!window.confirm("用最近一份备份覆盖 config.json？（当前配置会先被备份）")) return;
  try {
    const r = await api("/api/config/rollback", { method: "POST", body: {} });
    toast("已回滚：" + (r.restored_from || ""), "ok");
    await loadParamPanel(true); await loadAll(true);
  } catch (e) { toast("回滚失败：" + e.message, "err"); }
};
$("paramAdvanced").onchange = (e) => { PADV = !!e.target.checked; renderParamPanel(); };

function renderSettings() {
  const cfg = ST.config || {};
  const st = cfg.strategy || {};
  const idx = cfg.index || {};
  const llm = cfg.llm_enabled ? ("已启用（" + esc(cfg.llm_provider || "LLM") + "）")
    : (cfg.llm_needs_key ? "开关已开但 api_key 为空（未生效）"
                         : "未启用");
  $("setAccount").innerHTML = `<table><tbody>
    <tr><td>账户名称</td><td>${esc(ST.name)}</td></tr>
    <tr><td>起始资金（可自定义）</td><td>${fmtMoney(ST.initial)}</td></tr>
    <tr><td>目标线</td><td class="gold-txt">${fmtMoney(ST.target)}（较起始资金 ${pctOfTarget()}）</td></tr>
    <tr><td>目标倍数</td><td>${(ST.target_multiple ?? (ST.target / ST.initial)).toFixed(2)}×（在「参数开关面板 → 运行与服务」里改起始资金/倍数，保存即生效）</td></tr>
    <tr><td>观察期</td><td>${esc(ST.start)} → ${esc(ST.end)}（${esc(ST.days_left)} 天剩余）</td></tr>
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
    <tr><td>LLM 深度研判</td><td>${llm}（config.json → llm：enabled=true 且 api_key 非空即生效；改完**无需重启**，服务自动热加载，下次研判全文由大模型撰写）</td></tr>
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
     schtasks /Create /TN fundai_daily /TR "cmd /c cd /d <你的项目目录> ^&^& python app.py run-daily >> data\\daily.log" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 20:40`;
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
    "<br>⚠️ 历史回测结果绝不代表未来收益。目标 " + pctOfTarget() + " 需市场显著上涨，属于较高目标，请把它当视频实验看待。";
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
    if (b.dataset.tab === "micro") loadMicroTab();
    if (b.dataset.tab === "settings") loadParamPanel();
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
    loadCalibration(false);
  } catch (e) { toast("消息数据加载失败：" + e.message, "err"); }
}

function renderNews() {
  if (!NEWS) return;
  $("nDate").value = NEWS.date;
  // 顶部分数卡片
  $("nAutoScore").textContent = (NEWS.auto_score >= 0 ? "+" : "") + NEWS.auto_score;
  $("nAutoScore").className = "v " + (NEWS.auto_score > 0 ? "up" : NEWS.auto_score < 0 ? "down" : "");
  const sc = NEWS.auto_scope || {};
  const scopeTxt = sc.n_in_scope != null
    ? "；口径：重点事件 " + sc.n_in_scope + " 条 / 词典 " + (sc.dict_mode === "off" ? "关" : sc.dict_mode)
      + (sc.scale ? " / 刻度 " + sc.scale : "")
      + (NEWS.auto_net_src === "cache" ? "（与引擎同一份口径）" : "（按当前口径近似重算）")
    : "";
  $("nAutoSub").textContent = "净情绪 " + (NEWS.auto_net >= 0 ? "+" : "") + NEWS.auto_net +
    "（分 = 净情绪 × 振幅 " + NEWS.cfg.news_amp + "）" + scopeTxt;
  const h = NEWS.human;
  if (h) {
    $("nHumanScore").textContent = (h.score >= 0 ? "+" : "") + h.score;
    $("nLabels").textContent = "人工消息分已生效：净情绪 " + (h.net >= 0 ? "+" : "") + h.net +
      "（方向打标 " + h.directional + " 条 / 共 " + h.labeled + " 条）";
  } else {
    $("nHumanScore").textContent = "未打标";
    $("nLabels").textContent = "AI 已直接采纳 " + (NEWS.ai_accepted || 0) + " 条明确利好/利空；" +
      "只挑 AI 没把握的（中性）" + (NEWS.todo || 0) + " 条给你确认（≤50），确认后算法学成语录词并用于人工消息分";
  }
  const by = NEWS.labels.by_label || {};
  const doneN = NEWS.done || 0;
  $("nProgress").textContent = "待确认 " + (NEWS.todo || 0) + " 条 · AI 已采纳 " +
    (NEWS.ai_accepted || 0) + " 条 · 已打标 " + doneN + " / 共 " + NEWS.feed_count + " 条";
  const isToday = NEWS.date === todayStrLocal();
  let capNote = "";
  if (isToday && (NEWS.review_extra || 0) > 0) {
    capNote = " · 另有 " + NEWS.review_extra + " 条中性超出 50 条人工额度，默认按 AI 中性处理";
  }
  $("nScoreNote").innerHTML = "消息权重 " + Math.round(NEWS.cfg.news_weight * 100) +
    "% · 学习词 " + NEWS.learned_total + " 个 · " +
    (NEWS.demo ? "" : NEWS.feed_count ? NEWS.feed_count + " 条已入库" : "尚未拉取") +
    (isToday ? capNote : "（非今日，只读历史）");
  renderNewsFeed();
  renderNewsDir();
  renderNewsLearn();
  renderNewsQueue();
}

/* ---------- 消息列表 ---------- */
function renderNewsFeed() {
  const items = (NEWS.feed || []).filter(it => {
    if (NF === "all") return true;
    if (NF === "unrated") return it.needs_review && !it.user_label; // 待你确认(≤50)
    if (NF === "auto") return it.ai_decided && !it.user_label;      // AI 已采纳
    if (NF === "imp") return !!it.important;                        // 🔴重要电报
    if (NF === "archived") return it.auto_handled && !it.user_label; // 📦AI自动归档
    return it.user_label ? it.user_label === NF : it.auto_label === NF;
  });
  // 同模板归并统计：代表条上显示“🔁同类×N”（N 含自身）
  const dupN = {};
  (NEWS.feed || []).forEach(x => {
    if (x.dup_of) dupN[x.dup_of] = (dupN[x.dup_of] || 0) + 1;
  });
  const autoKindTxt = {
    premium: "产品风险提示·AI判中性",
    disclosure: "例行持股披露·AI判中性",
    dup: "同模板批量·AI已归并",
  };
  const shownNote = NF === "unrated" ? "（AI 没把握、待你确认）"
    : NF === "auto" ? "（AI 已直接采纳，可展开覆盖）" : "";
  $("nFeedCount").textContent = "共 " + NEWS.feed_count + " 条，当前显示 " + items.length +
    " 条" + shownNote;
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
    const unlabeled = !it.user_label;
    const autoOnly = unlabeled && !it.needs_review;  // AI 已采纳 / 中性超额：默认不打标
    const dupPill = dupN[it.id]
      ? `<span class="pill" style="color:#8d99ae" title="该模板其余同类消息已被 AI 自动归并归档（不占人工额度）；仅保留本条代表供你一眼复核、有异议可打标">🔁同类 ${dupN[it.id] + 1} 条·已归并</span>`
      : "";
    const buttons = Object.keys(LABEL_TXT).map(k =>
      `<button class="ratebtn ${it.user_label === k ? (k.includes("bull") ? "on-bull" : k.includes("bear") ? "on-bear" : k === "neutral" ? "on-neutral" : "") : ""}"
        data-rate="${it.id}" data-label="${k}">${LABEL_TXT[k]}</button>`).join("");
    const flagPill = it.needs_review
      ? `<span class="pill" style="color:#f7b731;border-color:rgba(247,183,49,.6)">待你确认</span>`
      : (autoOnly && it.auto_handled
          ? `<span class="pill" style="color:#8d99ae;border-color:rgba(141,153,174,.6)" title="例行披露/同模板批量消息：只是个股或产品层面的趋势记录，无大盘方向信息，AI 已自动判中性归档，不占用你的人工复核额度">${autoKindTxt[it.auto_kind] || "AI判中性·已归档"}</span>`
          : (autoOnly && it.ai_decided
              ? `<span class="pill" style="color:#4adf9a;border-color:rgba(33,191,115,.5)">AI 已自动采纳${autoSign}${autoTxt}</span>`
              : (autoOnly && unlabeled ? `<span class="pill" style="color:#8d99ae">超出额度·按AI中性</span>` : "")));
    return `<div class="feed-item lab-${lab === "irrelevant" ? "neutral" : lab}">
      <div style="display:flex; gap:8px; align-items:flex-start; flex-wrap:wrap">
        ${it.important ? `<span class="pill" style="color:#ff7d80;border-color:rgba(255,77,79,.55)" title="重要电报：命中突发/重要/涨跌停等关键词，方向情绪已加权×1.25">🔴重要</span>` : ""}
        <span class="pill">${esc(it.time || "")} ${esc(it.source || "")}</span>
        <span class="pill" style="${it.auto_label === "bull" ? "color:#ff7d80;border-color:rgba(255,77,79,.5)" :
            it.auto_label === "bear" ? "color:#4adf9a;border-color:rgba(33,191,115,.5)" : ""}">
          自动:${autoTxt}${autoSign}</span>
        ${flagPill}${dupPill}${sectors}${funds}
      </div>
      <div style="margin:4px 0; font-weight:600; line-height:1.5">${esc(it.title || "")}</div>
      ${it.text && it.text !== it.title ? `<div class="mut small clamp2" style="margin-bottom:4px">${esc(it.text)}</div>` : ""}
      ${it.auto_reason ? `<div class="mut small" style="margin:0 0 6px;color:#7d8aa0">💡 AI 依据：${esc(it.auto_reason)}</div>` : ""}
      <div style="display:flex; gap:4px; flex-wrap:wrap; align-items:center">
        <span class="mut small">我的判断：</span>
        ${autoOnly && unlabeled
          ? `<span class="ratebox" style="display:none">${buttons}</span>
             <a href="javascript:;" class="mut small" data-reveal>有异议？点这里手动打标</a>`
          : `<span class="ratebox">${buttons}</span>`}
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
  const rev = ev.target.closest("[data-reveal]");
  if (rev) {
    const box = rev.parentElement.querySelector(".ratebox");
    if (box) { box.style.display = "inline-flex"; rev.remove(); }
    return;
  }
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

/* ---------- 事件命中率校准（消息→次日大盘）可视化 ---------- */
let CAL = null;
const CAL_EVN = {
  cbank_ease: "央行宽松", cbank_tight: "货币收紧", geo_conflict: "地缘冲突",
  earnings: "公司业绩", holder_flow: "股东增减持/监管",
  market_policy: "宏观/资本市场政策", industry_policy: "产业政策",
  eco_data: "经济数据", fund_premium_warning: "ETF产品风险提示",
  dict: "词典兜底",
};
async function loadCalibration(quiet) {
  try {
    const j = await api("/api/calibration");
    CAL = j.cal || null;
    renderCalibration();
  } catch (e) { if (!quiet) toast("校准数据加载失败：" + e.message, "err"); }
}
function fmtPctS(x) {
  return (x == null) ? "—" : ((x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%");
}
function renderCalibration() {
  const sub = $("calibSub"), box = $("calibBox");
  if (!CAL) { if (sub) sub.textContent = "—"; return; }
  const rep = CAL.report || {}; const hist = CAL.history || {};
  if (sub) {
    sub.innerHTML = "已结算样本 <b>" + (rep.total || 0) + "</b> 条 · 主指数历史缓存 " +
      (hist.bars || 0) + " 根（" + esc(hist.start || "—") + " → " + esc(hist.end || "—") + "）<br>" +
      esc(CAL.next_auto || "");
  }
  const evs = Object.entries(rep.by_event || {}).sort((a, b) => b[1].n - a[1].n);
  if (!box) return;
  let html = "";
  if (!evs.length) {
    html += `<div class="mut">逐日积累样本：暂无已结算样本（每交易日 20:30 自动结算后出表）。
      想立刻有样本量，用下方「🔄 历史回填」把财联社历史电报补进来。</div>`;
  } else {
    html += `<table style="width:100%"><thead><tr>
    <th>事件类型</th><th>样本</th><th>利多/利空/中性</th><th>命中</th><th>命中率</th><th>平均次日涨跌</th>
    </tr></thead><tbody>` + evs.map(([k, a]) => {
      const rate = a.hit_rate;
      const cls = rate == null ? "" : (rate >= 0.55 ? "up" : rate <= 0.45 ? "down" : "");
      return `<tr>
      <td>${esc(CAL_EVN[k] || k)}</td>
      <td>${a.n}</td>
      <td class="mut small">${a.bull || 0}/${a.bear || 0}/${a.neutral || 0}</td>
      <td>${a.hit || 0}</td>
      <td class="${cls}">${rate == null ? "—" : fmtPct0(rate)}</td>
      <td>${fmtPctS(a.avg_chg)}</td></tr>`;
    }).join("") + `</tbody></table>
  <div class="mut small" style="margin-top:6px">命中口径：利多→次日沪深300 上涨 &gt;0.3%、利空→下跌 &gt;0.3%、中性→|涨跌|≤0.3%；样本越多越可信。</div>`;
  }
  html += calibBackfillHtml();
  box.innerHTML = html;
  renderEventModel();
}

/* 历史回填校准（财联社电报时间游标回溯；与线上同口径打分 → 次日涨跌命中率） */
function calibBackfillHtml() {
  const back = CAL.hist_backfill || {};
  const dd = back.day_level || {};
  let h = `<h4 style="margin:16px 0 4px">📜 历史回填校准（财联社电报 · 时间游标回溯任意历史）</h4>`;
  if (CAL.backfill_running) {
    h += `<div class="mut small">⏳ ${esc(CAL.backfill_msg || "抓取中…")}（页面每 5 秒自动刷新）</div>`;
  }
  if (!back.generated_at) {
    h += `<div class="mut small">尚无回填报告：点右上「🔄 历史回填」运行一次（逐页回溯、带缓存可断点续跑，约 2–5 分钟）；
      命令行等价：<code>python app.py news-calibrate --days 20</code>。<br>
      说明：<code>/nodeapi/telegraphList</code> 该接口已 <b>404 下线</b>；可用通道是
      <code>/v1/roll/get_roll_list</code> 的 <code>refresh_type=1 + last_time</code> 时间游标（实测可回溯 1 年）。</div>`;
    return h;
  }
  const evs = back.events || [];
  h += `<div class="mut small">窗口 ${esc(back.window.from)} → ${esc(back.window.to)}
    （${dd.n} 个交易日，电报 ${(back.data || {}).telegrams} 条，翻页 ${(back.data || {}).pages}）
    ｜上涨日占比 ${fmtPct0(back.baseline_up_rate)}
    ｜日级方向命中率 ${dd.hit_rate == null ? "—" : fmtPct0(dd.hit_rate)}
    ｜IC ${dd.ic == null ? "—" : dd.ic}
    ｜净情绪口径 ${esc(back.net_mode || "—")}（刻度 ${back.net_scale ?? "—"}）</div>`;
  h += `<table style="width:100%; margin-top:6px"><thead><tr>
    <th>事件类型</th><th>方向</th><th>样本</th><th>命中率</th><th>基准</th>
    <th>增量</th><th>次日均值</th><th>p</th><th>建议倍数</th></tr></thead><tbody>`;
  h += evs.map(e => {
    const cls = e.edge == null ? "" : (e.edge >= 0.05 ? "up" : e.edge <= -0.05 ? "down" : "");
    return `<tr>
      <td>${esc(e.event_cn || e.event)}</td>
      <td>${e.label === "bull" ? "利多" : "利空"}</td>
      <td>${e.n}</td>
      <td class="${cls}">${fmtPct0(e.hit_rate)}</td>
      <td class="mut">${fmtPct0(e.baseline)}</td>
      <td class="${cls}">${e.edge >= 0 ? "+" : ""}${(e.edge * 100).toFixed(1)}%</td>
      <td>${fmtPctS(e.avg_ret_pct / 100)}</td>
      <td class="mut">${e.p}</td>
      <td>${e.suggest_mult == null ? "—" : e.suggest_mult}</td></tr>`;
  }).join("") + `</tbody></table>
    <div class="mut small" style="margin-top:6px">
    增量 = 命中率 − 基准（利多基准=窗口上涨日占比，利空基准=1−上涨日占比）：<b>只有正增量</b>才说明该事件方向有
    超出市场漂移的额外预测力（下跌窗口里只看命中率会把所有利多信号都判成"差"）。
    建议倍数经 n/(n+20) 收缩、样本 ≥5 才给；默认只报告，
    <code>config.json → strategy.event_calibration=true</code> 时才自动参与自动打分。</div>`;
  const drows = back.day_rows || [];
  if (drows.length) {
    h += `<details style="margin-top:10px"><summary class="mut small" style="cursor:pointer">逐日明细（净情绪原始强度 / 合成后分数 / 次日涨跌 / 是否命中）</summary>`
      + `<div style="max-height:260px; overflow:auto; margin-top:6px"><table style="width:100%"><thead><tr>
        <th>交易日</th><th>电报</th><th>利多/利空</th><th>Σ强度</th><th>净情绪</th><th>分数</th><th>次日涨跌</th><th>命中</th>
        </tr></thead><tbody>`
      + drows.map(r => `<tr>
        <td>${esc(r.date)}</td>
        <td class="mut">${r.n_news ?? "—"}</td>
        <td class="mut small">${(r.labels || {}).bull || 0}/${(r.labels || {}).bear || 0}</td>
        <td class="mut">${r.net_raw ?? "—"}</td>
        <td>${r.net == null ? "—" : r.net.toFixed(2)}</td>
        <td class="${r.score > 0 ? "up" : r.score < 0 ? "down" : ""}">${r.score}</td>
        <td class="${r.next_ret_pct >= 0 ? "up" : "down"}">${r.next_ret_pct == null ? "—" : (r.next_ret_pct >= 0 ? "+" : "") + r.next_ret_pct + "%"}</td>
        <td>${r.hit == null ? "—" : (r.hit ? "✅" : "❌")}</td></tr>`).join("")
      + `</tbody></table></div>
      <div class="mut small" style="margin-top:4px">旧口径下这些天的「净情绪」会全部等于 8.00（Σ强度天天顶格，合成后恒为 +25 = 零区分度）；现按刻度换算后才有高低之分。</div></details>`;
  }
  return h;
}
/* ---------- 事件→次日方向：样本外模型面板 ---------- */
function renderEventModel() {
  const sub = $("modelSub"), box = $("modelBox");
  if (!box) return;
  const m = CAL.event_model || {};
  if (CAL.model_running && sub) {
    sub.innerHTML = `⏳ ${esc(CAL.model_msg || "运行中…")}（页面每 5 秒自动刷新）`;
  }
  if (!m.metrics || !m.metrics.n) {
    if (sub && !CAL.model_running) sub.textContent = "尚无结果";
    return;
  }
  const met = m.metrics, pol = m.policy || {}, sel = m.selection || {};
  const pct = v => (v == null ? "—" : fmtPct0(v));
  if (sub) {
    sub.innerHTML = `窗口 ${esc(m.window.from)} → ${esc(m.window.to)}（${m.window.days} 个交易日，样本外 ${met.n} 天）`
      + `｜基线：上涨日 ${fmtPct0(met.up_rate)}、多数类 ${fmtPct0(met.baseline_majority)}｜Brier ${met.brier}`;
  }
  let h = `<div class="mut small">命中口径：次日 |涨跌| ≥ 0.3% 的样本里方向判对（噪声日不进分母）。
    整体出手 ${met.taken} 天（覆盖 ${fmtPct0(met.coverage)}）命中 <b>${pct(met.acc)}</b>
    （基线 ${pct(met.baseline_majority)}，增量 <b class="${(met.edge_vs_baseline || 0) >= 0 ? "up" : "down"}">${met.edge_vs_baseline == null ? "—" : ((met.edge_vs_baseline >= 0 ? "+" : "") + (met.edge_vs_baseline * 100).toFixed(1) + "pp")}</b>）；
    其中波动 ≥0.5% 的日子 <b>${pct(met.acc_big_move)}</b>（${met.n_big_move} 天）。</div>`;
  if (m.backtest) {
    const bt = m.backtest;
    h += `<div class="mut small" style="margin-top:4px">策略（按信号做多/空仓，费 5bp）：
      净值 <b>${bt.nav}</b> vs 买入持有 <b>${bt.buy_hold}</b>
      <b class="${bt.win_vs_bh ? "up" : "down"}">${bt.win_vs_bh ? "跑赢基线" : "跑输基线"}</b>
      （超额 ${bt.excess >= 0 ? "+" : ""}${(bt.excess * 100).toFixed(1)}%，调仓 ${bt.switches} 次，最大回撤 ${pct(bt.max_drawdown)}）</div>`;
  }
  if ((m.per_year || []).length) {
    h += `<table style="width:100%; margin-top:8px"><thead><tr>
      <th>年份</th><th>天数</th><th>命中率</th><th>基线</th><th>增量</th><th>策略净值</th><th>买入持有</th>
      </tr></thead><tbody>` + m.per_year.map(y => `<tr>
      <td>${esc(y.year)}</td><td>${y.days}</td>
      <td class="${(y.edge || 0) > 0 ? "up" : (y.edge || 0) < 0 ? "down" : ""}">${pct(y.acc)}</td>
      <td class="mut">${pct(y.baseline)}</td>
      <td class="${(y.edge || 0) > 0 ? "up" : (y.edge || 0) < 0 ? "down" : ""}">${y.edge == null ? "—" : ((y.edge >= 0 ? "+" : "") + (y.edge * 100).toFixed(1) + "pp")}</td>
      <td>${y.bt_nav == null ? "—" : y.bt_nav}</td>
      <td class="mut">${y.bt_bh == null ? "—" : y.bt_bh}</td></tr>`).join("") + `</tbody></table>`;
  }
  h += `<table style="width:100%; margin-top:8px"><thead><tr>
    <th>选择性出手（阈值只在训练窗内定）</th><th>出手</th><th>含波动日</th><th>命中</th><th>命中率</th><th>覆盖率</th>
    </tr></thead><tbody>`;
  Object.keys(pol.targets || {}).sort((a, b) => a - b).forEach(k => {
    const c = pol.targets[k];
    const cls = c.acc == null ? "" : (c.acc >= 0.7 ? "up" : c.acc < 0.5 ? "down" : "");
    h += `<tr><td>覆盖目标 ${Math.round((c.target || 0) * 100)}%</td><td>${c.taken}</td>
      <td>${c.movers}</td><td>${c.hits}</td><td class="${cls}"><b>${pct(c.acc)}</b></td>
      <td class="mut">${fmtPct0(c.coverage)}</td></tr>`;
  });
  h += `</tbody></table>`;
  if (sel.ok) {
    h += `<div class="mut small" style="margin-top:8px">选择期（${sel.select_period.days} 天）挑中：
      feature_set=<b>${esc(sel.selected.feature_set || "market")}</b>、l2=${sel.selected.l2}、
      每 ${sel.selected.refit_every} 天重训、half_life=${sel.selected.half_life ?? 0}；
      留出期 ${esc(sel.holdout_period.from)} → ${esc(sel.holdout_period.to)}（${sel.holdout_period.days} 天，<b>仅用于报告</b>）：
      命中 <b class="${(sel.holdout.edge || 0) >= 0 ? "up" : "down"}">${pct(sel.holdout.acc)}</b>
      vs 基线 ${pct(sel.holdout.baseline)}（增量 ${sel.holdout.edge == null ? "—" : ((sel.holdout.edge >= 0 ? "+" : "") + (sel.holdout.edge * 100).toFixed(1) + "pp")}）
      ｜策略 <b class="${sel.holdout.win ? "up" : "down"}">${sel.holdout.win ? "跑赢" : "跑输"}</b>
      买入持有（${sel.holdout.bt_nav} vs ${sel.holdout.bt_bh}）</div>`;
  }
  const bk = met.buckets || [];
  if (bk.length) {
    h += `<details style="margin-top:8px"><summary class="mut small" style="cursor:pointer">把握度分档明细</summary>
      <table style="width:100%"><thead><tr><th>|p−0.5|</th><th>样本</th><th>含波动日</th><th>命中率</th></tr></thead><tbody>`
      + bk.map(b => `<tr><td>${esc(b.edge)}</td><td>${b.n}</td><td>${b.movers}</td><td>${pct(b.acc)}</td></tr>`).join("")
      + `</tbody></table></details>`;
  }
  if (m.latest) {
    h += `<div class="mut small" style="margin-top:8px">最近一日样本外预测：${esc(m.latest.date)} → ${esc(m.latest.next)}
      上涨概率 <b>${fmtPct0(m.latest.p_up)}</b>（实际次日 ${fmtPctS(m.latest.actual_next_ret)}）</div>`;
  }
  box.innerHTML = h;
}

$("btnRunModel").onclick = async () => {
  const btn = $("btnRunModel");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "建模中…";
  try {
    const r = await api("/api/event-model/run", {
      method: "POST", body: { days: 400, select: true, warmup: 60 } });
    if (!r.ok) throw new Error(r.message || "启动失败");
    toast(r.message, "ok");
    let n = 0;
    const t = setInterval(async () => {
      await loadCalibration(true);
      if (!(CAL && CAL.model_running) || ++n > 120) {
        clearInterval(t);
        btn.disabled = false; btn.textContent = old;
        toast((CAL && CAL.model_msg) || "建模结束", "ok");
      }
    }, 5000);
  } catch (e) {
    toast("建模失败：" + e.message, "err");
    btn.disabled = false; btn.textContent = old;
  }
};

$("btnRefreshCalib").onclick = () => { loadCalibration(false); toast("已刷新校准数据", "ok"); };
$("btnBackfillCalib").onclick = async () => {
  const btn = $("btnBackfillCalib");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "抓取中…";
  try {
    const r = await api("/api/calibration/backfill", { method: "POST", body: { days: 20 } });
    if (!r.ok) throw new Error(r.message || "启动失败");
    toast(r.message, "ok");
    let n = 0;
    const t = setInterval(async () => {
      await loadCalibration(true);
      if (!(CAL && CAL.backfill_running) || ++n > 120) {
        clearInterval(t);
        btn.disabled = false; btn.textContent = old;
        toast((CAL && CAL.backfill_msg) || "历史回填结束", "ok");
      }
    }, 5000);
  } catch (e) {
    toast("历史回填失败：" + e.message, "err");
    btn.disabled = false; btn.textContent = old;
  }
};
$("btnExtendHist").onclick = async () => {
  const btn = $("btnExtendHist");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "延伸中…";
  try {
    const r = await api("/api/history/extend", { method: "POST", body: { years: 8 } });
    if (!r.ok) throw new Error(r.message || "延伸失败");
    toast("主指数历史已延伸：" + (r.result?.start || "—") + " → " + (r.result?.end || "—") +
      "（" + (r.result?.bars || 0) + " 根，source=" + (r.result?.source || "") + "）", "ok");
    loadCalibration(true);
  } catch (e) { toast("延伸失败：" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; }
};
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
  const d = NEWS.direction_today || NEWS.direction;
  // 跨天/跨周末：今日还没跑研判时，显示“上一个交易日收盘给出、针对下一个交易日”的判断
  const pend = !NEWS.direction && !!NEWS.direction_today;
  const dirTxt = (dd) => dd.dir === "bull" ? "看多" : dd.dir === "bear" ? "看空" : "中性";
  const forTxt = (dd) => esc(dd.for_date || dd.date || "");
  $("nDirCur").innerHTML = d
    ? (d.resolved_date
        ? `${pend ? "AI 次日判断（针对 " + forTxt(d) + "）" : "今日 AI 判断"}：<b>${dirTxt(d)}</b>（信心 ${Math.round((d.confidence || 1) * 100)}%）——已于 ${esc(d.resolved_date)} 结算：${d.hit ? '<span class="up">命中 ✓</span>' : '<span class="down">未中 ✗</span>'}（当日 ${fmtPct(d.next_chg)}）`
        : `${pend ? "AI 次日判断（针对 " + forTxt(d) + "，由 " + esc(d.made_on || "") + " 收盘给出）" : "今日 AI 判断"}：<b>${dirTxt(d)}</b>（信心 ${Math.round((d.confidence || 1) * 100)}%${pend ? "" : "，评分自动生成"}），等待 ${forTxt(d)} 收盘后自动结算`)
    : "今日尚未运行研判（运行“立即运行今日研判”后自动记录 AI 方向；也可在命令行跑 python app.py direction-check 单独补记/结算）";
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

/* ============================================================
   情绪复盘（微观结构：涨停情绪脉搏，借鉴 Cailianpress-Feishu-Bot）
   ============================================================ */
let MICRO = null;

async function loadMicroTab(force) {
  const note = $("microNote");
  if (note) note.textContent = force ? "重抓今日数据中…" : "加载中…";
  try {
    if (force) await api("/api/micro/refresh", { method: "POST", body: {} });
    const j = await api("/api/micro");
    MICRO = j.micro;
    renderMicro();
  } catch (e) {
    if (note) note.textContent = "加载失败：" + e.message;
  }
}

function microKpi(k, v, sub, cls) {
  return `<div class="card"><div class="k">${k}</div>` +
    `<div class="v small ${cls || ""}">${v}</div>` +
    `<div class="sub">${sub || ""}</div></div>`;
}
const microPct = (v) => v == null ? "—" : (v * 100).toFixed(1) + "%";

function renderMicro() {
  if (!MICRO) return;
  const note = $("microNote");
  if (!MICRO.enabled) {
    $("microKpis").innerHTML = "";
    $("microLines").innerHTML = "该维度已关闭（strategy.micro_enable=false）。";
    return;
  }
  if (!MICRO.ok) {
    $("microKpis").innerHTML = "";
    $("microLines").innerHTML = "微观结构数据暂不可用：" + esc(MICRO.message || "接口失败") +
      "（已自动降级，不影响研判其余维度；收盘后重试或点【↻ 重抓今日】）。";
    if (note) note.textContent = "今日暂不可用（自动降级，不影响研判主流程）";
    return;
  }
  const s = MICRO.snap || {}, q = s.qualitative || {};
  const sc = s.score;
  if (note) {
    note.innerHTML = `交易日 <b>${esc(s.date || "")}</b> · 情绪分 ` +
      `<b class="${sc >= 0 ? "up" : "down"}">${sc == null ? "—" : (sc >= 0 ? "+" : "") + sc}</b>` +
      `（进合成评分权重 ${Math.round((MICRO.weight || 0) * 100)}% · 限幅 ±${MICRO.cap || 0}）` +
      (MICRO.asof ? ` · ${esc(MICRO.asof)}` : "") +
      (MICRO.stale ? " · 缓存兜底" : "") +
      ((s.errors || MICRO.errors || []).length ? " · 部分源：" + esc((s.errors || MICRO.errors).join("；")) : "");
  }
  $("microKpis").innerHTML = [
    microKpi("涨停 / 跌停", `${s.zt ?? "—"} / ${s.dt ?? "—"}`,
      `赚钱效应：${esc(q.promotion_effect || "—")} · 亏钱效应：${esc(q.loss_effect || "—")}`,
      (s.zt ?? 0) >= (s.dt ?? 0) ? "up" : "down"),
    microKpi("昨日晋级率", microPct(s.promotion_rate),
      s.promotion == null ? "接入首日（逐日累积后可用）" : `${esc(s.prev_date || "昨日")}涨停今日晋级 ${s.promotion} 只`,
      s.promotion_rate > 0.3 ? "up" : (s.promotion_rate != null && s.promotion_rate < 0.15 ? "down" : "")),
    microKpi("炸板率", microPct(s.zb_rate), `炸板 ${s.zb ?? "—"} 只 · <30% 封板质量好`,
      s.zb_rate != null && s.zb_rate > 0.5 ? "down" : ""),
    microKpi("涨跌广度", microPct(s.rise_ratio),
      `涨 ${s.rise ?? "—"} : 跌 ${s.fall ?? "—"} · 成交 ${esc((s.turnover || {}).now || "—")}`,
      s.rise_ratio > 0.6 ? "up" : (s.rise_ratio != null && s.rise_ratio < 0.4 ? "down" : "")),
    microKpi("连板梯队", s.max_board >= 2 ? `${s.max_board}板高标` : "断档",
      `情绪定性：${esc(q.mood || "—")}`, s.max_board >= 3 ? "up" : ""),
  ].join("");
  $("microLines").innerHTML = (MICRO.lines || []).map(l => `<div>${esc(l)}</div>`).join("");
  const lad = s.ladder || {};
  const ks = Object.keys(lad).sort((a, b) => Number(b) - Number(a));
  let html = "";
  if (ks.length) {
    html += "<div><b>梯队（非ST）</b><br>" + ks.map(k =>
      `<span class="pill">${k}板</span> ` + esc((lad[k] || []).join("、"))).join("<br>") + "</div>";
  } else {
    html += "<div>今日无 ≥2 板连板梯队（情绪断档，短线冰点特征）。</div>";
  }
  if ((s.top_concepts || []).length) {
    html += "<div style='margin-top:10px'><b>主流题材</b>（涨停家数）：" +
      s.top_concepts.map(c => `<span class="pill">${esc(c[0])}·${c[1]}家</span>`).join("") + "</div>";
  }
  if ((s.persistent || []).length) {
    html += `<div style="margin-top:8px"><b>持续主线</b>：${esc(s.persistent.join("、"))}` +
      ((s.new_entries || []).length ? ` · <b>新题材</b>：${esc(s.new_entries.join("、"))}` : "") + `</div>`;
  }
  $("microLadder").innerHTML = html;
  renderHeatNote();
  whenEcharts(drawMicroCharts);
}

/* 题材热度“更新到哪天 / 怎么算的”说明行（无图表时也可见） */
function renderHeatNote() {
  const hn = $("heatNote");
  if (!hn) return;
  const meta = (MICRO && MICRO.theme_heat_meta) || {};
  if (!meta.date) { hn.textContent = "暂无题材数据（收盘后生成）"; return; }
  const days = (meta.days || []).map(d => String(d).slice(5)).join("/");
  const arrow = meta.trend === "up" ? " ↑升温" : meta.trend === "down" ? " ↓降温" : " →持平";
  hn.innerHTML = `更新至 <b>${esc(meta.date)}</b>（基于 ${esc(days)} 涨停快照，权重 ${(meta.decay || []).join("/")}）`
    + (meta.market_heat != null
      ? ` ｜ 市场整体热度 <b>${meta.market_heat}</b>（涨停 ${meta.zt_latest ?? "—"} 家${meta.zt_prev != null ? "，前一日 " + meta.zt_prev : ""}${meta.trend ? arrow : ""}）` : "")
    + ((meta.ignitions || []).length ? ` ｜ 🔥 今日点火：${esc(meta.ignitions.join("、"))}` : "");
}

function drawMicroCharts() {
  if (!MICRO || !MICRO.ok || !echartsOK()) return;
  const H = MICRO.history || [];
  const c = chartInit("chMicroTrend");
  if (c) {
    const common = axisCommon();
    c.setOption(Object.assign(common, {
      xAxis: { type: "category", data: H.map(h => (h.date || "").slice(5)),
        axisLabel: { color: "#8d99ae" } },
      yAxis: [
        { type: "value", min: -100, max: 100, name: "情绪分",
          axisLabel: { color: "#8d99ae" }, splitLine: { lineStyle: { color: "#1c2740" } } },
        { type: "value", name: "家数", axisLabel: { color: "#8d99ae" },
          splitLine: { show: false } }],
      series: [
        { name: "情绪分", type: "bar", data: H.map(h => ({
            value: h.score ?? 0,
            itemStyle: { color: (h.score ?? 0) >= 0 ? "#ff4d4f" : "#2ab871" } })) },
        { name: "涨停家数", type: "line", yAxisIndex: 1, data: H.map(h => h.zt),
          smooth: true, lineStyle: { color: "#f7b731" }, itemStyle: { color: "#f7b731" } },
        { name: "跌停家数", type: "line", yAxisIndex: 1, data: H.map(h => h.dt),
          smooth: true, lineStyle: { color: "#2ab871" }, itemStyle: { color: "#2ab871" } }],
    }), true);
    c.resize();
  }
  const heat = MICRO.theme_heat || {};
  const meta = MICRO.theme_heat_meta || {};
  const rows = (meta.rows || []).slice(0, 10).reverse();   // 横向条形：自下而上递增
  const ent = rows.length
    ? rows.map(r => ({ name: (r.ignition ? "🔥" : "") + r.theme, heat: r.heat, r }))
    : Object.entries(heat).sort((a, b) => a[1] - b[1]).slice(-10)
        .map(e => ({ name: e[0], heat: e[1], r: null }));
  const hn = $("heatNote");
  if (hn) renderHeatNote();
  const ctx = MICRO.sentiment_context || {};
  if (ctx.days && $("microTrendNote")) {
    $("microTrendNote").innerHTML = `近 ${ctx.days} 个交易日情绪分布（缓存保留，辅助决策）：当前 <b>${ctx.latest}</b> 分，处于 <b>${Math.round((ctx.pct_rank || 0) * 100)}%</b> 分位（<b>${esc(ctx.label || "中性")}</b>）`
      + `｜均值 ${ctx.mean}（区间 ${ctx.min}~${ctx.max}）`
      + `｜5 日趋势 ${ctx.trend == null ? "—" : (ctx.trend > 0 ? "+" : "") + ctx.trend}`
      + `｜偏热(≥40) ${ctx.hot_days} 天、偏冷(≤−40) ${ctx.cold_days} 天`;
  }
  const c2 = chartInit("chMicroHeat");
  if (c2) {
    c2.setOption({
      tooltip: { trigger: "axis", formatter: ps => {
          const p = ps[0];
          const r = (ent[p.dataIndex] || {}).r;
          if (!r) return esc(p.name) + " 热度 " + p.value;
          return `<b>${esc(r.theme)}</b> 热度 ${r.heat}<br/>`
            + `近${r.shares.length}日占比：${(r.shares || []).map(v => (v * 100).toFixed(1) + "%").join(" → ")}<br/>`
            + `涨停家数：${(r.values || []).join(" → ")}<br/>`
            + `出现 ${r.days_seen}/${r.shares.length} 天`
            + (r.ignition ? " ｜ 🔥 今日点火（新热点）" : "");
        } },
      grid: { left: 100, right: 46, top: 12, bottom: 26 },
      xAxis: { type: "value", max: 1, axisLabel: { color: "#8d99ae" },
        splitLine: { lineStyle: { color: "#1c2740" } } },
      yAxis: { type: "category", data: ent.map(e => e.name),
        axisLabel: { color: "#cfd6e6" } },
      series: [{ type: "bar", data: ent.map(e => e.heat),
        itemStyle: { color: (p) => (ent[p.dataIndex] && ent[p.dataIndex].r
          && ent[p.dataIndex].r.ignition) ? "#f7b731" : "#ff6b6b" },
        label: { show: true, position: "right", color: "#8d99ae",
          formatter: (p) => Number(p.value).toFixed(2) } }],
    }, true);
    c2.resize();
  }
}

$("btnMicroRefresh").onclick = () => loadMicroTab(true);
