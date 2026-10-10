const ROUTES = [
  {
    id: "alpha",
    label: { zh: "阿尔法工厂", en: "Alpha Factory" },
    copy: { zh: "策略候选竞赛、forward shadow / dry-run 综合绩效、候选池集中度。", en: "Strategy race, forward shadow/dry-run performance, candidate pool concentration." },
  },
  {
    id: "dashboard",
    label: { zh: "总览", en: "Dashboard" },
    copy: { zh: "PnL、告警、信号与运行状态总览。", en: "PnL, alerts, signals, and runtime status." },
  },
  {
    id: "replay",
    label: { zh: "复盘", en: "Replay" },
    copy: { zh: "signal / shadow / 实盘统一时间轴。", en: "Timeline replay across signals, shadow, and trade outcomes." },
  },
  {
    id: "markets",
    label: { zh: "行情", en: "Markets" },
    copy: { zh: "事件推导价格带、池列表和信号叠加。", en: "Event-derived price tape, pools, and signal overlays." },
  },
  {
    id: "execution",
    label: { zh: "执行", en: "Execution" },
    copy: { zh: "持仓、交易归因与每日评估。", en: "Positions, trades, attribution, and daily evaluation." },
  },
  {
    id: "strategies",
    label: { zh: "策略", en: "Strategies" },
    copy: { zh: "工作流状态、信号队列与人工复核。", en: "Workflow state, signal cohorts, and operator review." },
  },
  {
    id: "risk",
    label: { zh: "风控", en: "Risk" },
    copy: { zh: "回撤、活动告警与受控操作。", en: "Drawdown, active alerts, and gated control actions." },
  },
  {
    id: "guide",
    label: { zh: "导览", en: "Guide" },
    copy: { zh: "项目介绍、阶段、术语与更新日志。", en: "Project intro, phases, glossary, and changelog." },
  },
  {
    id: "settings",
    label: { zh: "设置", en: "Settings" },
    copy: { zh: "安全姿态、部署模式与驾驶舱限制。", en: "Security posture, deployment mode, and cockpit limits." },
  },
];

const UI = {
  zh: {
    status_live: "运行中",
    status_panic: "紧急停止",
    status_watch: "观察",
    status_healthy: "正常",
    last_refresh: "最近刷新",
    ledger_rows: "条台账记录",
    runtime: "运行时",
    security: "安全",
    operator: "操作台",
    mode_controlled: "受控模式",
    mode_read_only: "只读模式",
    refresh_now: "立即刷新",
    set_action_token: "设置动作令牌",
    update_action_token: "更新动作令牌",
    action_token_ready: "动作令牌已就绪",
    action_failed: "操作失败",
    alert_acknowledged: "告警已确认",
    signal_approved: "信号已批准",
    signal_observed: "信号已标记为观察",
    signal_rejected: "信号已拒绝",
    panic_set: "SYSTEM_PANIC 已设置",
    panic_cleared: "SYSTEM_PANIC 已清除",
    prompt_action_token: "请输入 dashboard action token",
    refresh_failed_prefix: "刷新失败：",
    route_suffix: "首版默认关闭交易写入口，聚焦可视化、复盘和受控操作。",
    headline_kicker: "量化驾驶舱 v1",
  },
  en: {
    status_live: "LIVE",
    status_panic: "PANIC",
    status_watch: "WATCH",
    status_healthy: "HEALTHY",
    last_refresh: "Last refresh",
    ledger_rows: "ledger rows",
    runtime: "Runtime",
    security: "Security",
    operator: "Operator",
    mode_controlled: "Controlled",
    mode_read_only: "Read-only",
    refresh_now: "Refresh now",
    set_action_token: "Set action token",
    update_action_token: "Update action token",
    action_token_ready: "Action token ready",
    action_failed: "Action failed",
    alert_acknowledged: "Alert acknowledged",
    signal_approved: "Signal approved",
    signal_observed: "Signal observed",
    signal_rejected: "Signal rejected",
    panic_set: "SYSTEM_PANIC set",
    panic_cleared: "SYSTEM_PANIC cleared",
    prompt_action_token: "Enter dashboard action token",
    refresh_failed_prefix: "Failed to refresh: ",
    route_suffix: "The first cut keeps trading writes off the surface and focuses on visualization, replay, and gated operator controls.",
    headline_kicker: "quant cockpit v1",
  },
};

const EXACT_TEXT_ZH = {
  "Live runtime snapshot": "实时运行快照",
  "No active alert. Runtime surface is clean right now.": "当前没有活动告警，运行面暂时平稳。",
  "Action: ": "处理建议：",
  "Acknowledged at ": "已确认于 ",
  "No rows yet.": "暂无数据。",
  "Equity and realized curve": "权益与已实现收益曲线",
  "Cumulative realized PnL from executor sells. This is the cockpit anchor for daily oversight.": "来自 executor 卖出流水的累计已实现 PnL，是日常总览的核心锚点。",
  "Active alerts": "活动告警",
  "Operator-facing warnings and circuit-breaker state.": "面向操作员的告警与熔断状态。",
  "Recent signals": "最近信号",
  "Fresh entries from the signals stream.": "最近写入 signals 流的信号。",
  "Recent trades": "最近交易",
  "Executor logs with PnL and exit reason.": "executor 日志中的盈亏与退出原因。",
  "Markets watch": "市场观察",
  "No active pool": "暂无活跃池",
  "Price tape and signal overlay": "价格带与信号叠加",
  "Derived from raw swap events. Buy/sell markers come from the signals stream and executor ledger.": "基于原始 swap 事件生成。买卖标记来自 signals 流和 executor 台账。",
  "Pool context": "池上下文",
  "Selected pool metadata and live runtime linkage.": "当前池元数据和运行时关联信息。",
  "Recent pool-linked signals": "最近池级信号",
  "Signals sorted by most recent runtime publication.": "按运行时发布时间倒序展示。",
  "Open positions": "当前持仓",
  "Today realized PnL": "今日已实现盈亏",
  "Live inventory as tracked by the executor position store.": "来自 executor 持仓存储的实时库存。",
  "Attribution": "归因分析",
  "PnL grouping from exec:trades, aligned with trade attribution tooling.": "基于 exec:trades 的盈亏分组，与归因工具保持一致。",
  "Trade ledger": "交易流水",
  "Buy/sell events with source, cap, and realized PnL.": "买卖事件、来源、仓位上限与已实现 PnL。",
  "Daily evaluation": "每日评估",
  "The same executor-generated summary surface used for gate checks.": "与 gate 检查共用的 executor 日结面。",
  "No daily summaries have been emitted yet.": "还没有产生日结摘要。",
  "Strategy workflow": "策略工作流",
  "Readable operator map of the current signal-to-execution lifecycle.": "当前信号到执行链路的可读工作流视图。",
  "Shadow cohort": "Shadow 队列表现",
  "Closed shadow performance grouped by signal source.": "按信号来源分组的已关闭 shadow 表现。",
  "Pending operator reviews": "待处理人工复核",
  "V1 controlled interaction layer. Reviews are stored separately from the runtime stream.": "V1 受控交互层。review 结果与运行时流分离存储。",
  "Unified timeline": "统一时间线",
  "Signals, shadow events, and trades merged into a replay-friendly stream.": "将信号、shadow 和交易汇总成可回放时间线。",
  signal: "信号",
  trade: "实盘成交",
  shadow: "纸面影子",
  "Closed shadow records": "已关闭 shadow 记录",
  "Replay candidates with hold time and executable quote return.": "用于复盘的候选，包含持有时间和可执行报价回报。",
  "Risk surface": "风险面",
  "Alerts are generated from panic, concurrency, drawdown, signal freshness, and shadow quality.": "告警来自 panic、并发、回撤、信号新鲜度和 shadow 质量。",
  "Controlled actions": "受控操作",
  "Only a very small write surface is enabled in V1.": "V1 只开放极小的写操作面。",
  "Panic reason": "熔断原因",
  "manual dashboard panic": "dashboard 手动熔断",
  "Set SYSTEM_PANIC": "设置 SYSTEM_PANIC",
  "Clear SYSTEM_PANIC": "清除 SYSTEM_PANIC",
  "Write actions require the X-Dashboard-Action-Token secret and remain separate from executor trade writes.": "写操作需要 X-Dashboard-Action-Token，且与 executor 的交易写接口保持隔离。",
  "Security baseline": "安全基线",
  "The cockpit is designed for reverse proxy deployment with a minimal public surface.": "驾驶舱按最小公网暴露面设计，默认用于反向代理部署。",
  "Bind": "监听地址",
  "Basic auth": "基础认证",
  "Action token": "动作令牌",
  "Read-only": "只读",
  "Panic actions": "熔断操作",
  "Cockpit limits": "驾驶舱限制",
  "Useful runtime caps and data loading boundaries.": "当前运行上限和加载边界。",
  "Max concurrent positions": "最大并发持仓",
  "Signals loaded": "已加载信号",
  "Shadow loaded": "已加载 shadow 记录",
  "Deployment notes": "部署说明",
  "Recommended VPS posture for this dashboard runtime.": "该 dashboard runtime 的推荐 VPS 部署姿态。",
  "Expose only 80/443 at the reverse proxy layer.": "反向代理层只暴露 80/443。",
  "Keep dashboard container bound to 127.0.0.1 by default.": "默认将 dashboard 容器绑定到 127.0.0.1。",
  "Do not publish Redis, Postgres, ClickHouse, or strategy services to the public internet.": "不要把 Redis、Postgres、ClickHouse 或 strategy 服务直接暴露到公网。",
  "Use Basic Auth for read access and an action token for the small write surface.": "读访问使用 Basic Auth，小范围写操作使用 action token。",
  "EATLQ runtime": "EATLQ 运行时",
  "Quant Cockpit": "量化驾驶舱",
  "A high-density operator shell for strategy, execution, replay, and risk.": "面向策略、执行、复盘和风险的高密度操作壳层。",
  "Status": "状态",
  "Chain": "链",
  "Update": "更新时间",
  "Mode": "模式",
  "Pool": "池",
  "Price": "价格",
  "Type": "类型",
  "Fee": "费率",
  "Token": "代币",
  "Candles": "K 线数",
  "Markers": "标记数",
  "Time": "时间",
  "Source": "来源",
  "Score": "得分",
  "Review": "复核",
  "Action": "动作",
  "Reason": "原因",
  "Opened": "开仓时间",
  "Entry": "入场",
  "Age": "时长",
  "Tx": "交易",
  "Decision": "处理",
  "Return": "回报",
  "Hold": "持有",
  "Control mode": "控制模式",
  "Drawdown": "回撤",
  "Max drawdown": "最大回撤",
  "Cap hits": "仓位上限触发",
  "Latest signal": "最近信号",
  "Executor size cap interventions": "executor 仓位上限触发次数",
  "Stream freshness checkpoint": "信号流新鲜度检查点",
  "from peak": "相对峰值",
  "pending": "待处理",
  "dry-run": "试运行",
  "enabled": "已启用",
  "disabled": "已禁用",
  "read-only": "只读",
  "controlled": "受控",
  "healthy": "正常",
  "watch": "观察",
  "critical": "严重",
  "warning": "警告",
  "live": "运行中",
  "panic": "紧急停止",
  "No source attribution yet.": "暂无来源归因数据。",
  "Shadow records need to accumulate before cohort insights appear.": "shadow 记录需要继续积累后才会显示队列洞察。",
};

const state = {
  data: null,
  route: (location.hash || "#dashboard").slice(1),
  selectedPool: null,
  locale: localStorage.getItem("dashboard-locale") || "zh",
  actionToken: localStorage.getItem("dashboard-action-token") || "",
  refreshTimer: null,
  toastTimer: null,
  charts: {},
};

function tr(key) {
  return (UI[state.locale] || UI.en)[key] || UI.en[key] || key;
}

function routeLabel(route) {
  return route.label[state.locale] || route.label.en;
}

function routeCopy(route) {
  return route.copy[state.locale] || route.copy.en;
}

function localizeExact(text) {
  if (state.locale !== "zh") return text;
  return EXACT_TEXT_ZH[text] || text;
}

function localizeDynamicText(text) {
  if (state.locale !== "zh") return text;
  let out = localizeExact(String(text ?? ""));
  out = out.replace(/^No recent signal in the last 15 minutes\.$/, "最近 15 分钟内没有新信号。");
  out = out.replace(/^Win rate is ([\d.]+)% across (\d+) closed trades\.$/, "已平仓 $2 笔，胜率为 $1%。");
  out = out.replace(/^Peak-to-trough drawdown is ([\d.]+)%\.$/, "峰谷回撤为 $1%。");
  out = out.replace(/^Average closed shadow return is ([+\-\d.]+)%\.$/, "已关闭 shadow 平均收益为 $1%。");
  out = out.replace(/^(\d+) signals \/ hour$/, "$1 条信号 / 小时");
  out = out.replace(/^(\d+) shadow closes$/, "$1 条 shadow 平仓");
  out = out.replace(/^(\d+) smart-money prompts$/, "$1 条 smart-money 提示");
  out = out.replace(/^SYSTEM_PANIC clear$/, "SYSTEM_PANIC 已清除");
  out = out.replace(/^([A-Z]+) · ([+\-\d.]+) USD$/, (_, side, pnl) => `${side === "BUY" ? "买入" : "卖出"} · ${pnl} USD`);
  out = out.replace(/^([a-zA-Z_]+) · score ([\d.]+)$/, "$1 · 得分 $2");
  out = out.replace(/^([a-zA-Z_]+) · ([+\-\d.]+)%$/, "$1 · $2%");
  return out;
}

function localizeApiData(data) {
  if (state.locale !== "zh") return data;
  const cloned = JSON.parse(JSON.stringify(data));
  cloned.status = localizeExact(cloned.status);
  cloned.meta.dashboard_mode = localizeExact(cloned.meta.dashboard_mode);
  cloned.meta.security.basic_auth = localizeExact(cloned.meta.security.basic_auth);
  cloned.meta.security.action_token = localizeExact(cloned.meta.security.action_token);
  cloned.meta.security.read_only = localizeExact(cloned.meta.security.read_only);
  cloned.meta.security.panic_actions = localizeExact(cloned.meta.security.panic_actions);
  cloned.overview.hero = cloned.overview.hero.map((item) => ({ ...item, label: localizeExact(item.label) }));
  cloned.overview.alerts = cloned.overview.alerts.map((alert) => ({
    ...alert,
    severity: localizeExact(alert.severity),
    title: localizeDynamicText(alert.title),
    body: localizeDynamicText(alert.body),
    action: localizeDynamicText(alert.action),
  }));
  cloned.risk.alerts = cloned.risk.alerts.map((alert) => ({
    ...alert,
    severity: localizeExact(alert.severity),
    title: localizeDynamicText(alert.title),
    body: localizeDynamicText(alert.body),
    action: localizeDynamicText(alert.action),
  }));
  cloned.strategies.flow = cloned.strategies.flow.map((step) => ({
    ...step,
    step: localizeExact(step.step),
    status: localizeExact(step.status),
    detail: localizeDynamicText(step.detail),
    metric: localizeDynamicText(step.metric),
  }));
  cloned.replay.timeline = cloned.replay.timeline.map((item) => ({
    ...item,
    kind: state.locale === "zh" ? localizeExact(item.kind) : String(item.kind || "").toUpperCase(),
    subtitle: item.subtitle ? localizeDynamicText(item.subtitle) : "",
    detail: localizeDynamicText(item.detail),
  }));
  cloned.settings.security.basic_auth = localizeExact(cloned.settings.security.basic_auth);
  cloned.settings.security.action_token = localizeExact(cloned.settings.security.action_token);
  cloned.settings.security.read_only = localizeExact(cloned.settings.security.read_only);
  cloned.settings.security.panic_actions = localizeExact(cloned.settings.security.panic_actions);
  return cloned;
}

function applyMarkupTranslations(html) {
  if (state.locale !== "zh") return html;
  let out = html;
  const entries = Object.entries(EXACT_TEXT_ZH).sort((a, b) => b[0].length - a[0].length);
  for (const [en, zh] of entries) {
    out = out.split(en).join(zh);
  }
  return out;
}

function fmtNumber(value, digits = 2) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return "—";
  return num.toLocaleString(state.locale === "zh" ? "zh-CN" : "en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtSigned(value, digits = 2, suffix = "") {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return "—";
  return `${num >= 0 ? "+" : ""}${fmtNumber(num, digits)}${suffix}`;
}

function fmtTs(ts) {
  if (!ts) return "—";
  const epochMs = Number(ts) >= 10 ** 12 ? Number(ts) : Number(ts) * 1000;
  return new Date(epochMs).toLocaleString(state.locale === "zh" ? "zh-CN" : "en-US");
}

function fmtMs(ms) {
  if (!ms) return "—";
  return new Date(ms).toLocaleString(state.locale === "zh" ? "zh-CN" : "en-US");
}

function shortHash(value) {
  if (!value) return "—";
  return value.length > 12 ? `${value.slice(0, 8)}...${value.slice(-4)}` : value;
}

function clsSigned(value) {
  return Number(value || 0) >= 0 ? "positive" : "negative";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showToast(message, isError = false) {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.innerHTML = `<div class="${isError ? "negative" : "positive"}">${escapeHtml(message)}</div>`;
  document.body.appendChild(toast);
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.remove(), 3200);
}

function routeMeta() {
  return ROUTES.find((route) => route.id === state.route) || ROUTES[0];
}

function setRoute(routeId) {
  state.route = routeId;
  location.hash = routeId;
  render();
}

function pickPool(poolId) {
  state.selectedPool = poolId;
  render();
}

function currentPool(data) {
  const markets = data?.markets;
  if (!markets) return null;
  if (state.selectedPool) {
    const found = markets.pools.find((p) => p.pool === state.selectedPool);
    if (found) return found;
    const addr = state.selectedPool;
    return {
      pool: addr,
      label: `pool ${shortHash(addr)}`,
      pool_type: "",
      fee_bps: 0,
      has_weth_leg: false,
      token_symbol: "?",
      token_address: "",
      token_decimals: 18,
      pool_meta: {},
      last_price_usd: 0,
      recent_volume_usd: 0,
      sample_count: 0,
    };
  }
  return markets.active_pool;
}

function currentMarketSnapshot(data) {
  const pool = currentPool(data);
  if (!pool) return null;
  const snap = data?.markets?.pool_snapshots?.[pool.pool];
  if (snap) return snap;
  return { meta: pool, candles: [], volumes: [], markers: [] };
}

function destroyCharts() {
  Object.values(state.charts).forEach((chart) => {
    if (chart && typeof chart.remove === "function") chart.remove();
  });
  state.charts = {};
}

function heroCards(data) {
  return data.overview.hero.map((item) => `
    <article class="hero-card">
      <div class="metric-label">${escapeHtml(item.label)}</div>
      <div class="metric-value ${escapeHtml(item.tone || clsSigned(item.value))}">
        ${item.signed
          ? fmtSigned(item.value, item.suffix === "%" ? 1 : 2, item.suffix ? ` ${item.suffix}` : "")
          : `${fmtNumber(item.value, item.suffix === "%" ? 1 : 2)}${item.suffix ? ` ${item.suffix}` : ""}`
        }
      </div>
      <div class="metric-foot">Live runtime snapshot</div>
    </article>
  `).join("");
}

function alertList(alerts, capabilities = state.data?.settings?.capabilities || {}) {
  const canWrite = capabilities.action_token_required && !capabilities.read_only;
  if (!alerts.length) {
    return `<div class="empty">No active alert. Runtime surface is clean right now.</div>`;
  }
  return alerts.map((alert) => `
    <article class="alert-card ${escapeHtml(alert.severity)}">
      <div class="alert-head">
        <div>
          <h4 class="alert-title">${escapeHtml(alert.title)}</h4>
          <div class="alert-copy">${escapeHtml(alert.body)}</div>
        </div>
        <span class="severity-pill ${escapeHtml(alert.severity)}">${escapeHtml(alert.severity)}</span>
      </div>
      <div class="alert-copy">Action: ${escapeHtml(alert.action)}</div>
      ${alert.acked ? `<div class="alert-copy">Acknowledged at ${fmtTs(alert.acked.acked_at)}</div>` : ""}
      <div class="panel-actions">
        <button class="btn-ghost" data-ack-alert="${escapeHtml(alert.id)}" ${canWrite ? "" : "disabled"}>Acknowledge</button>
      </div>
    </article>
  `).join("");
}

function tableRows(items, columns) {
  if (!items.length) {
    return `<tr><td colspan="${columns.length}" class="muted">No rows yet.</td></tr>`;
  }
  return items.map((item) => `
    <tr>
      ${columns.map((column) => `<td>${column.render(item)}</td>`).join("")}
    </tr>
  `).join("");
}

function renderResearchRace(data) {
  const r = data.research;
  const zh = state.locale === "zh";
  if (!r) {
    return `<section class="research-race"><article class="panel"><p class="muted">${zh ? "Research / Forward Race：服务器未返回 research 块（请重启 dashboard 容器以加载 Sprint078B）。" : "Research / Forward Race: no research block (restart dashboard for Sprint078B)."}</p></article></section>`;
  }
  const redis = r.redis_signals || {};
  const redisPill = redis.status === "live" ? "positive" : redis.status === "stale" ? "warn" : "neutral";
  const top = r.forward_top || [];
  const rows = r.forward_leaderboard || [];
  const fam = r.family_scoreboard || [];
  const agg = r.aggregates || {};
  const risks = r.risk_flags || [];
  const expN = r.expansion_plan_count ?? r.sprint077_resource?.strategy_count ?? "—";
  const artOk = r.artifact_root_exists ? "OK" : "MISSING";

  const topChips = top.length
    ? top.map((row) => `
      <span class="research-chip mono">${escapeHtml(row.strategy_id)} · n=${row.forward_n ?? "—"} · EV ${row.ev != null ? fmtNumber(row.ev, 4) : "—"}</span>
    `).join("")
    : `<span class="muted">${zh ? "无 forward 行" : "No forward rows"}</span>`;

  const tableRowsHtml = rows.length
    ? rows.map((row) => `
      <tr>
        <td class="mono">${escapeHtml(row.strategy_id)}</td>
        <td>${escapeHtml(row.family || "—")}</td>
        <td>${escapeHtml(String(row.confidence_bucket || "—"))}</td>
        <td>${row.forward_n ?? "—"}</td>
        <td>${row.ev != null ? fmtNumber(row.ev, 4) : "—"}</td>
        <td>${row.quote_fail_rate != null ? fmtNumber(row.quote_fail_rate, 4) : "—"}</td>
        <td>${row.open_position_stuck_count ?? "—"}</td>
        <td>${row.hard_exit_forced_count != null ? escapeHtml(String(row.hard_exit_forced_count)) : "—"}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="8" class="muted">${zh ? "无数据：检查 compose volume 是否挂载 mvp/audits" : "No data: verify compose volume mounts mvp/audits"}</td></tr>`;

  const famRows = fam.length
    ? fam.map((f) => `
      <tr>
        <td>${escapeHtml(f.family)}</td>
        <td>${f.strategy_count ?? "—"}</td>
        <td>${f.total_forward_n_across_strategies ?? "—"}</td>
        <td>${f.weighted_forward_ev != null ? fmtNumber(f.weighted_forward_ev, 4) : "—"}</td>
        <td>${escapeHtml(f.family_evidence_tag || "—")}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="5" class="muted">—</td></tr>`;

  const riskLine = risks.length
    ? risks.slice(0, 12).map((x) => `<span class="risk-chip">${escapeHtml(x)}</span>`).join("")
    : `<span class="muted">${zh ? "无 boolean 风险标记" : "No flag markers"}</span>`;

  const title = zh ? "Research / Forward Race（只读 artifact）" : "Research / Forward Race (read-only artifacts)";
  const histNote = zh
    ? "Historical snapshot（工厂标签回测）≠ forward proof — 不得与 forward-only 混算 alpha。"
    : r.historical_note || "Historical factory snapshots are NOT forward proof.";

  return `
    <section class="research-race">
      <article class="panel research-race-main">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${title}</h3>
            <div class="panel-copy">${zh ? "Sprint076 排行榜 + Sprint077 扩容；Redis 信号新鲜度独立标注。" : "Sprint076 leaderboard + Sprint077 expansion; Redis signal freshness shown separately."}</div>
          </div>
          <span class="small-pill ${redisPill}">${escapeHtml(redis.status || "?").toUpperCase()}</span>
        </div>
        <div class="research-strip">
          <div class="research-kv"><span class="muted">${zh ? "Redis 信号" : "Redis signals"}</span><strong>${escapeHtml((redis.status || "unknown").toUpperCase())}</strong><span class="muted small">${escapeHtml(redis.detail || "")}</span></div>
          <div class="research-kv"><span class="muted">LIVE_TRADING_MODE</span><strong class="${r.live_trading_mode === "ON" ? "text-warn" : ""}">${escapeHtml(r.live_trading_mode)}</strong></div>
          <div class="research-kv"><span class="muted">${zh ? "策略数" : "Strategy count"}</span><strong>${r.strategy_count ?? "—"}</strong></div>
          <div class="research-kv"><span class="muted">${zh ? "Sprint076 更新" : "Sprint076 updated"}</span><strong class="mono">${escapeHtml(r.sprint076_generated_at_utc || "—")}</strong></div>
          <div class="research-kv"><span class="muted">${zh ? "Sprint077 扩容" : "Sprint077 expansion"}</span><strong>${escapeHtml(String(expN))}</strong></div>
          <div class="research-kv"><span class="muted">${zh ? "Artifact 根" : "Artifact root"}</span><strong>${escapeHtml(artOk)}</strong><span class="mono muted small">${escapeHtml(r.artifact_root)}</span></div>
        </div>
        <div class="research-strip muted">
          <span>${zh ? "汇总 stuck（门控计数）" : "Stuck (gate counts)"}: <strong>${agg.total_stuck_gate_counts ?? "—"}</strong></span>
          <span>max quote_fail: <strong>${agg.max_quote_fail_rate_forward076 ?? "—"}</strong></span>
          <span>Σ hard_exit_forced (077): <strong>${agg.sum_hard_exit_forced_sprint077 ?? "—"}</strong></span>
        </div>
        <div class="research-risk-row">${riskLine}</div>
        <p class="research-hist-note">${escapeHtml(histNote)}</p>
        <div class="panel-subtitle">${zh ? "Forward-only · Top strategies" : "Forward-only · Top strategies"}</div>
        <div class="research-chip-row">${topChips}</div>
        <div class="panel-subtitle">${zh ? "Forward-only 全表" : "Forward-only full leaderboard"} (${rows.length})</div>
        <div class="table-scroll research-table-scroll">
          <table class="research-table">
            <thead>
              <tr>
                <th>strategy_id</th>
                <th>family</th>
                <th>confidence</th>
                <th>fwd_n</th>
                <th>EV</th>
                <th>quote_fail</th>
                <th>stuck</th>
                <th>hard_exit_Δ</th>
              </tr>
            </thead>
            <tbody>${tableRowsHtml}</tbody>
          </table>
        </div>
        <div class="panel-subtitle">${zh ? "Family ranking（Sprint076）" : "Family ranking (Sprint076)"}</div>
        <div class="table-scroll research-table-scroll">
          <table class="research-table">
            <thead>
              <tr>
                <th>family</th>
                <th>strategies</th>
                <th>Σ fwd_n</th>
                <th>wgt EV</th>
                <th>evidence</th>
              </tr>
            </thead>
            <tbody>${famRows}</tbody>
          </table>
        </div>
        ${(r.errors || []).length ? `<div class="muted small">${zh ? "读取警告：" : "Load warnings:"} ${escapeHtml(r.errors.join("; "))}</div>` : ""}
      </article>
    </section>
  `;
}

function renderPipelineCard(data) {
  const p = data.pipeline || {};
  const ingest = p.ingest || {};
  const archive = p.archive || {};
  const pool = (p.ingestor_pool || {}).fields || {};
  const overall = p.overall || "unknown";
  const tone = overall === "healthy" ? "positive" : overall === "warning" ? "watch" : "negative";
  const ingestAge = ingest.age_sec == null ? "—" : `${ingest.age_sec}s`;
  const archiveAge = archive.age_sec == null ? "—" : `${archive.age_sec}s`;
  const wsProv = pool["ws:active_provider"] || "—";
  const httpProv = pool["http:active_provider"] || "—";
  return `
    <article class="panel grid-span-12 pipeline-card">
      <div class="panel-header">
        <div>
          <h3 class="panel-title">${state.locale === "zh" ? "管道新鲜度（权威）" : "Pipeline freshness (source of truth)"}</h3>
          <div class="panel-copy">${state.locale === "zh" ? "market_ticks 断流与 ClickHouse 归档滞后必须在第一屏报警。" : "market_ticks staleness and ClickHouse archive lag must alarm on the first screen."}</div>
        </div>
        <span class="small-pill ${tone}">${overall.toUpperCase()}</span>
      </div>
      <div class="hero-grid" style="grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px;">
        <div class="metric-card"><div class="metric-label">market_ticks age</div><div class="metric-value">${escapeHtml(String(ingestAge))}</div></div>
        <div class="metric-card"><div class="metric-label">CH max(block_time) age</div><div class="metric-value">${escapeHtml(String(archiveAge))}</div></div>
        <div class="metric-card"><div class="metric-label">WS provider</div><div class="metric-value mono">${escapeHtml(String(wsProv))}</div></div>
        <div class="metric-card"><div class="metric-label">HTTP provider</div><div class="metric-value mono">${escapeHtml(String(httpProv))}</div></div>
      </div>
    </article>
  `;
}

function renderDashboard(data) {
  return `
    ${renderPipelineCard(data)}
    ${renderResearchRace(data)}
    <section class="hero-grid">${heroCards(data)}</section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-8">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Equity and realized curve</h3>
            <div class="panel-copy">${state.locale === "zh" ? "以日初权益为基线，叠加 executor 已实现盈亏后的权益曲线，用来看当日趋势和回撤。" : "Equity curve anchored at the day-start balance and updated by realized executor PnL for intraday trend and drawdown."}</div>
          </div>
          <span class="small-pill ${data.status === "紧急停止" || data.status === "panic" ? "negative" : "positive"}">${data.status === "紧急停止" || data.status === "panic" ? tr("status_panic") : tr("status_live")}</span>
        </div>
        <div id="equity-chart" class="chart-box"></div>
      </article>
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Active alerts</h3>
            <div class="panel-copy">Operator-facing warnings and circuit-breaker state.</div>
          </div>
        </div>
        <div class="alert-list">${alertList(data.overview.alerts, data.settings.capabilities)}</div>
      </article>
      <article class="table-card grid-span-6">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Recent signals</h3>
            <div class="panel-copy">Fresh entries from the signals stream.</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Pool</th>
                <th>Score</th>
                <th>Review</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.overview.recent_signals, [
              { render: (row) => `<span class="mono">${fmtTs(row.timestamp)}</span>` },
              { render: (row) => `<span class="route-chip">${escapeHtml(row.signal_source || "unknown")}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => fmtNumber(row.final_score || row.confidence || 0, 2) },
              { render: (row) => row.review ? `<span class="small-pill">${escapeHtml(row.review.decision)}</span>` : `<span class="muted">pending</span>` },
            ])}</tbody>
          </table>
        </div>
      </article>
      <article class="table-card grid-span-6">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Recent trades</h3>
            <div class="panel-copy">Executor logs with PnL and exit reason.</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Pool</th>
                <th>PnL</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.overview.recent_trades, [
              { render: (row) => `<span class="mono">${fmtTs(row.ts)}</span>` },
              { render: (row) => `<span class="route-chip">${escapeHtml(state.locale === "zh" ? ((row.action || "").toUpperCase() === "BUY" ? "买入" : "卖出") : (row.action || "").toUpperCase())}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => `<span class="${clsSigned(row.pnl_usd)}">${fmtSigned(row.pnl_usd, 4, " USD")}</span>` },
              { render: (row) => escapeHtml(row.reason || row.signal_source || "—") },
            ])}</tbody>
          </table>
        </div>
      </article>
    </section>
  `;
}

function renderMarkets(data) {
  const pool = currentPool(data);
  const snapshot = currentMarketSnapshot(data);
  const hasCandles = Boolean(snapshot?.candles?.length);
  const poolChips = [...(data.markets?.pools || [])];
  if (pool && !poolChips.some((item) => item.pool === pool.pool)) {
    poolChips.unshift(pool);
  }
  return `
    <section class="panel">
      <div class="panel-header">
        <div>
          <h3 class="panel-title">Markets watch</h3>
          <div class="panel-copy">${state.locale === "zh" ? "最近的 swap 事件会尽量整理成价格带。即使短时间没有可绘制 K 线，也会保留活跃池与上下文。" : "Recent swap events are compacted into a price tape when possible. Pools remain visible even when the latest window has no drawable candles."}</div>
        </div>
        <span class="route-chip">${escapeHtml(pool?.label || "No active pool")}</span>
      </div>
      <div class="pool-selector">
        ${poolChips.map((item) => `
          <button class="pool-chip ${item.pool === pool?.pool ? "active" : ""}" data-pool="${escapeHtml(item.pool)}">
            <div class="signal-head">
              <strong>${escapeHtml(item.label)}</strong>
              <span class="small-pill">${escapeHtml(item.pool_type || "pool")}</span>
            </div>
            <div class="signal-copy">${escapeHtml(item.token_symbol || "UNKNOWN")} · ${item.fee_bps || 0}bps</div>
            <div class="signal-copy pool-chip-meta">
              ${state.locale === "zh"
                ? `最近价 ${fmtNumber(item.last_price_usd || 0, 4)} USD · 体量 ${fmtNumber(item.recent_volume_usd || 0, 0)}`
                : `Last ${fmtNumber(item.last_price_usd || 0, 4)} USD · Vol ${fmtNumber(item.recent_volume_usd || 0, 0)}`}
            </div>
          </button>
        `).join("")}
      </div>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-8">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Price tape and signal overlay</h3>
            <div class="panel-copy">${state.locale === "zh" ? "基于原始 swap 事件生成。买卖标记来自 signals 流和 executor 台账。" : "Derived from raw swap events. Buy/sell markers come from the signals stream and executor ledger."}</div>
          </div>
        </div>
        <div id="market-chart" class="chart-box"></div>
        ${hasCandles ? "" : `<div class="footer-note">${state.locale === "zh" ? "当前选中池在最近采样窗口里没有足够的可绘制 swap K 线，但池、信号和交易上下文仍会显示。" : "The selected pool does not have enough recent swap samples to draw candles yet, but pool, signal, and trade context still remain visible."}</div>`}
      </article>
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Pool context</h3>
            <div class="panel-copy">Selected pool metadata and live runtime linkage.</div>
          </div>
        </div>
        <div class="stack">
          <div class="metric-row"><span class="muted">Pool</span><span class="mono">${escapeHtml(shortHash(pool?.pool || ""))}</span></div>
          <div class="metric-row"><span class="muted">Type</span><span>${escapeHtml(pool?.pool_type || "—")}</span></div>
          <div class="metric-row"><span class="muted">Fee</span><span>${escapeHtml(String(pool?.fee_bps || "—"))} bps</span></div>
          <div class="metric-row"><span class="muted">Token</span><span>${escapeHtml(pool?.token_symbol || "—")}</span></div>
          <div class="metric-row"><span class="muted">Price</span><span>${fmtNumber(pool?.last_price_usd || 0, 4)} USD</span></div>
          <div class="metric-row"><span class="muted">Candles</span><span>${snapshot?.candles?.length || 0}</span></div>
          <div class="metric-row"><span class="muted">Markers</span><span>${snapshot?.markers?.length || 0}</span></div>
          <div class="metric-row"><span class="muted">${state.locale === "zh" ? "事件样本" : "Samples"}</span><span>${pool?.sample_count || 0}</span></div>
        </div>
      </article>
      <article class="table-card grid-span-12">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Recent pool-linked signals</h3>
            <div class="panel-copy">Signals sorted by most recent runtime publication.</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Pool</th>
                <th>Source</th>
                <th>Position</th>
                <th>Score</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.markets.recent_signals.filter((row) => !pool || row.pool === pool.pool), [
              { render: (row) => `<span class="mono">${fmtTs(row.timestamp)}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => `<span class="route-chip">${escapeHtml(row.signal_source || "unknown")}</span>` },
              { render: (row) => `${fmtNumber(row.position_size_usd || 0, 2)} USD` },
              { render: (row) => fmtNumber(row.final_score || row.confidence || 0, 2) },
            ])}</tbody>
          </table>
        </div>
      </article>
    </section>
  `;
}

function renderExecution(data) {
  return `
    <section class="panel-grid">
      <article class="table-card grid-span-7">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Open positions</h3>
            <div class="panel-copy">Live inventory as tracked by the executor position store.</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Opened</th>
                <th>Pool</th>
                <th>Type</th>
                <th>Entry</th>
                <th>Age</th>
                <th>Tx</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.open_positions, [
              { render: (row) => `<span class="mono">${fmtMs(row.opened_at_ms)}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => `<span class="route-chip">${escapeHtml(row.pool_type || "—")}</span>` },
              { render: (row) => `${fmtNumber(row.entry_usd, 4)} USD` },
              { render: (row) => `${fmtNumber(row.age_min, 1)} min` },
              { render: (row) => row.buy_tx ? `<a class="mono" href="https://basescan.org/tx/0x${escapeHtml(row.buy_tx)}" target="_blank" rel="noreferrer">${shortHash(row.buy_tx)}</a>` : "—" },
            ])}</tbody>
          </table>
        </div>
      </article>
      <article class="panel grid-span-5">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Attribution</h3>
            <div class="panel-copy">PnL grouping from exec:trades, aligned with trade attribution tooling.</div>
          </div>
        </div>
        <div class="mini-list">
          ${data.execution.by_source.map((row) => `
            <div class="list-row">
              <div>
                <div>${escapeHtml(row.label)}</div>
                <div class="muted">${state.locale === "zh" ? `${row.count} 笔已平仓 · 胜率 ${row.win_rate}%` : `${row.count} closed trades · ${row.win_rate}% win rate`}</div>
              </div>
              <div class="${clsSigned(row.total_pnl_usd)}">${fmtSigned(row.total_pnl_usd, 4, " USD")}</div>
            </div>
          `).join("") || `<div class="empty">No source attribution yet.</div>`}
        </div>
      </article>
      <article class="table-card grid-span-12">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Trade ledger</h3>
            <div class="panel-copy">Buy/sell events with source, cap, and realized PnL.</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Pool</th>
                <th>Source</th>
                <th>PnL</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.trades, [
              { render: (row) => `<span class="mono">${fmtTs(row.ts)}</span>` },
              { render: (row) => `<span class="route-chip">${escapeHtml(state.locale === "zh" ? ((row.action || "").toUpperCase() === "BUY" ? "买入" : "卖出") : (row.action || "").toUpperCase())}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => escapeHtml(row.signal_source || "—") },
              { render: (row) => row.action === "sell" ? `<span class="${clsSigned(row.pnl_usd)}">${fmtSigned(row.pnl_usd, 4, " USD")}</span>` : `${fmtNumber(row.amount_usd || 0, 2)} USD` },
              { render: (row) => escapeHtml(row.reason || "—") },
            ])}</tbody>
          </table>
        </div>
      </article>
      <article class="panel grid-span-12">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Daily evaluation</h3>
            <div class="panel-copy">The same executor-generated summary surface used for gate checks.</div>
          </div>
        </div>
        <div class="three-col">
          ${data.execution.daily_evals.map((row) => `
            <article class="signal-card">
              <div class="signal-head">
                <strong>${escapeHtml(row.day)}</strong>
                <span class="small-pill ${row.net_pnl_usd >= 0 ? "positive" : "negative"}">${row.dry_run ? localizeExact("dry-run") : localizeExact("live")}</span>
              </div>
              <div class="signal-copy">${state.locale === "zh" ? `净值 ${fmtSigned(row.net_pnl_usd, 4, " USD")} · ${row.trade_count} 笔交易 · 胜率 ${fmtNumber(row.win_rate, 1)}%` : `Net ${fmtSigned(row.net_pnl_usd, 4, " USD")} · ${row.trade_count} trades · ${fmtNumber(row.win_rate, 1)}% win rate`}</div>
            </article>
          `).join("") || `<div class="empty">No daily summaries have been emitted yet.</div>`}
        </div>
      </article>
    </section>
  `;
}

function renderStrategies(data) {
  const pending = data.strategies.pending_reviews || [];
  const canWrite = data.settings.capabilities.action_token_required && !data.settings.capabilities.read_only;
  const reviewTitle = data.settings.capabilities.read_only
    ? (state.locale === "zh" ? "信号复核队列（只读快照）" : "Signal review queue (read-only snapshot)")
    : (state.locale === "zh" ? "待处理人工复核" : "Pending operator reviews");
  const reviewCopy = data.settings.capabilities.read_only
    ? (state.locale === "zh"
      ? "当前站点是只读模式，这里只展示哪些信号本来需要人工复核；真正的 Approve / Observe / Reject 需要在可写环境执行。"
      : "This site is in read-only mode. The table only shows which signals would require human review; actual Approve / Observe / Reject actions must happen in a writable environment.")
    : "V1 controlled interaction layer. Reviews are stored separately from the runtime stream.";
  return `
    <section class="panel">
      <div class="panel-header">
        <div>
          <h3 class="panel-title">Strategy workflow</h3>
          <div class="panel-copy">Readable operator map of the current signal-to-execution lifecycle.</div>
        </div>
      </div>
      <div class="workflow">
        ${data.strategies.flow.map((step) => `
          <article class="workflow-card">
            <div class="workflow-head">
              <strong class="workflow-title">${escapeHtml(step.step)}</strong>
              <span class="severity-pill ${step.status === "healthy" ? "healthy" : step.status === "critical" ? "critical" : "warning"}">${escapeHtml(step.status)}</span>
            </div>
            <div class="workflow-copy">${escapeHtml(step.detail)}</div>
            <div class="workflow-copy">${escapeHtml(step.metric)}</div>
          </article>
        `).join("")}
      </div>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Shadow cohort</h3>
            <div class="panel-copy">Closed shadow performance grouped by signal source.</div>
          </div>
        </div>
        <div class="mini-list">
          ${data.strategies.shadow_summary.by_source.map((row) => `
            <div class="list-row">
              <div>
                <div>${escapeHtml(row.source)}</div>
                <div class="muted">${state.locale === "zh" ? `${row.count} 条已关闭 · 胜率 ${fmtNumber(row.win_rate, 1)}%` : `${row.count} closes · ${fmtNumber(row.win_rate, 1)}% wins`}</div>
              </div>
              <div class="${clsSigned(row.mean_return_pct)}">${fmtSigned(row.mean_return_pct, 2, "%")}</div>
            </div>
          `).join("") || `<div class="empty">Shadow records need to accumulate before cohort insights appear.</div>`}
        </div>
      </article>
      <article class="table-card grid-span-8">
        <div class="table-header">
          <div>
            <h3 class="panel-title">${reviewTitle}</h3>
            <div class="panel-copy">${reviewCopy}</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Pool</th>
                <th>Source</th>
                <th>Score</th>
                <th>Decision</th>
              </tr>
            </thead>
            <tbody>${tableRows(pending, [
              { render: (row) => `<span class="mono">${fmtTs(row.timestamp)}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => escapeHtml(row.signal_source || "—") },
              { render: (row) => fmtNumber(row.final_score || row.confidence || 0, 2) },
              { render: (row) => canWrite
                ? `
                  <div class="chip-row">
                    <button class="btn-ghost" data-review="${escapeHtml(row.id)}:approved">Approve</button>
                    <button class="btn-ghost" data-review="${escapeHtml(row.id)}:observed">Observe</button>
                    <button class="btn-danger" data-review="${escapeHtml(row.id)}:rejected">Reject</button>
                  </div>
                `
                : `<span class="small-pill">${state.locale === "zh" ? "只读，仅展示" : "Read-only"}</span>` },
            ])}</tbody>
          </table>
        </div>
      </article>
    </section>
  `;
}

function renderReplay(data) {
  const sells = (data.trades || []).filter((row) => (row.action || "").toLowerCase() === "sell");
  const closedShadow = (data.replay.shadow_records || []).filter((row) => row.status === "closed");
  const rejectedShadow = (data.replay.shadow_records || []).filter((row) => String(row.signal_source || "").includes("rejected"));
  const replaySignals = (data.overview?.recent_signals || []).length;
  const sum = data.replay?.summary || {};
  const mu = sum.shadow_closed_mean_pct;
  const kpiShadowMean = mu != null && Number.isFinite(Number(mu)) ? fmtSigned(mu, 2, "%") : "—";
  const kpiLivePnl = sum.live_net_pnl_usd != null && Number.isFinite(Number(sum.live_net_pnl_usd))
    ? fmtSigned(sum.live_net_pnl_usd, 2, " USD")
    : "—";
  const replayKpis = state.locale === "zh"
    ? `<div class="replay-kpis muted">
        <span><strong>Shadow 均值</strong> ${kpiShadowMean} · 已关 ${fmtNumber(sum.shadow_closed_n ?? 0, 0)} · 在途 ${fmtNumber(sum.shadow_open_n ?? 0, 0)}</span>
        <span><strong>实盘已实现</strong> ${kpiLivePnl} · 平仓 ${fmtNumber(sum.live_closed_n ?? 0, 0)}</span>
        <span><strong>试运行平仓</strong> ${fmtNumber(sum.dry_run_closed_n ?? 0, 0)}</span>
        <span><strong>实盘胜率</strong> ${fmtNumber(sum.live_win_rate_pct ?? 0, 1)}%</span>
      </div>`
    : `<div class="replay-kpis muted">
        <span><strong>Shadow mean</strong> ${kpiShadowMean} · closed ${fmtNumber(sum.shadow_closed_n ?? 0, 0)} · open ${fmtNumber(sum.shadow_open_n ?? 0, 0)}</span>
        <span><strong>Live net</strong> ${kpiLivePnl} · exits ${fmtNumber(sum.live_closed_n ?? 0, 0)}</span>
        <span><strong>Dry-run exits</strong> ${fmtNumber(sum.dry_run_closed_n ?? 0, 0)}</span>
        <span><strong>Live win rate</strong> ${fmtNumber(sum.live_win_rate_pct ?? 0, 1)}%</span>
      </div>`;
  return `
    <section class="metric-grid">
      <article class="hero-card">
        <div class="metric-label">${state.locale === "zh" ? "最近信号" : "Recent signals"}</div>
        <div class="metric-value">${fmtNumber(replaySignals, 0)}</div>
        <div class="metric-foot">${state.locale === "zh" ? "来自 signals 流的最新候选" : "Latest candidates published to the signals stream"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">${state.locale === "zh" ? "已关闭 shadow" : "Closed shadow"}</div>
        <div class="metric-value">${fmtNumber(closedShadow.length, 0)}</div>
        <div class="metric-foot">${state.locale === "zh" ? "可以做纸面复盘的样本数" : "Samples available for paper replay review"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">${state.locale === "zh" ? "拒绝样本" : "Rejected shadow"}</div>
        <div class="metric-value">${fmtNumber(rejectedShadow.length, 0)}</div>
        <div class="metric-foot">${state.locale === "zh" ? "被规则或人工挡下来的候选" : "Candidates rejected by rules or review"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">${state.locale === "zh" ? "实际卖出" : "Closed live trades"}</div>
        <div class="metric-value">${fmtNumber(sells.length, 0)}</div>
        <div class="metric-foot">${state.locale === "zh" ? "executor 已落地的真实退出记录" : "Actual executor exits already written to the ledger"}</div>
      </article>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-5">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Unified timeline</h3>
            <div class="panel-copy">${state.locale === "zh" ? "把 signal、shadow 和真实交易放在同一时间轴里，先看先后顺序，再去看细表。" : "Signals, shadow events, and live trades merged into one order-of-events timeline."}</div>
            <div class="panel-copy muted">${state.locale === "zh"
              ? "有条目的池可点击卡片跳转「行情」：有 swap 样本时能看到推导 K 线，否则图为空。"
              : "Click a row to open that pool on Markets — candles appear when swap-derived tape exists."}</div>
          </div>
        </div>
        ${replayKpis}
        <div class="timeline">
          ${data.replay.timeline.map((item) => {
            const jump = item.pool ? " timeline-card--jump" : "";
            const poolAttr = item.pool ? ` data-timeline-pool="${escapeHtml(item.pool)}"` : "";
            const sub = item.subtitle ? `<div class="timeline-subtitle">${escapeHtml(item.subtitle)}</div>` : "";
            return `
            <article class="timeline-card${jump}"${poolAttr}>
              <div class="timeline-head">
                <strong class="timeline-title">${escapeHtml(item.kind)}</strong>
                <span class="muted mono">${fmtTs(item.ts)}</span>
              </div>
              <div class="timeline-primary">${escapeHtml(item.label)}</div>
              ${sub}
              <div class="timeline-copy">${escapeHtml(item.detail)}</div>
            </article>
          `;
          }).join("")}
        </div>
      </article>
      <article class="table-card grid-span-7">
        <div class="table-header">
          <div>
            <h3 class="panel-title">Closed shadow records</h3>
            <div class="panel-copy">${state.locale === "zh" ? "先看纸面回报、持有时长和来源，判断信号质量，再决定要不要推进真实执行。" : "Review paper returns, hold times, and sources before deciding whether the signal quality is ready for live execution."}</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Signal Time</th>
                <th>Pool</th>
                <th>Source</th>
                <th>${state.locale === "zh" ? "拒绝原因" : "Reject"}</th>
                <th>${state.locale === "zh" ? "状态" : "Status"}</th>
                <th>Return</th>
                <th>Hold</th>
              </tr>
            </thead>
            <tbody>${tableRows(data.replay.shadow_records.filter((row) => row.status === "closed"), [
              { render: (row) => `<span class="mono">${fmtMs(row.signal_ts_ms)}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => escapeHtml(row.signal_source || "—") },
              { render: (row) => escapeHtml(row.reject_reason || "—") },
              { render: (row) => `<span class="small-pill">${escapeHtml(row.status || "—")}</span>` },
              { render: (row) => `<span class="${clsSigned(row.net_return_pct)}">${fmtSigned(row.net_return_pct, 2, "%")}</span>` },
              { render: (row) => `${fmtNumber(row.hold_minutes || 0, 1)} min` },
            ])}</tbody>
          </table>
        </div>
      </article>
      <article class="table-card grid-span-12">
        <div class="table-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "真实卖出结果" : "Live exit outcomes"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "这里只看已经卖出的真实交易，能直接回答‘为什么退场、赚了还是亏了’。" : "This table only shows completed sell trades so you can directly answer why the position exited and whether it made money."}</div>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Pool</th>
                <th>Source</th>
                <th>${state.locale === "zh" ? "模式" : "Mode"}</th>
                <th>PnL</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>${tableRows(sells, [
              { render: (row) => `<span class="mono">${fmtTs(row.ts)}</span>` },
              { render: (row) => escapeHtml(row.label || row.pool) },
              { render: (row) => escapeHtml(row.signal_source || "—") },
              { render: (row) => (row.dry_run
                ? `<span class="small-pill">${state.locale === "zh" ? localizeExact("dry-run") : "dry-run"}</span>`
                : `<span class="small-pill">${state.locale === "zh" ? "实盘" : "live"}</span>`) },
              { render: (row) => `<span class="${clsSigned(row.pnl_usd)}">${fmtSigned(row.pnl_usd, 4, " USD")}</span>` },
              { render: (row) => escapeHtml(row.reason || "—") },
            ])}</tbody>
          </table>
        </div>
      </article>
    </section>
  `;
}

// ─────────────────────────────────────────────────────────────────────────────
// Alpha Factory Page
// ─────────────────────────────────────────────────────────────────────────────
function promotionBadge(band) {
  if (!band) return "";
  const cls = band.includes("PROMOTE") ? "alpha-badge alpha-badge--promote"
    : band.includes("LIVE") ? "alpha-badge alpha-badge--live"
    : band.includes("KEEP") ? "alpha-badge alpha-badge--keep"
    : band.includes("EXPLORE") ? "alpha-badge alpha-badge--explore"
    : "alpha-badge";
  return `<span class="${cls}">${escapeHtml(band.replace(/_/g, " "))}</span>`;
}

function gateLight(pass) {
  return pass ? `<span class="gate-dot gate-dot--pass">●</span>` : `<span class="gate-dot gate-dot--fail">●</span>`;
}

function renderAlpha(data) {
  const a = data.alpha || {};
  const agg = a.aggregate || {};
  const rows = a.strategy_rows || [];
  const pools = a.pool_rows || [];
  const funnel = data.shadow_funnel || {};
  const gates = a.gates_l073 || {};
  const bandHist = a.band_histogram || {};
  const topDry = a.top_dryrun_candidates || [];
  const top5hist = a.top5_historical || [];

  const zh = state.locale === "zh";

  // KPI cards
  const kpis = [
    { label: zh ? "Forward 成交数" : "Forward trades", value: fmtNumber(agg.total_fwd_trades || 0, 0), foot: zh ? "所有策略纸面累计" : "Paper trades across all strategies" },
    { label: zh ? "平均权益" : "Avg equity", value: `${fmtNumber(agg.avg_equity || 100, 2)}`, suffix: "", foot: zh ? "起始 100，正值为盈" : "Started at 100; above = profitable" },
    { label: zh ? "盈利策略数" : "Profitable strats", value: `${agg.strategies_positive || 0} / ${agg.strategies_total || 0}`, foot: zh ? "equity > 100 的策略" : "Strategies with equity > 100" },
    { label: zh ? "候选池数" : "Candidate pools", value: fmtNumber(agg.unique_pools || 0, 0), foot: zh ? "多策略共用池" : "Pools seen in forward harness" },
    { label: zh ? "纸面总 PnL" : "Total paper PnL", value: fmtSigned(agg.total_paper_pnl || 0, 2, ""), foot: zh ? "所有策略所有池累计" : "Sum across all strategies & pools" },
    { label: zh ? "Shadow 漏斗" : "Shadow funnel", value: fmtNumber(funnel.total || 0, 0), foot: zh ? `接受 ${funnel.accepted || 0} · 拒绝 ${funnel.rejected || 0}` : `accepted ${funnel.accepted || 0} · rejected ${funnel.rejected || 0}` },
  ];

  // Gate row
  const l0 = gates.L0_passed;
  const l1n = gates.L1_forward_trades || 0;
  const l2 = gates.L2_passed;
  const gateHtml = `
    <div class="alpha-gates">
      <div class="alpha-gate-item">${gateLight(l0)} <span>L0 Gate</span> <span class="muted">${l0 ? (zh ? "通过" : "pass") : (zh ? "未通过" : "fail")}</span></div>
      <div class="alpha-gate-item">${gateLight(l1n >= 20)} <span>L1 Forward n=${l1n}</span> <span class="muted">${l1n >= 20 ? (zh ? "≥20，可晋级" : "≥20, promotable") : (zh ? `<20，仍采样` : `<20, keep sampling`)}</span></div>
      <div class="alpha-gate-item">${gateLight(l2)} <span>L2 Gate</span> <span class="muted">${l2 ? (zh ? "通过" : "pass") : (zh ? "未通过" : "fail")}</span></div>
      <div class="alpha-gate-item alpha-gate-note muted">${zh ? "数据来源：sprint074 工厂 L073 摘要。LIVE_ALLOWED 仍为 false 直到 L2 全通过。" : "Source: S074 factory L073 summary. LIVE_ALLOWED stays false until L2 clears."}</div>
    </div>
  `;

  // Band histogram
  const bandOrder = ["PROMOTE_DRYRUN", "LIVE_QUEUE_CANDIDATE", "KEEP_SAMPLING", "EXPLORE"];
  const bandTotal = Object.values(bandHist).reduce((s, v) => s + (v || 0), 0);
  const bandHtml = bandOrder.map((b) => {
    const cnt = bandHist[b] || 0;
    const pct = bandTotal > 0 ? Math.round(cnt / bandTotal * 100) : 0;
    return `<div class="alpha-band-row">
      ${promotionBadge(b)}
      <div class="alpha-band-bar-wrap"><div class="alpha-band-bar" style="width:${pct}%"></div></div>
      <span class="mono">${cnt}</span>
    </div>`;
  }).join("");

  // Strategy leaderboard
  const stratHeaders = zh
    ? ["策略", "组", "退出", "Forward n", "胜率", "PF", "EV", "权益", "ROI", "开仓", "末次运行"]
    : ["Strategy", "Family", "Exit", "Fwd n", "WR", "PF", "EV", "Equity", "ROI", "Open", "Last run"];
  const stratRows = rows.map((r) => {
    const roiCls = r.roi_pct > 0 ? "positive" : r.roi_pct < 0 ? "negative" : "muted";
    const lastRun = r.last_run ? r.last_run.replace("T", " ").slice(0, 16) : "—";
    const harnessBadge = r.harness === "sprint075"
      ? `<span class="small-pill">top10</span>`
      : `<span class="small-pill muted">exp</span>`;
    return `<tr>
      <td><span class="mono">${escapeHtml(r.strategy_id)}</span> ${harnessBadge}</td>
      <td><span class="muted">${escapeHtml(r.family)}</span></td>
      <td><span class="mono">${escapeHtml(r.exit_horizon)}</span></td>
      <td>${fmtNumber(r.fwd_n, 0)}</td>
      <td><span class="${r.fwd_wr >= 0.5 ? "positive" : "negative"}">${fmtNumber(r.fwd_wr * 100, 1)}%</span></td>
      <td>${r.fwd_pf >= 99 ? "99+" : fmtNumber(r.fwd_pf, 2)}</td>
      <td>${r.fwd_ev != null ? fmtNumber(r.fwd_ev, 4) : "—"}</td>
      <td class="mono">${fmtNumber(r.equity, 4)}</td>
      <td><span class="${roiCls}">${fmtSigned(r.roi_pct, 2, "%")}</span></td>
      <td>${r.open_count > 0 ? `<span class="positive">${r.open_count}</span>` : "0"}</td>
      <td class="muted mono">${escapeHtml(lastRun)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="11" class="muted">${zh ? "暂无数据" : "No data"}</td></tr>`;

  // Pool table
  const poolTableRows = pools.map((p) => {
    const multiChip = p.strategy_count > 1 ? `<span class="small-pill positive">${zh ? "多策略" : "multi"}</span>` : `<span class="small-pill muted">${zh ? "单策略" : "single"}</span>`;
    return `<tr>
      <td>${escapeHtml(p.label)}</td>
      <td>${fmtNumber(p.n, 0)}</td>
      <td><span class="${p.wr >= 0.5 ? "positive" : "negative"}">${fmtNumber(p.wr * 100, 1)}%</span></td>
      <td><span class="${p.pnl >= 0 ? "positive" : "negative"}">${fmtSigned(p.pnl, 3, "")}</span></td>
      <td>${multiChip}</td>
      <td>${fmtNumber(p.strategy_count, 0)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">${zh ? "暂无数据" : "No data"}</td></tr>`;

  // Shadow funnel bars
  const funnelSources = (funnel.by_source || []).slice(0, 8);
  const funnelTotal = funnel.total || 1;
  const funnelHtml = funnelSources.map((row) => {
    const pct = Math.round(row.count / funnelTotal * 100);
    const isBad = String(row.source || "").includes("rejected");
    return `<div class="funnel-row">
      <span class="funnel-label muted">${escapeHtml(row.source)}</span>
      <div class="alpha-band-bar-wrap"><div class="alpha-band-bar ${isBad ? "alpha-band-bar--reject" : ""}" style="width:${pct}%"></div></div>
      <span class="mono">${row.count}</span>
    </div>`;
  }).join("") || `<div class="muted">${zh ? "暂无数据" : "No data"}</div>`;

  const rejectReasons = (funnel.by_reject_reason || []).slice(0, 5).map((r) =>
    `<span class="small-pill muted">${escapeHtml(r.reason)} × ${r.count}</span>`).join(" ");

  // Top dryrun candidates
  const dryrunRows = topDry.map((c) => `<div class="list-row">
    <div>
      <div class="mono">${escapeHtml(c.strategy_id)}</div>
      <div class="muted">${escapeHtml(c.why || "")}</div>
    </div>
    <div>${promotionBadge(c.promotion_band)}</div>
  </div>`).join("") || `<div class="empty muted">${zh ? "暂无候选" : "No candidates"}</div>`;

  // Top 5 historical (factory backtest, labeled as 历史回测)
  const hist5Rows = top5hist.map((r) => `<tr>
    <td class="mono">${escapeHtml(r.strategy_id)}</td>
    <td class="mono">${escapeHtml(r.exit_horizon)}</td>
    <td>${fmtNumber(r.n_trades, 0)}</td>
    <td><span class="${r.wr >= 0.5 ? "positive" : "negative"}">${fmtNumber((r.wr || 0) * 100, 1)}%</span></td>
    <td>${fmtNumber(r.pf || 0, 2)}</td>
    <td class="${(r.mean_ret || 0) >= 0 ? "positive" : "negative"}">${fmtSigned((r.mean_ret || 0) * 100, 2, "%")}</td>
    <td class="mono">${fmtNumber(r.equity || 100, 1)}</td>
    <td>${promotionBadge(r.promotion_band)}</td>
  </tr>`).join("") || `<tr><td colspan="8" class="muted">${zh ? "暂无数据" : "No data"}</td></tr>`;

  const updatedAt = a.rolling_generated_at ? a.rolling_generated_at.slice(0, 16) + " UTC" : "—";

  return `
    <div class="alpha-meta muted">${zh ? `最后更新：${updatedAt} · 5 分钟自动刷新` : `Last updated: ${updatedAt} · auto-refresh every 5 min`}</div>

    <section class="metric-grid" style="margin-bottom:16px">
      ${kpis.map((k) => `<article class="hero-card">
        <div class="metric-label">${k.label}</div>
        <div class="metric-value">${k.value}${k.suffix || ""}</div>
        <div class="metric-foot">${k.foot}</div>
      </article>`).join("")}
    </section>

    <section class="panel-grid">

      <article class="panel grid-span-4">
        <div class="panel-header"><div>
          <h3 class="panel-title">${zh ? "Alpha Gate 状态" : "Alpha Gate Status"}</h3>
          <div class="panel-copy muted">${zh ? "L073 全局门控；L2 通过前禁止实盘。" : "L073 global gate; LIVE blocked until L2 passes."}</div>
        </div></div>
        ${gateHtml}
        <div style="margin-top:12px">
          <div class="muted" style="font-size:.8rem;margin-bottom:6px">${zh ? "策略晋级分布（S074 历史回测）" : "Promotion band distribution (S074 historical)"}</div>
          ${bandHtml}
        </div>
      </article>

      <article class="panel grid-span-4">
        <div class="panel-header"><div>
          <h3 class="panel-title">${zh ? "Top Dryrun 候选（S074 工厂）" : "Top Dryrun Candidates (S074 factory)"}</h3>
          <div class="panel-copy muted">${zh ? "alpha_factory_dryrun_watchlist，PROMOTE_DRYRUN 档位" : "alpha_factory_dryrun_watchlist, PROMOTE_DRYRUN band"}</div>
        </div></div>
        <div class="mini-list">${dryrunRows}</div>
      </article>

      <article class="panel grid-span-4">
        <div class="panel-header"><div>
          <h3 class="panel-title">${zh ? "候选池集中度" : "Candidate Pool Breakdown"}</h3>
          <div class="panel-copy muted">${zh ? "forward harness 所有策略成交分池统计。单策略=集中风险；多策略=泛化信号。" : "Forward harness trades grouped by pool. Single = concentrated; multi = generalizing signal."}</div>
        </div></div>
        <div class="table-scroll">
          <table>
            <thead><tr>
              <th>${zh ? "池" : "Pool"}</th>
              <th>${zh ? "成交" : "Trades"}</th>
              <th>${zh ? "胜率" : "WR"}</th>
              <th>${zh ? "纸面 PnL" : "Paper PnL"}</th>
              <th>${zh ? "分布" : "Spread"}</th>
              <th>${zh ? "策略数" : "Strats"}</th>
            </tr></thead>
            <tbody>${poolTableRows}</tbody>
          </table>
        </div>
      </article>

      <article class="table-card grid-span-12">
        <div class="table-header"><div>
          <h3 class="panel-title">${zh ? "Forward Harness 策略竞赛台" : "Forward Harness Strategy Leaderboard"}</h3>
          <div class="panel-copy muted">${zh
            ? "sprint075（Top10 dryrun）和 sprint077（扩容批）实时竞赛。top10 = 历史高置信度候选；exp = 扩容探索。权益从 100 开始，ROI = equity−100。数据来自 logs/sprint075 & logs/sprint077。"
            : "sprint075 (top10 dryrun) and sprint077 (expansion) live race. top10 = historically high-confidence; exp = exploratory expansion. Equity starts at 100, ROI = equity−100. Source: logs/sprint075 & logs/sprint077."}</div>
        </div></div>
        <div class="table-scroll">
          <table class="alpha-strat-table">
            <thead><tr>${stratHeaders.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
            <tbody>${stratRows}</tbody>
          </table>
        </div>
      </article>

      <article class="panel grid-span-6">
        <div class="panel-header"><div>
          <h3 class="panel-title">${zh ? "历史回测 Top 5（S074 工厂，非 Forward 证明）" : "Historical Backtest Top 5 (S074 factory — not forward proof)"}</h3>
          <div class="panel-copy muted">${zh
            ? "⚠️ 纯回测数字，不能与 forward harness 混用。仅供参考策略候选方向。"
            : "⚠️ Backtest figures only — never blend with forward data. Reference only."}</div>
        </div></div>
        <div class="table-scroll">
          <table>
            <thead><tr>
              <th>${zh ? "策略" : "Strategy"}</th>
              <th>${zh ? "退出" : "Exit"}</th>
              <th>${zh ? "历史 n" : "Hist n"}</th>
              <th>${zh ? "历史 WR" : "Hist WR"}</th>
              <th>${zh ? "历史 PF" : "Hist PF"}</th>
              <th>${zh ? "历史均回报" : "Mean ret"}</th>
              <th>${zh ? "权益" : "Equity"}</th>
              <th>${zh ? "晋级带" : "Band"}</th>
            </tr></thead>
            <tbody>${hist5Rows}</tbody>
          </table>
        </div>
      </article>

      <article class="panel grid-span-6">
        <div class="panel-header"><div>
          <h3 class="panel-title">${zh ? "Shadow 信号漏斗（最近 500 条）" : "Shadow Signal Funnel (latest 500)"}</h3>
          <div class="panel-copy muted">${zh
            ? "按 signal_source 分类。pool_score_rejected = 前置规则拒绝；pool_score = shadow-only 验证；smart_money = 可执行候选。"
            : "Grouped by signal_source. pool_score_rejected = pre-filter rejected; pool_score = shadow-only probe; smart_money = executable candidate."}</div>
        </div></div>
        <div style="margin-bottom:10px">${funnelHtml}</div>
        ${rejectReasons ? `<div style="margin-top:8px"><span class="muted" style="font-size:.8rem">${zh ? "拒绝原因：" : "Reject reasons: "}</span>${rejectReasons}</div>` : ""}
        <div class="replay-kpis muted" style="margin-top:12px">
          <span><strong>${zh ? "总计" : "Total"}</strong> ${fmtNumber(funnel.total || 0, 0)}</span>
          <span><strong>${zh ? "接受" : "Accepted"}</strong> ${fmtNumber(funnel.accepted || 0, 0)}</span>
          <span><strong>${zh ? "拒绝" : "Rejected"}</strong> ${fmtNumber(funnel.rejected || 0, 0)}</span>
          <span><strong>${zh ? "已关闭" : "Closed"}</strong> ${fmtNumber(funnel.closed || 0, 0)}</span>
          <span><strong>${zh ? "在途" : "Open"}</strong> ${fmtNumber(funnel.open || 0, 0)}</span>
        </div>
      </article>

    </section>
  `;
}

function renderGuide(data) {
  const signalCount = (data.markets?.recent_signals || []).length;
  const liveTradeCount = (data.trades || []).length;
  const closedShadowCount = data.strategies?.shadow_summary?.closed_count || 0;
  const currentMode = data.meta?.dashboard_mode || "controlled";
  const phases = state.locale === "zh"
    ? [
        { name: "Phase 1", title: "基础设施", progress: 100, status: "done", detail: "Docker Compose、Redis、ClickHouse 与主链路稳定。" },
        { name: "Phase 2", title: "感知层", progress: 100, status: "done", detail: "newHeads + eth_getLogs -> market_ticks -> 归档 已验证。" },
        { name: "Phase 3", title: "信号层", progress: 100, status: "done", detail: "FeatureStore、模型打分、signals 流与 gating 已接入。" },
        { name: "Phase 4", title: "执行层", progress: 35, status: "live", detail: "当前是受控推进：shadow、executor 与 cockpit 已接通，但仍以安全和可解释性优先。" },
      ]
    : [
        { name: "Phase 1", title: "Foundation", progress: 100, status: "done", detail: "Docker Compose, Redis, ClickHouse, and the core runtime are stable." },
        { name: "Phase 2", title: "Perception", progress: 100, status: "done", detail: "newHeads + eth_getLogs -> market_ticks -> archive has been validated." },
        { name: "Phase 3", title: "Signal Layer", progress: 100, status: "done", detail: "FeatureStore, scoring, signals stream, and gating are live." },
        { name: "Phase 4", title: "Execution", progress: 35, status: "live", detail: "Rolling out in a controlled way: shadow, executor, and cockpit are connected, with safety and explainability first." },
      ];
  const flowSteps = state.locale === "zh"
    ? [
        {
          title: "链上事件",
          badge: "Input",
          copy: "监听新区块，然后为目标池拉取 swap 日志，保证只盯高价值池。",
        },
        {
          title: "实时总线",
          badge: "Redis",
          copy: "标准化后的事件先写入 Redis Streams 的 market_ticks，作为实时 handoff。",
        },
        {
          title: "特征与打分",
          badge: "Strategy",
          copy: "策略引擎消费 market_ticks，计算特征、模型得分，并写入 signals。",
        },
        {
          title: "验证与执行",
          badge: "Shadow / Executor",
          copy: "先通过 shadow 观察候选质量，再由 executor 在受控条件下真实执行。",
        },
        {
          title: "驾驶舱",
          badge: "Cockpit",
          copy: "把总览、行情、复盘、风控和安全状态汇总成一个操作首页。",
        },
      ]
    : [
        {
          title: "Onchain Events",
          badge: "Input",
          copy: "Watch new blocks and pull swap logs only for curated high-value pools.",
        },
        {
          title: "Realtime Bus",
          badge: "Redis",
          copy: "Normalized events land in the Redis Streams market_ticks surface for realtime handoff.",
        },
        {
          title: "Features & Scoring",
          badge: "Strategy",
          copy: "The strategy engine consumes market_ticks, computes features, scores candidates, and emits signals.",
        },
        {
          title: "Validation & Execution",
          badge: "Shadow / Executor",
          copy: "Shadow validates candidate quality before executor is allowed to trade under gated rules.",
        },
        {
          title: "Cockpit",
          badge: "Cockpit",
          copy: "Overview, market, replay, risk, and security are combined into one operator homepage.",
        },
      ];
  const runtimeStage = state.locale === "zh"
    ? "当前已经跑通“链上事件 -> 特征 -> 信号 -> shadow / executor -> 驾驶舱可视化”这条主链路。"
    : "The runtime has already connected the full path from onchain events to features, signals, shadow/executor outcomes, and cockpit visualization.";
  const beginnerTrad = state.locale === "zh"
    ? [
        "把它理解成一套量化交易工厂：先收行情，再算特征，再打分，再决定要不要下单，最后做复盘和风控。",
        "你在这里看到的 `signals` 类似“候选交易列表”，`shadow` 类似“纸面模拟单”，`executor` 才是真正执行过的交易。",
        "如果你只关心‘策略到底准不准’，先看 `复盘` 页的 shadow 和真实卖出结果，再看 `总览` 页的权益曲线与回撤。 ",
      ]
    : [
        "Think of EATLQ as a quant factory: ingest market events, compute features, score opportunities, decide whether to trade, then replay and manage risk.",
        "`signals` are candidate trades, `shadow` is paper-trading evidence, and `executor` is the real trade path.",
        "If you only care whether the strategy works, start with Replay for shadow/live outcomes and Dashboard for equity and drawdown.",
      ];
  const beginnerWeb3 = state.locale === "zh"
    ? [
        "这里的‘行情’不是交易所 API 直接给的 K 线，而是从链上 swap 日志重新推出来的事件价格带。",
        "一个 `pool` 就是一条 DEX 交易池，比如 `WETH/ST`；`v2 / v3` 表示 AMM 版本；`bps` 是手续费。",
        "项目核心不是盲扫全链，而是盯住少量高价值池，把实时信号和执行证据做扎实。"
      ]
    : [
        "The market view is not a direct exchange candle feed. It is reconstructed from onchain swap logs.",
        "A `pool` is a DEX trading pair such as `WETH/ST`; `v2 / v3` is the AMM family and `bps` is the fee tier.",
        "The project does not try to index the whole chain. It focuses on a curated set of high-value pools and makes the signal/execution evidence reliable."
      ];
  const changelog = state.locale === "zh"
    ? [
        "2026-04: 驾驶舱 v1 上线，加入中文/英文切换、市场页、执行页、策略页、复盘页、风控页。",
        "2026-03-28: Phase 3.5 完成，FeatureStore 修复，实时训练链路和新模型加载路径打通。",
        "2026-03-25: Base/Aerodrome 池扩展、多链训练、链感知模型加载完成。"
      ]
    : [
        "2026-04: Cockpit v1 shipped with bilingual UI, market, execution, strategy, replay, and risk views.",
        "2026-03-28: Phase 3.5 completed with FeatureStore fixes, realtime training, and upgraded model loading.",
        "2026-03-25: Base/Aerodrome pool expansion, multichain training, and chain-aware model loading completed."
      ];
  const glossary = state.locale === "zh"
    ? [
        ["最近信号", "策略引擎刚刚写进 `signals` 流的候选交易。它不等于已经成交。"],
        ["待处理人工复核", "这些信号理论上可以让人工做 Approve / Observe / Reject。只读模式下只能看，不能写。"],
        ["shadow", "不真下单，只做纸面跟踪，用来验证信号质量。"],
        ["executor", "真实执行器。只有这里产生的买卖，才算真实交易结果。"],
        ["权益曲线", "日初权益 + 已实现 PnL 的轨迹，用来看当日趋势和回撤。"],
      ]
    : [
        ["Recent signals", "Fresh candidate trades written by the strategy engine into `signals`. They are not executed trades yet."],
        ["Operator review", "Signals that could be approved, observed, or rejected by a human. In read-only mode they are view-only."],
        ["shadow", "Paper tracking without real execution, used to validate signal quality."],
        ["executor", "The live execution engine. Only executor trades count as real outcomes."],
        ["Equity curve", "Day-start equity plus realized PnL, used to judge intraday trend and drawdown."],
      ];
  return `
    <section class="guide-hero">
      <article class="guide-hero-main">
        <div class="eyebrow">${state.locale === "zh" ? "EATLQ PRODUCT OVERVIEW" : "EATLQ PRODUCT OVERVIEW"}</div>
        <h2 class="guide-hero-title">${state.locale === "zh" ? "一套把链上事件变成信号、验证与受控执行的量化操作系统" : "An operator system that turns onchain events into signals, validation, and controlled execution"}</h2>
        <div class="guide-hero-copy">${runtimeStage}</div>
        <div class="guide-hero-copy">${state.locale === "zh" ? "你可以把它理解成“链上量化版 Bloomberg + 交易值班台”。上游负责感知和打分，下游负责验证、执行、风控与复盘。": "You can think of it as a crypto-native blend of market intelligence, execution gating, and an operator control room."}</div>
        <div class="chip-row">
          <span class="route-chip">${state.locale === "zh" ? "单节点部署" : "Single-node deploy"}</span>
          <span class="route-chip">${state.locale === "zh" ? "受控执行" : "Controlled execution"}</span>
          <span class="route-chip">${state.locale === "zh" ? "中英双语" : "Bilingual UI"}</span>
        </div>
      </article>
      <article class="guide-hero-side">
        <div class="section-label">${state.locale === "zh" ? "现在的盘面" : "Current runtime"}</div>
        <div class="guide-kpi-grid">
          <div class="guide-kpi-card">
            <div class="metric-label">${state.locale === "zh" ? "信号样本" : "Signals"}</div>
            <div class="metric-value">${fmtNumber(signalCount, 0)}</div>
            <div class="metric-foot">${state.locale === "zh" ? "当前加载到驾驶舱的最新信号" : "Recent signal candidates loaded into the cockpit"}</div>
          </div>
          <div class="guide-kpi-card">
            <div class="metric-label">${state.locale === "zh" ? "Shadow 平仓" : "Shadow closes"}</div>
            <div class="metric-value">${fmtNumber(closedShadowCount, 0)}</div>
            <div class="metric-foot">${state.locale === "zh" ? "用于纸面验证策略质量" : "Paper-trade evidence for signal quality"}</div>
          </div>
          <div class="guide-kpi-card">
            <div class="metric-label">${state.locale === "zh" ? "真实交易" : "Live trades"}</div>
            <div class="metric-value">${fmtNumber(liveTradeCount, 0)}</div>
            <div class="metric-foot">${state.locale === "zh" ? "executor 已经写入的真实台账" : "Real executor ledger rows already written"}</div>
          </div>
          <div class="guide-kpi-card">
            <div class="metric-label">${state.locale === "zh" ? "运行模式" : "Mode"}</div>
            <div class="metric-value">${escapeHtml(currentMode)}</div>
            <div class="metric-foot">${state.locale === "zh" ? "当前驾驶舱对写操作的开放程度" : "How much write access is currently exposed"}</div>
          </div>
        </div>
      </article>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-12">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "系统流程总图" : "System flow map"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "这张图回答的不是“页面怎么点”，而是“系统里的数据和决策怎么流动”。" : "This explains how data and decisions flow through the system, not just how pages are organized."}</div>
          </div>
        </div>
        <div class="flow-diagram">
          ${flowSteps.map((step, index) => `
            <article class="flow-node">
              <div class="flow-badge">${escapeHtml(step.badge)}</div>
              <h4 class="flow-title">${escapeHtml(step.title)}</h4>
              <div class="flow-copy">${escapeHtml(step.copy)}</div>
              ${index < flowSteps.length - 1 ? `<div class="flow-arrow">-></div>` : ""}
            </article>
          `).join("")}
        </div>
      </article>
      <article class="panel grid-span-12">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "当前阶段进度条" : "Current phase progress"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "不是 PPT 式愿景，而是按当前已落地程度给出的工程进度感。" : "A practical engineering progress view based on what is already live today."}</div>
          </div>
        </div>
        <div class="phase-list">
          ${phases.map((phase) => `
            <article class="phase-row">
              <div class="phase-meta">
                <div>
                  <div class="phase-name">${escapeHtml(phase.name)}</div>
                  <div class="phase-title">${escapeHtml(phase.title)}</div>
                </div>
                <span class="small-pill ${phase.status === "done" ? "positive" : "warning"}">${phase.progress}%</span>
              </div>
              <div class="phase-track">
                <div class="phase-fill ${phase.status === "done" ? "done" : "live"}" style="width:${phase.progress}%"></div>
              </div>
              <div class="phase-copy">${escapeHtml(phase.detail)}</div>
            </article>
          `).join("")}
        </div>
      </article>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "现在执行到哪一步" : "Current stage"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "结合当前运行态和项目文档给出人话版状态。" : "Plain-language status based on runtime evidence and project docs."}</div>
          </div>
        </div>
        <div class="stack">
          <div class="security-row"><span>${state.locale === "zh" ? "链上采集" : "Ingestion"}</span><span>${state.locale === "zh" ? "已稳定" : "Stable"}</span></div>
          <div class="security-row"><span>${state.locale === "zh" ? "特征 / 信号" : "Features / signals"}</span><span>${state.locale === "zh" ? "已跑通" : "Live"}</span></div>
          <div class="security-row"><span>${state.locale === "zh" ? "纸面验证" : "Shadow validation"}</span><span>${fmtNumber(data.strategies.shadow_summary.closed_count || 0, 0)}</span></div>
          <div class="security-row"><span>${state.locale === "zh" ? "真实执行" : "Live execution"}</span><span>${fmtNumber((data.trades || []).length, 0)}</span></div>
          <div class="security-row"><span>${state.locale === "zh" ? "驾驶舱模式" : "Cockpit mode"}</span><span>${escapeHtml(data.meta.dashboard_mode)}</span></div>
        </div>
      </article>
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "传统量化小白怎么理解" : "For trad-quant beginners"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "从因子、信号、风控、执行这条熟悉路径来读。": "Read it through the familiar factor -> signal -> risk -> execution lens."}</div>
          </div>
        </div>
        <div class="stack">
          ${beginnerTrad.map((line) => `<div class="muted">${escapeHtml(line)}</div>`).join("")}
        </div>
      </article>
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "Web3 小白怎么理解" : "For web3 beginners"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "先把 pool、swap、链上日志这些词看懂，再回到策略。" : "Understand pools, swaps, and onchain logs first, then map them back to strategy."}</div>
          </div>
        </div>
        <div class="stack">
          ${beginnerWeb3.map((line) => `<div class="muted">${escapeHtml(line)}</div>`).join("")}
        </div>
      </article>
      <article class="panel grid-span-6">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "术语速查" : "Glossary"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "先看这块，再去看各页面会轻松很多。" : "Read this first if the other pages feel too dense."}</div>
          </div>
        </div>
        <div class="mini-list">
          ${glossary.map(([term, desc]) => `
            <div class="list-row">
              <div>
                <div>${escapeHtml(term)}</div>
                <div class="muted">${escapeHtml(desc)}</div>
              </div>
            </div>
          `).join("")}
        </div>
      </article>
      <article class="panel grid-span-6">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">${state.locale === "zh" ? "更新日志" : "Changelog"}</h3>
            <div class="panel-copy">${state.locale === "zh" ? "按里程碑汇总，不是逐文件流水账。" : "Milestone-oriented notes, not a file-by-file dump."}</div>
          </div>
        </div>
        <div class="stack">
          ${changelog.map((line) => `<div class="muted">${escapeHtml(line)}</div>`).join("")}
        </div>
      </article>
    </section>
  `;
}

function renderRisk(data) {
  const risk = data.risk;
  const settings = data.settings.capabilities;
  const canWrite = settings.action_token_required && !settings.read_only;
  const panicDisabled = !settings.panic_actions_enabled || !canWrite;
  return `
    <section class="metric-grid">
      <article class="hero-card">
        <div class="metric-label">Drawdown</div>
        <div class="metric-value negative">${fmtSigned(risk.drawdown.drawdown_pct, 2, " %")}</div>
        <div class="metric-foot">${fmtSigned(risk.drawdown.drawdown_usd, 4, " USD")} ${state.locale === "zh" ? "相对峰值" : "from peak"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">Cap hits</div>
        <div class="metric-value">${fmtNumber(risk.cap_hit_count, 0)}</div>
        <div class="metric-foot">${state.locale === "zh" ? "executor 仓位上限触发次数" : "Executor size cap interventions"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">Latest signal</div>
        <div class="metric-value">${risk.latest_timestamps.signal_ts ? fmtTs(risk.latest_timestamps.signal_ts).split(",")[1]?.trim() || fmtTs(risk.latest_timestamps.signal_ts) : "—"}</div>
        <div class="metric-foot">${state.locale === "zh" ? "信号流新鲜度检查点" : "Stream freshness checkpoint"}</div>
      </article>
      <article class="hero-card">
        <div class="metric-label">Control mode</div>
        <div class="metric-value">${settings.read_only ? tr("mode_read_only") : tr("mode_controlled")}</div>
        <div class="metric-foot">${state.locale === "zh" ? `Action token ${settings.action_token_required ? "已要求" : "未配置"}` : `Action token ${settings.action_token_required ? "required" : "not set"}`}</div>
      </article>
    </section>
    <section class="panel-grid" style="margin-top:16px">
      <article class="panel grid-span-8">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Risk surface</h3>
            <div class="panel-copy">Alerts are generated from panic, concurrency, drawdown, signal freshness, and shadow quality.</div>
          </div>
        </div>
        <div class="alert-list">${alertList(risk.alerts, data.settings.capabilities)}</div>
      </article>
      <article class="panel grid-span-4">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Controlled actions</h3>
            <div class="panel-copy">Only a very small write surface is enabled in V1.</div>
          </div>
        </div>
        <div class="stack">
          <div class="input-group">
            <label class="muted" for="panic-reason-input">Panic reason</label>
            <textarea id="panic-reason-input" class="textarea" placeholder="manual dashboard panic"></textarea>
          </div>
          <div class="panel-actions">
            <button class="btn-danger" id="set-panic-btn" ${panicDisabled ? "disabled" : ""}>Set SYSTEM_PANIC</button>
            <button class="btn" id="clear-panic-btn" ${panicDisabled ? "disabled" : ""}>Clear SYSTEM_PANIC</button>
          </div>
          <button class="btn-ghost" id="set-action-token-btn">${state.actionToken ? tr("update_action_token") : tr("set_action_token")}</button>
          <div class="footer-note">Write actions require the X-Dashboard-Action-Token secret and remain separate from executor trade writes.</div>
        </div>
      </article>
    </section>
  `;
}

function renderSettings(data) {
  const security = data.settings.security;
  const limits = data.settings.limits;
  return `
    <section class="three-col">
      <article class="panel">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Security baseline</h3>
            <div class="panel-copy">The cockpit is designed for reverse proxy deployment with a minimal public surface.</div>
          </div>
        </div>
        <div class="stack">
          <div class="security-row"><span class="muted">Bind</span><span class="mono">${escapeHtml(security.dashboard_bind)}</span></div>
          <div class="security-row"><span class="muted">Basic auth</span><span>${escapeHtml(security.basic_auth)}</span></div>
          <div class="security-row"><span class="muted">Action token</span><span>${escapeHtml(security.action_token)}</span></div>
          <div class="security-row"><span class="muted">Read-only</span><span>${escapeHtml(security.read_only)}</span></div>
          <div class="security-row"><span class="muted">Panic actions</span><span>${escapeHtml(security.panic_actions)}</span></div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Cockpit limits</h3>
            <div class="panel-copy">Useful runtime caps and data loading boundaries.</div>
          </div>
        </div>
        <div class="stack">
          <div class="security-row"><span class="muted">Max concurrent positions</span><span>${limits.max_concurrent_positions}</span></div>
          <div class="security-row"><span class="muted">Signals loaded</span><span>${limits.signals_loaded}</span></div>
          <div class="security-row"><span class="muted">Shadow loaded</span><span>${limits.shadow_loaded}</span></div>
        </div>
      </article>
      <article class="panel">
        <div class="panel-header">
          <div>
            <h3 class="panel-title">Deployment notes</h3>
            <div class="panel-copy">Recommended VPS posture for this dashboard runtime.</div>
          </div>
        </div>
        <div class="stack">
          <div class="muted">Expose only 80/443 at the reverse proxy layer.</div>
          <div class="muted">Keep dashboard container bound to 127.0.0.1 by default.</div>
          <div class="muted">Do not publish Redis, Postgres, ClickHouse, or strategy services to the public internet.</div>
          <div class="muted">Use Basic Auth for read access and an action token for the small write surface.</div>
        </div>
      </article>
    </section>
  `;
}

function renderMain(data) {
  switch (state.route) {
    case "alpha":
      return renderAlpha(data);
    case "guide":
      return renderGuide(data);
    case "markets":
      return renderMarkets(data);
    case "execution":
      return renderExecution(data);
    case "strategies":
      return renderStrategies(data);
    case "replay":
      return renderReplay(data);
    case "risk":
      return renderRisk(data);
    case "settings":
      return renderSettings(data);
    case "dashboard":
    default:
      return renderDashboard(data);
  }
}

function renderShell(data) {
  const route = routeMeta();
  const routeSuffix = route.id === "guide" ? "" : ` ${tr("route_suffix")}`;
  return `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-kicker">EATLQ runtime</div>
          <div class="brand-title">Quant Cockpit</div>
          <div class="brand-copy">A high-density operator shell for strategy, execution, replay, and risk.</div>
        </div>
        <div class="status-card">
          <div class="section-label">${tr("runtime")}</div>
          <div class="status-row">
            <span>Status</span>
            <span class="status-pill ${data.status === "紧急停止" || data.status === "panic" ? "panic" : "live"}">${data.status === "紧急停止" || data.status === "panic" ? tr("status_panic") : tr("status_live")}</span>
          </div>
          <div class="status-row">
            <span>Chain</span>
            <span class="mono">${data.meta.chain_id}</span>
          </div>
          <div class="status-row">
            <span>Update</span>
            <span class="mono">${fmtTs(data.meta.server_ts)}</span>
          </div>
        </div>
        <nav class="nav">
          ${ROUTES.map((item) => `
            <a class="nav-link ${item.id === state.route ? "active" : ""}" href="#${item.id}" data-route="${item.id}">
              <span class="nav-title">${routeLabel(item)}</span>
              <span class="nav-copy">${routeCopy(item)}</span>
            </a>
          `).join("")}
        </nav>
        <div class="security-card">
          <div class="section-label">${tr("security")}</div>
          <div class="security-row"><span>Basic auth</span><span>${data.meta.security.basic_auth}</span></div>
          <div class="security-row"><span>Action token</span><span>${data.meta.security.action_token}</span></div>
          <div class="security-row"><span>Mode</span><span>${data.meta.security.read_only}</span></div>
        </div>
        <div class="quick-actions">
          <div class="section-label">${tr("operator")}</div>
          <div class="sidebar-actions">
            <button class="btn-ghost" id="refresh-now-btn">${tr("refresh_now")}</button>
            <button class="btn-ghost" id="set-token-btn">${state.actionToken ? tr("action_token_ready") : tr("set_action_token")}</button>
          </div>
        </div>
      </aside>
      <main class="main">
        <header class="topbar">
          <div class="topbar-title">
            <div class="eyebrow">${tr("headline_kicker")}</div>
            <h1 class="headline">${routeLabel(route)}</h1>
            <div class="topbar-copy">${routeCopy(route)}${routeSuffix}</div>
          </div>
          <div class="topbar-meta">
            <div class="meta-stack">
              <span class="route-chip">ETH ${fmtNumber(data.eth_price_usd, 0)}</span>
              <span class="route-chip">${data.meta.dashboard_mode}</span>
              <span class="route-chip">${data.trades.length} ${tr("ledger_rows")}</span>
              <button class="btn-ghost" id="toggle-locale-btn">${state.locale === "zh" ? "EN" : "中文"}</button>
            </div>
            <div class="muted">${tr("last_refresh")} ${fmtTs(data.server_ts)}</div>
          </div>
        </header>
        ${renderMain(data)}
      </main>
    </div>
  `;
}

function initEquityChart(data) {
  const container = document.getElementById("equity-chart");
  if (!container || !window.LightweightCharts) return;
  const chart = LightweightCharts.createChart(container, chartTheme());
  const series = chart.addAreaSeries({
    lineColor: "#8da2ff",
    topColor: "rgba(141,162,255,0.32)",
    bottomColor: "rgba(141,162,255,0.04)",
  });
  series.setData((data.equity_curve || data.pnl_curve || []).map((point) => ({
    time: point.time,
    value: point.value,
  })));
  chart.timeScale().fitContent();
  state.charts.equity = chart;
}

function initMarketChart(data) {
  const container = document.getElementById("market-chart");
  if (!container || !window.LightweightCharts) return;
  const snapshot = currentMarketSnapshot(data);
  if (!snapshot?.candles?.length) return;
  const chart = LightweightCharts.createChart(container, chartTheme());
  const candle = chart.addCandlestickSeries({
    upColor: "#58e48d",
    borderUpColor: "#58e48d",
    wickUpColor: "#58e48d",
    downColor: "#ff6b6b",
    borderDownColor: "#ff6b6b",
    wickDownColor: "#ff6b6b",
  });
  candle.setData(snapshot.candles || []);
  if (typeof candle.setMarkers === "function") {
    candle.setMarkers(snapshot.markers || []);
  }
  const histogram = chart.addHistogramSeries({
    color: "rgba(255,255,255,0.28)",
    priceFormat: { type: "volume" },
    priceScaleId: "",
  });
  histogram.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  histogram.setData((snapshot.volumes || []).map((item) => ({
    time: item.time,
    value: item.value,
    color: "rgba(255,255,255,0.18)",
  })));
  chart.timeScale().fitContent();
  state.charts.market = chart;
}

function chartTheme() {
  return {
    layout: {
      background: { color: "rgba(0,0,0,0)" },
      textColor: "rgba(245,241,232,0.72)",
      fontFamily: "Inter, Segoe UI, sans-serif",
    },
    grid: {
      vertLines: { color: "rgba(255,255,255,0.06)" },
      horzLines: { color: "rgba(255,255,255,0.06)" },
    },
    rightPriceScale: {
      borderColor: "rgba(255,255,255,0.12)",
    },
    timeScale: {
      borderColor: "rgba(255,255,255,0.12)",
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: {
      vertLine: { color: "rgba(255,255,255,0.18)" },
      horzLine: { color: "rgba(255,255,255,0.18)" },
    },
  };
}

function bindEvents() {
  const appRoot = document.getElementById("app");
  if (appRoot && appRoot.dataset.timelineDelegation !== "1") {
    appRoot.dataset.timelineDelegation = "1";
    appRoot.addEventListener("click", (event) => {
      const card = event.target.closest("[data-timeline-pool]");
      if (!card) return;
      const pool = card.getAttribute("data-timeline-pool") || "";
      if (!pool) return;
      state.selectedPool = pool;
      setRoute("markets");
    });
  }
  document.querySelectorAll("[data-route]").forEach((node) => {
    node.addEventListener("click", (event) => {
      const route = event.currentTarget.getAttribute("data-route");
      if (route) setRoute(route);
    });
  });
  document.querySelectorAll("[data-pool]").forEach((node) => {
    node.addEventListener("click", () => pickPool(node.getAttribute("data-pool")));
  });
  document.querySelectorAll("[data-ack-alert]").forEach((node) => {
    node.addEventListener("click", async () => {
      const alertId = node.getAttribute("data-ack-alert");
      await postAction("/api/actions/ack-alert", { alert_id: alertId }, tr("alert_acknowledged"));
    });
  });
  document.querySelectorAll("[data-review]").forEach((node) => {
    node.addEventListener("click", async () => {
      const raw = node.getAttribute("data-review") || "";
      const [signalId, decision] = raw.split(":");
      const successMessage = decision === "approved"
        ? tr("signal_approved")
        : decision === "observed"
          ? tr("signal_observed")
          : tr("signal_rejected");
      await postAction("/api/actions/review-signal", { signal_id: signalId, decision }, successMessage);
    });
  });
  document.getElementById("refresh-now-btn")?.addEventListener("click", refreshData);
  document.getElementById("set-token-btn")?.addEventListener("click", promptActionToken);
  document.getElementById("set-action-token-btn")?.addEventListener("click", promptActionToken);
  document.getElementById("toggle-locale-btn")?.addEventListener("click", () => {
    state.locale = state.locale === "zh" ? "en" : "zh";
    localStorage.setItem("dashboard-locale", state.locale);
    render();
  });
  document.getElementById("set-panic-btn")?.addEventListener("click", async () => {
    const reason = document.getElementById("panic-reason-input")?.value?.trim() || "manual dashboard panic";
    await postAction("/api/actions/panic", { mode: "set", reason }, tr("panic_set"));
  });
  document.getElementById("clear-panic-btn")?.addEventListener("click", async () => {
    await postAction("/api/actions/panic", { mode: "clear" }, tr("panic_cleared"));
  });
}

function initCharts() {
  destroyCharts();
  if (!state.data) return;
  if (state.route === "dashboard") initEquityChart(state.data);
  if (state.route === "markets") initMarketChart(state.data);
}

function render() {
  const app = document.getElementById("app");
  if (!app || !state.data) return;
  const localizedData = localizeApiData(state.data);
  app.innerHTML = applyMarkupTranslations(renderShell(localizedData));
  bindEvents();
  initCharts();
}

async function postAction(path, payload, successMessage) {
  const headers = { "Content-Type": "application/json" };
  if (state.actionToken) headers["X-Dashboard-Action-Token"] = state.actionToken;
  const response = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    showToast(result.error || tr("action_failed"), true);
    if ((result.error || "").includes("token")) {
      promptActionToken();
    }
    return;
  }
  showToast(successMessage);
  await refreshData();
}

function promptActionToken() {
  const token = window.prompt(tr("prompt_action_token"), state.actionToken || "");
  if (token === null) return;
  state.actionToken = token.trim();
  localStorage.setItem("dashboard-action-token", state.actionToken);
  render();
}

async function refreshData() {
  try {
    const response = await fetch("/api/data", { cache: "no-store" });
    const data = await response.json();
    state.data = data;
    if (!state.selectedPool && data.markets?.active_pool?.pool) {
      state.selectedPool = data.markets.active_pool.pool;
    }
    render();
  } catch (error) {
    showToast(`${tr("refresh_failed_prefix")}${error}`, true);
  }
}

window.addEventListener("hashchange", () => {
  const nextRoute = (location.hash || "#dashboard").slice(1);
  if (ROUTES.some((route) => route.id === nextRoute)) {
    state.route = nextRoute;
    render();
  }
});

async function boot() {
  if (!ROUTES.some((route) => route.id === state.route)) state.route = "dashboard";
  await refreshData();
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(refreshData, 300000); // 5-minute polling
}

boot();
