const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const r = await fetch(path, opts);
  let body = null;
  try {
    body = await r.json();
  } catch (e) {
    /* 有些错误页不是 JSON，下面按状态码兜底 */
  }
  // 之前这里只看 json 不看状态码，导致保存失败时把错误体当成正常结果，
  // rooms 被赋成 undefined，整页白屏。非 2xx 一律抛错，错误文案取后端返回的 error。
  if (!r.ok) {
    const err = body && body.error ? body.error : `请求失败（HTTP ${r.status}）`;
    const e2 = new Error(err);
    e2.status = r.status;
    throw e2;
  }
  return body || {};
}

function fmt(n, d = 2) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  return Number(n).toFixed(d);
}

function setStatus(msg, ok) {
  const s = $("status");
  s.textContent = msg || "";
  s.className = "status" + (ok === true ? " ok" : ok === false ? " err" : "");
}

function fmtDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}
function todayISO() { return fmtDate(new Date()); }
function yesterdayISO() {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return fmtDate(d);
}
const DAY_MS = 86400000;
const minISO = (a, b) => (a && a < b ? a : b);

// 默认区间 = 结束日期是**昨天**，不是今天。
// 学校每天只在 23:59 存一次用电快照，今天这一行在当天根本不存在，
// 拿今天当结束日期只会让最后一天空着（柱子是空的、表里少一行）。
function setDateRange(days) {
  const end = new Date();
  end.setDate(end.getDate() - 1);
  const begin = new Date(end);
  begin.setDate(end.getDate() - days + 1);
  $("endDate").value = fmtDate(end);
  $("beginDate").value = fmtDate(begin);
}

// 结束日期的上限：
//   用电记录 / 趋势图 —— 最多选到昨天（今天没数据，学校 23:59 才结算）
//   购电记录 —— 可以选到今天（购电是实时的，今天的购电现在就有）
// 上限直接写在 input 的 max 上，看得见；跟着标签页切。
let usageCap = "";        // 上次应用时的"昨天"，用来识别"页面开着过夜"
let endTouched = false;   // 用户手动改过结束日期就不再自动挪它
function capForTab() {
  return curTab === "purchase" ? todayISO() : yesterdayISO();
}
function applyDateLimits() {
  const y = yesterdayISO();
  const prevY = usageCap;
  usageCap = y;
  const cap = capForTab();
  const endEl = $("endDate");
  const beginEl = $("beginDate");
  endEl.max = cap;
  beginEl.max = cap;

  let changed = false;
  // 页面开着过夜：原来停在"当时的昨天"，就整体顺延同样天数，别让它第二天还指着老区间
  if (prevY && prevY !== y && !endTouched && endEl.value === prevY) {
    const shift = Math.max(1, Math.round((Date.parse(y) - Date.parse(prevY)) / DAY_MS));
    const end = new Date(Date.parse(endEl.value));
    const begin = new Date(Date.parse(beginEl.value));
    end.setDate(end.getDate() + shift);
    begin.setDate(begin.getDate() + shift);
    endEl.value = fmtDate(end);
    beginEl.value = fmtDate(begin);
    changed = true;
  }
  // 上限收紧了（如从购电切回用电）就把值压回去
  for (const el of [endEl, beginEl]) {
    if (el.value && el.value > cap) { el.value = cap; changed = true; }
  }
  // 切到购电、而结束日期还停在昨天（用电的上限）时顶到今天 —— 今天的购电是有数据的
  if (curTab === "purchase" && !endTouched && endEl.value === y) {
    endEl.value = cap;
    changed = true;
  }
  const hint = $("dateCapHint");
  if (hint) {
    hint.textContent = curTab === "purchase"
      ? `购电是实时的，结束日期可到今天（${cap}）`
      : `用电每天 23:59 才结算，结束日期最多选到昨天（${y}）`;
  }
  return changed;
}

// 后端接口版本，必须和 app.py 里的 APP_VERSION 一致。
// 页面（静态文件）每次请求都从磁盘读，改了立刻生效；Python 进程不重启就一直跑旧代码。
// 于是会出现"页面是新的、接口是旧的"，症状是新按钮报 404。用版本号把这种情况当场指出来。
const EXPECT_VERSION = "1.1";

function showStale(msg) {
  const b = $("staleBanner");
  if (!b) return;
  b.textContent = msg;
  b.classList.remove("hidden");
}

function checkVersion(cfg) {
  if (cfg.version === EXPECT_VERSION) {
    $("staleBanner").classList.add("hidden");
    return true;
  }
  showStale(
    "后台程序是旧版本（浏览器里的页面是新的，但跑着的服务还是老代码）。" +
    "请关掉那个跑着服务的命令行窗口，再双击一次 start.bat —— " +
    "因为端口被旧进程占着，直接双击只会打开浏览器、不会换成新代码。"
  );
  return false;
}

// 下拉选项都从后端来，前端不硬编码（换学校/换校区只改后端）。
//   楼栋：value = 楼栋编号（查询以它为准），显示文本 = 楼栋名
//   校区：value = 校区编号（学校登录页里那个 client），显示文本 = 校区名
let clientOptions = [];   // 兜底校区表（探测不到服务器时也能选）

function fillSelect(sel, items, placeholder, toValue, toText) {
  sel.innerHTML = "";
  if (placeholder) {
    const o0 = document.createElement("option");
    o0.value = "";
    o0.textContent = placeholder;
    sel.appendChild(o0);
  }
  for (const it of items) {
    const o = document.createElement("option");
    o.value = toValue(it);
    o.textContent = toText(it);
    sel.appendChild(o);
  }
}

function setBuildings(list, keepValue) {
  const sel = $("cfgBuilding");
  const keep = keepValue || sel.value;
  fillSelect(sel, list || [], "请选择楼栋", (b) => b.id, (b) => b.name);
  if (keep) sel.value = keep;
}

function setClients(list, keepValue) {
  const sel = $("cfgClient");
  const keep = keepValue || sel.value;
  fillSelect(sel, list || [], "", (c) => c.value, (c) => `${c.name}`);
  if (keep) sel.value = keep;
}

async function loadDormOptions() {
  const d = await api("/api/dorm-options");
  clientOptions = d.clients || [];
  setBuildings(d.buildings || []);
  setClients(clientOptions);
}

// ── 自动检测：探服务器 → 拿校区表 → 拿该校区的楼栋表 ─────────────────
let buildingSource = "builtin";   // builtin = 用的内置兜底表；live = 从学校页面现取的

async function runDetect(client) {
  const btn = $("cfgDetect");
  const msg = $("detectMsg");
  const cur = client != null ? client : $("cfgClient").value;
  btn.disabled = true;
  msg.className = "inline-msg";
  msg.textContent = "检测中…";
  try {
    const q = cur ? `?client=${encodeURIComponent(cur)}` : "";
    const d = await api("/api/detect" + q);
    if (!d.ok) throw new Error(d.error || "检测失败");
    if (d.server) $("cfgServer").value = d.server;
    if (d.clients && d.clients.length) {
      clientOptions = d.clients;
      setClients(d.clients, d.client || cur);
    }
    const keepBuilding = $("cfgBuilding").value;
    if (d.buildings && d.buildings.length) {
      setBuildings(d.buildings, keepBuilding);
      buildingSource = d.source;
    }
    msg.className = "inline-msg ok";
    msg.textContent = `已连上 ${d.server}${d.clientName ? "（" + d.clientName + "）" : ""}`;
  } catch (e) {
    msg.className = "inline-msg err";
    msg.textContent = e.message;
    // 404 基本只有一个原因：后台进程还是旧代码（新接口不存在）
    if (e.status === 404) {
      showStale(
        "这个功能需要新版后台程序，但现在跑着的是旧进程（接口不存在，HTTP 404）。" +
        "请关掉那个跑着服务的命令行窗口，再双击一次 start.bat。"
      );
    }
  } finally {
    btn.disabled = false;
  }
}

async function loadConfig() {
  const cfg = await api("/api/config");
  checkVersion(cfg);   // 后台进程是旧的就把横幅挂出来，别让用户对着 404 猜
  $("cfgRoom").value = cfg.room || "";
  $("roomTag").textContent = [cfg.building, cfg.room].filter(Boolean).join(" ") || "未配置宿舍";
  $("cfgServer").value = cfg.server || "";
  $("cfgDays").value = cfg.defaultRangeDays || 30;
  $("cfgPurchaseUrl").value = cfg.purchaseUrl || "";
  // 校区：配置里有就选它，没有就等检测结果
  if (cfg.client) {
    if (!clientOptions.some((c) => c.value === cfg.client)) {
      clientOptions = clientOptions.concat([
        { value: cfg.client, name: cfg.clientName || cfg.client },
      ]);
    }
    setClients(clientOptions, cfg.client);
  }
  // 楼栋：优先按编号选中（内置表里没有的楼栋，编号也能对上）
  const sel = $("cfgBuilding");
  if (cfg.buildingId) {
    if (!Array.from(sel.options).some((o) => o.value === cfg.buildingId)) {
      const o = document.createElement("option");
      o.value = cfg.buildingId;
      o.textContent = cfg.building || cfg.buildingId;
      sel.appendChild(o);
    }
    sel.value = cfg.buildingId;
  } else if (cfg.building) {
    sel.value = cfg.building;
  }

  // 顶栏快捷方式：
  //   「学校原页面」= 走本机 /school 中转页，自动把楼栋+房号 POST 进学校登录表单，
  //     所以点开就是"你所在的宿舍"那个页面，不用再重复登录、再选楼栋填房号；
  //   「扫码购电」= 用户填的缴费入口，没填就整条藏掉。
  //   注意这里给的是二维码而不是链接：缴费页非微信 UA 会被强制弹「请在微信客户端打开」，
  //   在电脑上点链接是打不开的，扫码才是对的姿势。
  const base = String(cfg.server || "").replace(/\/+$/, "");
  const client = encodeURIComponent(cfg.client || "");
  const lOriginal = $("linkOriginal");
  const dormLabel = [cfg.building, cfg.room].filter(Boolean).join(" ");
  if (base && client && cfg.building && cfg.room) {
    // 直接跳 /school：由后端拼登录表单并自动提交，不会再停在登录页
    lOriginal.href = "/school";
    lOriginal.title = `以已登录状态打开学校原页面（自动带上 ${dormLabel}）`;
    lOriginal.hidden = false;
  } else {
    lOriginal.hidden = true;
  }
  setPurchaseUrl(cfg.purchaseUrl || "");

  setDateRange(7);
  applyDateLimits();
  return cfg;
}

// 购电二维码：把配置的购电网址交给后端渲染成 SVG，前端不引任何二维码库。
// url 变了就强制重新加载（加时间戳破缓存），免得扫出来还是上一张。
let payQrUrl = "";
function setPurchaseUrl(url) {
  payQrUrl = url || "";
  $("openPayQr").hidden = !payQrUrl;
  $("payQrLink").hidden = !payQrUrl;
  if (payQrUrl) $("payQrLink").href = payQrUrl;
}

function showPayQr() {
  if (!payQrUrl) return;
  const img = $("payQr");
  const err = $("payQrErr");
  err.hidden = true;
  img.style.opacity = "0";
  img.src = "/api/qrcode.svg?url=" + encodeURIComponent(payQrUrl) + "&t=" + Date.now();
  img.onload = () => { img.style.opacity = "1"; };
  img.onerror = () => {
    err.textContent = "二维码生成失败，检查「修改宿舍 → 高级设置 → 购电网址」是不是填错了";
    err.hidden = false;
  };
  $("payQrModal").classList.remove("hidden");
}

$("openPayQr").onclick = showPayQr;
$("payQrClose").onclick = () => $("payQrModal").classList.add("hidden");

// 本地缓存：先秒开旧数据，再后台刷新（force=1 才真正去学校抓）
async function showCached(kind) {
  const begin = $("beginDate").value;
  const end = $("endDate").value;
  if (!begin || !end) return false;
  try {
    const c = await api(`/api/cached?begin=${encodeURIComponent(begin)}&end=${encodeURIComponent(end)}&kind=${kind}`);
    if (!c.ok || !c[kind]) return false;
    render(c[kind]);
    if (curTab === "purchase") {
      const p = c.purchase;
      if (p) renderPurchase(p.headers || [], p.rows || []);
    }
    if (c.updated_at) setStatus("数据更新于 " + c.updated_at, true);
    return true;
  } catch (e) {
    return false;
  }
}

async function refresh(force) {
  const begin = $("beginDate").value;
  // 用电侧的结束日期硬性不超过昨天：学校 23:59 才结算，今天的行此刻根本不存在。
  // （购电标签页的结束日期可以到今天，那边走 /api/purchase，不受这里限制）
  const end = minISO($("endDate").value, yesterdayISO());
  if (!begin || !end) { setStatus("请选择起止日期", false); return; }
  const qs = `begin=${encodeURIComponent(begin)}&end=${encodeURIComponent(end)}`
    + (force ? "&force=1" : "");
  setStatus("查询中…");
  try {
    const t = await api(`/api/trend?${qs}`);
    if (!t.ok) {
      setStatus(t.error, false);
      clearCharts();
      return;
    }
    render(t);
    setStatus(t.cached
      ? "数据更新于 " + t.updated_at
      : "更新于 " + new Date().toLocaleTimeString(), true);
    // 日期变化时，购电记录也要同步刷新（若当前正在看购电标签）
    if (curTab === "purchase") await loadPurchase(force);
  } catch (e) {
    setStatus("请求失败：" + e.message, false);
  }
}

// 页面打开：先拿缓存秒开，同时后台静默刷新一次
async function boot() {
  const painted = await showCached("trend");
  if (painted) refresh();
  else await refresh(true);
}

let curLabels = [], curRemain = [], curDaily = [];

function render(t) {
  $("vRemain").textContent = fmt(t.current_remain);

  // 「当前余额」要标清是哪天结算出来的：学校每天只在 23:59 存一次快照，
  // 所以这个数是那天的收盘值，不是此刻的实时读数。
  // 标题「当前余额」保持原样，日期只作为下面一行小字补充；没有记录就整行不显示。
  const asOf = $("vRemainAsOf");
  if (t.current_remain == null || !t.latest_date) {
    asOf.hidden = true;
  } else {
    asOf.hidden = false;
    asOf.textContent = "截至 " + t.latest_date;
  }

  // 「今日用电」整张卡片按"爬到才显示"处理。
  // 服务端每天 23:59 才结算出今天的记录，白天数据表里没有今天这一行，
  // 这个量无解 —— 那就让卡片直接消失，不显示、不猜、不拿昨天那格补位。
  // 等哪天服务端真吐出今天的记录了，卡片会自动出现并显示真实数字。
  const cardToday = $("cardToday");
  if (t.today_use == null) {
    cardToday.hidden = true;
  } else {
    cardToday.hidden = false;
    $("vToday").textContent = fmt(t.today_use);
  }
  $("vAvg").textContent = fmt(t.avg_daily);
  $("vEst").textContent = t.estimated_days == null ? "—" : t.estimated_days;

  curLabels = t.dates || [];
  curRemain = t.remain || [];
  curDaily = t.daily_use || [];
  drawCharts();

  const tb = $("history").querySelector("tbody");
  tb.innerHTML = "";
  const recs = (t.records || []).slice().reverse();
  for (const r of recs) {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td>" + r.date + "</td><td>" + fmt(r.remain) + "</td>";
    tb.appendChild(tr);
  }
}

function drawCharts(hover) {
  drawLine($("lineChart"), curLabels, curRemain, "#36d399", hover, "度");
  drawBar($("barChart"), curLabels, curDaily, "#7c5cff", hover, "度");
}

function clearCharts() {
  for (const id of ["lineChart", "barChart"]) {
    const c = $(id);
    const ctx = c.getContext("2d");
    ctx.clearRect(0, 0, c.width, c.height);
  }
  $("history").querySelector("tbody").innerHTML = "";
}

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, rect.width * dpr);
  canvas.height = Math.max(1, rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return { ctx, w: rect.width, h: rect.height };
}

function drawLine(canvas, labels, data, color, hover, unit) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 46, padR = 14, padT = 14, padB = 26;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const vals = data.filter((v) => v != null && !isNaN(v));
  if (!vals.length) { canvas._state = { pts: [], unit }; return; }
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const n = data.length;
  const x = (i) => padL + (n <= 1 ? plotW / 2 : (plotW * i) / (n - 1));
  const y = (v) => padT + plotH - ((v - min) / span) * plotH;

  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.fillStyle = "rgba(231,235,240,0.5)";
  ctx.font = "11px system-ui";
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (plotH * g) / 4;
    ctx.beginPath();
    ctx.moveTo(padL, gy);
    ctx.lineTo(w - padR, gy);
    ctx.stroke();
    ctx.fillText((max - (span * g) / 4).toFixed(1), 4, gy + 4);
  }
  // 0 刻度线加粗强调：0 在范围内则画真实 0 线，否则强调最低刻度线（视觉基准）
  const zeroY =
    min <= 0 && max >= 0 ? padT + plotH - ((0 - min) / span) * plotH : padT + plotH;
  const zeroLabel = min <= 0 && max >= 0 ? "0" : min.toFixed(1);
  ctx.strokeStyle = "rgba(248,114,114,0.9)";
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  ctx.moveTo(padL, zeroY);
  ctx.lineTo(w - padR, zeroY);
  ctx.stroke();
  ctx.fillStyle = "rgba(248,114,114,0.95)";
  ctx.font = "bold 11px system-ui";
  ctx.fillText(zeroLabel, 3, zeroY + 4);
  ctx.lineWidth = 1;
  ctx.fillStyle = "rgba(231,235,240,0.5)";
  ctx.font = "11px system-ui";
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  grad.addColorStop(0, color + "55");
  grad.addColorStop(1, color + "08");
  ctx.beginPath();
  ctx.moveTo(x(0), y(data[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(data[i]));
  ctx.lineTo(x(n - 1), padT + plotH);
  ctx.lineTo(x(0), padT + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(x(0), y(data[0]));
  for (let i = 1; i < n; i++) ctx.lineTo(x(i), y(data[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "rgba(231,235,240,0.5)";
  const step = Math.ceil(n / 6);
  for (let i = 0; i < n; i += step) {
    ctx.fillText(String(labels[i]).slice(5), x(i) - 12, h - 8);
  }

  const pts = data.map((v, i) => ({
    x: x(i),
    y: (v == null || isNaN(v)) ? null : y(v),
    label: String(labels[i] || ""),
    value: v,
  }));
  canvas._state = { pts, unit };
  if (hover != null && pts[hover] && pts[hover].y != null) {
    const p = pts[hover];
    ctx.strokeStyle = "rgba(255,255,255,0.28)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(p.x, padT);
    ctx.lineTo(p.x, padT + plotH);
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }
}

function drawBar(canvas, labels, data, color, hover, unit) {
  const { ctx, w, h } = setupCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 46, padR = 14, padT = 14, padB = 26;
  const plotW = w - padL - padR, plotH = h - padT - padB;
  const vals = data.filter((v) => v != null && !isNaN(v));
  const max = vals.length ? Math.max(...vals) : 1;
  const n = data.length;
  const gap = plotW / n;
  const bw = gap * 0.7;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.fillStyle = "rgba(231,235,240,0.5)";
  ctx.font = "11px system-ui";
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (plotH * g) / 4;
    ctx.beginPath();
    ctx.moveTo(padL, gy);
    ctx.lineTo(w - padR, gy);
    ctx.stroke();
    ctx.fillText((max - (max * g) / 4).toFixed(1), 4, gy + 4);
  }
  const pts = [];
  for (let i = 0; i < n; i++) {
    const v = data[i];
    if (v == null || isNaN(v)) { pts.push(null); continue; }
    const bx = padL + gap * i + (gap - bw) / 2;
    const bh = (v / (max || 1)) * plotH;
    const by = padT + plotH - bh;
    ctx.fillStyle = color;
    ctx.fillRect(bx, by, bw, bh);
    pts.push({ x: bx + bw / 2, y: by, label: String(labels[i] || ""), value: v });
  }
  ctx.fillStyle = "rgba(231,235,240,0.5)";
  const step = Math.ceil(n / 6);
  for (let i = 0; i < n; i += step) {
    ctx.fillText(String(labels[i]).slice(5), padL + gap * i + gap / 2 - 12, h - 8);
  }

  canvas._state = { pts, unit };
  if (hover != null && pts[hover] && pts[hover].y != null) {
    const p = pts[hover];
    ctx.fillStyle = "rgba(255,255,255,0.85)";
    ctx.fillRect(p.x - bw / 2, p.y, bw, padT + plotH - p.y);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }
}

// 图表悬停提示
let tipEl = null;
function showTip(cx, cy, label, value, unit) {
  if (!tipEl) {
    tipEl = document.createElement("div");
    tipEl.className = "tooltip hidden";
    document.body.appendChild(tipEl);
  }
  if (value == null || isNaN(value)) { tipEl.classList.add("hidden"); return; }
  tipEl.innerHTML =
    `<div class="t-date">${label}</div><div class="t-val">${Number(value).toFixed(2)} ${unit || ""}</div>`;
  tipEl.classList.remove("hidden");
  const r = tipEl.getBoundingClientRect();
  let x = cx + 14, y = cy + 14;
  if (x + r.width > window.innerWidth) x = cx - r.width - 14;
  if (y + r.height > window.innerHeight) y = cy - r.height - 14;
  tipEl.style.left = x + "px";
  tipEl.style.top = y + "px";
}
function hideTip() { if (tipEl) tipEl.classList.add("hidden"); }

function attachHover() {
  for (const [id] of [["lineChart"], ["barChart"]]) {
    const c = $(id);
    c.addEventListener("mousemove", (e) => {
      const st = c._state;
      if (!st || !st.pts.length) return;
      const rect = c.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      let bi = -1, best = null;
      st.pts.forEach((p, i) => {
        if (!p || p.x == null) return;
        const d = Math.abs(p.x - mx);
        if (best == null || d < best) { best = d; bi = i; }
      });
      if (bi < 0) return;
      drawCharts(bi);
      showTip(e.clientX, e.clientY, st.pts[bi].label, st.pts[bi].value, st.unit);
    });
    c.addEventListener("mouseleave", () => { hideTip(); drawCharts(); });
  }
}

// 购电记录标签页
let curTab = "usage";
function switchTab(tab) {
  curTab = tab;
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  $("history").classList.toggle("hidden", tab !== "usage");
  $("purchase").classList.toggle("hidden", tab !== "purchase");
  // 结束日期的上限跟着标签页走（用电到昨天 / 购电到今天），值被挪过就要重查
  const moved = applyDateLimits();
  if (tab === "purchase") loadPurchase();
  else if (moved) refresh();
}
async function loadPurchase(force) {
  const begin = $("beginDate").value;
  const end = $("endDate").value;
  if (!begin || !end) { setStatus("请选择起止日期", false); return; }
  const qs = `begin=${encodeURIComponent(begin)}&end=${encodeURIComponent(end)}`
    + (force ? "&force=1" : "");
  setStatus("加载购电记录…");
  try {
    const d = await api(`/api/purchase?${qs}`);
    if (!d.ok) { setStatus(d.error, false); return; }
    renderPurchase(d.headers || [], d.rows || []);
    setStatus(d.cached && d.updated_at
      ? "购电记录更新于 " + d.updated_at
      : "购电记录已更新", true);
  } catch (e) {
    setStatus("购电记录请求失败：" + e.message, false);
  }
}
// 不展示的列（按表头关键词匹配）：购买形式、房名
// 「购买者」列是充值流水号（同一人连续充值会产生多个号），不承载任何身份信息，故一并隐藏
const HIDE_COL_KEYS = ["购买形式", "房名", "购买者"];
function colVisible(h) {
  if (h == null) return true;
  return !HIDE_COL_KEYS.some((k) => String(h).includes(k));
}
// 购买日期精简：忽略年、秒 => "09-22 12:37"
function shortTime(s) {
  if (s == null) return "";
  const m = String(s).match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T]+(\d{1,2}):(\d{2})/);
  if (!m) return String(s);
  const mo = String(m[2]).padStart(2, "0");
  const d = String(m[3]).padStart(2, "0");
  const hh = String(m[4]).padStart(2, "0");
  return `${mo}-${d} ${hh}:${m[5]}`;
}
const isDateHeader = (h) => h != null && String(h).includes("日期");

function renderPurchase(headers, rows) {
  const head = $("purchaseHead");
  const keep = headers.map((h, i) => (colVisible(h) ? i : -1)).filter((i) => i >= 0);
  head.innerHTML = keep.length
    ? keep.map((i) => `<th>${headers[i] == null ? "" : headers[i]}</th>`).join("")
    : "<th>暂无表头</th>";
  const tb = $("purchase").querySelector("tbody");
  tb.innerHTML = "";
  if (!rows.length) {
    $("purchaseEmpty").classList.remove("hidden");
    return;
  }
  $("purchaseEmpty").classList.add("hidden");
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = keep
      .map((i) => {
        let val = r[i] == null ? "" : r[i];
        if (isDateHeader(headers[i])) val = shortTime(val);
        return `<td>${val}</td>`;
      })
      .join("");
    tb.appendChild(tr);
  }
}

// 配置弹窗：三步向导。用户只需要会「点自动检测 → 选校区 → 选楼栋 + 填门牌号」，
// roomid 这类内部编号一律不暴露。
function openConfig(autoDetect) {
  const r = $("cfgRoom");
  r.value = r.value.replace(/[^0-9A-Za-z\-]/g, "");
  $("cfgMsg").textContent = "";
  $("configModal").classList.remove("hidden");
  // 还没配服务器（第一次用）：进来就替他把检测跑掉，别让他自己找按钮
  if (autoDetect && !$("cfgServer").value.trim()) runDetect();
}
$("openConfig").onclick = () => openConfig(false);
$("cfgCancel").onclick = () => $("configModal").classList.add("hidden");

// 自动检测按钮 + 换校区后刷新楼栋表（楼栋是按校区分的，换校区必须重新取）
$("cfgDetect").onclick = () => runDetect();
$("cfgClient").onchange = () => runDetect($("cfgClient").value);

// 楼栋下拉一旦改了，房间号输入框聚焦，提醒用户确认层段对不对
$("cfgBuilding").onchange = () => $("cfgRoom").focus();

$("cfgSave").onclick = async () => {
  const sel = $("cfgBuilding");
  const opt = sel.options[sel.selectedIndex];
  const body = {
    // 名字给人看、编号给服务端用：楼栋编号直接带着走，
    // 这样学校新加的楼栋、或者换了校区（楼栋不在内置表里）也能正常查
    building: opt && opt.value ? opt.textContent : "",
    buildingId: sel.value,
    room: $("cfgRoom").value.trim(),
    server: $("cfgServer").value.trim(),
    client: $("cfgClient").value,
    clientName: (() => {
      const c = $("cfgClient");
      const o = c.options[c.selectedIndex];
      return o && o.value ? o.textContent : "";
    })(),
    defaultRangeDays: parseInt($("cfgDays").value, 10) || 30,
    purchaseUrl: $("cfgPurchaseUrl").value.trim(),
  };
  $("cfgSave").disabled = true;
  try {
    const r = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (r.ok === false) throw new Error(r.error || "保存被拒绝");
    $("roomTag").textContent = [r.building, r.room].filter(Boolean).join(" ") || "未配置宿舍";
    // 保存后顶栏快捷方式可能整条出现/消失（比如刚填了购电网址），跟 loadConfig 走一套逻辑
    setPurchaseUrl(r.purchaseUrl || "");
    $("cfgMsg").textContent = "已保存，正在查询…";
    $("cfgMsg").className = "status ok";
    setTimeout(() => {
      $("configModal").classList.add("hidden");
      $("cfgMsg").textContent = "";
    }, 500);
    refresh(true);
  } catch (e) {
    $("cfgMsg").textContent = "保存失败：" + e.message;
    $("cfgMsg").className = "status err";
  } finally {
    $("cfgSave").disabled = false;
  }
};

$("refresh").onclick = () => refresh(true);
document.querySelectorAll(".range-btn").forEach((b) => {
  b.onclick = () => { endTouched = false; setDateRange(parseInt(b.dataset.range, 10)); refresh(true); };
});
// 用户自己动过结束日期，之后切标签页/跨天就不再自动挪它（尊重手动选择）
$("endDate").oninput = () => { endTouched = true; };
$("beginDate").oninput = () => { endTouched = true; };
[$("beginDate"), $("endDate")].forEach((el) => { if (el) el.onchange = () => refresh(true); });
document.querySelectorAll(".tab").forEach((b) => {
  b.onclick = () => switchTab(b.dataset.tab);
});
let rz;
window.addEventListener("resize", () => {
  clearTimeout(rz);
  rz = setTimeout(refresh, 200);
});

// 学校每天 23:59 才结算出当天的行，那天之后"今日用电"才从 — 变成真实数字。
// 所以切回这个标签页时自动重查一次：跨过 23:59 回来就能看到，不用盯着刷新。
// 同时重算日期上限 —— 页面开着过夜时，"昨天"会往前走一天，区间要跟着顺延。
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    applyDateLimits();
    refresh();
  }
});

attachHover();

loadDormOptions()
  .catch(() => {})
  .then(loadConfig)
  .then((cfg) => {
    // 第一次用（配置不全）：直接把三步向导弹出来，免得用户在空面板前发呆
    const ready = cfg.server && cfg.client && cfg.room && (cfg.building || cfg.buildingId);
    if (!ready) {
      openConfig(true);
      setStatus("先按弹窗里的 1 · 2 · 3 设置一次宿舍");
      return null;
    }
    return refresh();
  })
  .catch((e) => setStatus("初始化失败：" + e.message, false));
