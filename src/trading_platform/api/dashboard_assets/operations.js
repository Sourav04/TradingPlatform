const opsStatus = element("operations-status");
let operationsBusy = false;

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function tableRow(values) {
  const row = document.createElement("tr");
  for (const value of values) row.insertCell().textContent = value;
  return row;
}

function tableWithRows(headers, rows) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  for (const label of headers) {
    const th = textNode("th", label);
    th.scope = "col";
    header.append(th);
  }
  const body = table.createTBody();
  body.append(...rows.map(tableRow));
  wrap.append(table);
  return wrap;
}

async function requestOperation(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Request failed");
  return result;
}

function actionButton(label, callback, confirmMessage) {
  const button = textNode("button", label, "secondary");
  button.type = "button";
  button.addEventListener("click", async () => {
    if (operationsBusy || (confirmMessage && !window.confirm(confirmMessage))) return;
    operationsBusy = true;
    button.disabled = true;
    try {
      await callback();
      opsStatus.textContent = label + " completed.";
      await refreshOperations();
    } catch (error) {
      await refreshOperations();
      opsStatus.textContent = error.message;
    } finally {
      operationsBusy = false;
      button.disabled = false;
    }
  });
  return button;
}

function formatTime(value) {
  return new Date(value).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }) + " IST";
}

function renderEvidence(items, current, policy) {
  element("evidence-empty").hidden = items.length > 0;
  element("evidence-rows").replaceChildren(...items.map((item) => {
    const node = document.createElement("details");
    node.className = "record";
    node.id = "evidence-" + item.id;
    const title = `${item.symbol} · ${item.strategy_id} v${item.strategy_version} · ${item.status} · ${money.format(item.historical.pnl)}`;
    node.append(textNode("summary", title));
    node.append(textNode("p", item.confidence));
    node.append(textNode("p", `${item.dataset} · ${item.settings.start} to ${item.settings.end} · ${formatTime(item.created_at)}`));
    node.append(tableWithRows(["Evidence", "Bars", "Fills", "Net P&L", "Drawdown"], [
      ["Historical run", item.historical], ["Later date window", item.validation],
      ["Double slippage", item.stress],
    ].map(([name, result]) => result ? [
      name, result.bars, result.fills, money.format(result.pnl), compact.format(result.drawdown) + "%",
    ] : [name, "—", "—", "—", "—"])));
    const reasons = document.createElement("ul");
    reasons.append(...item.reasons.map(listItem));
    node.append(reasons);
    const settings = item.settings;
    node.append(textNode("p", `EMA ${settings.fast}/${settings.slow} · ${settings.units} units · ${settings.stop_loss_percent}% stop · ${settings.max_risk_percent}% risk per trade`));
    if (item.status === "Paper only" && Number(settings.max_risk_percent) === Number(policy) && !current) {
      node.append(actionButton("Approve paper deployment", () =>
        requestOperation("/operations/paper", { evidence_id: item.id }),
        `Approve ${item.strategy_id} v${item.strategy_version} for ${item.symbol} in paper replay?`
      ));
    }
    return node;
  }));
}

function renderPaper(items) {
  element("paper-empty").hidden = items.length > 0;
  element("paper-rows").replaceChildren(...items.map((paper) => {
    const node = document.createElement("details");
    node.className = "record";
    node.open = paper.status !== "closed";
    node.append(textNode("summary", `${paper.symbol} · ${paper.strategy_id} v${paper.strategy_version} · ${paper.status} · ${paper.cursor}/${paper.total_bars} candles`));
    node.append(textNode("p", `Approved ${formatTime(paper.approved_at)} · ${paper.units} units · Stop ${paper.stop === null ? "—" : money.format(paper.stop)}`));
    node.append(textNode("p", `Equity ${money.format(paper.equity)} · Cash ${money.format(paper.cash)} · Realized ${money.format(paper.realized)} · Unrealized ${money.format(paper.unrealized)}`));
    if (paper.fills.length) {
      node.append(tableWithRows(["Session", "Side", "Units", "Price", "Stop", "Reason", "Status"],
        paper.fills.map((fill) => [
          dateLabel.format(new Date(fill.fill_bar)), fill.units > 0 ? "Buy" : "Sell", Math.abs(fill.units),
          money.format(fill.execution_price ?? fill.open_price), fill.stop_price === null ? "—" : money.format(fill.stop_price),
          ({ema: "EMA crossover", stop_loss: "Stop loss", paper_close: "Paper close"})[fill.reason] ?? fill.reason,
          "Simulated fill",
        ])));
    } else {
      node.append(textNode("p", "No fills recorded."));
    }
    if (paper.rejections.length) {
      const rejections = document.createElement("ul");
      rejections.id = "paper-rejections-" + paper.id;
      rejections.append(...paper.rejections.map(listItem));
      node.append(rejections);
    }
    const audit = document.createElement("details");
    audit.append(textNode("summary", "Approval and changes"));
    const auditRows = document.createElement("ul");
    auditRows.append(...paper.audit.map((item) => listItem(`${formatTime(item.at)} · ${item.actor} · ${item.action}`)));
    audit.append(auditRows);
    node.append(audit);
    function send(action) {
      return requestOperation("/operations/paper/" + paper.id, { action, revision: paper.revision });
    }
    if (paper.status !== "closed") {
      const actions = document.createElement("div");
      actions.className = "actions";
      if (paper.status === "active" || paper.status === "paused") {
        actions.append(actionButton("Advance one candle", () => send("advance")));
        const [label, action] = paper.status === "active" ? ["Pause entries", "pause"] : ["Resume entries", "resume"];
        actions.append(actionButton(label, () => send(action)));
      }
      const message = paper.units ? "Close this paper deployment and flatten at the last observed close?" : "Close this paper deployment?";
      actions.append(actionButton("Close deployment", () => send("close"), message));
      node.append(actions);
    }
    return node;
  }));
}

function renderNews(signals, last) {
  element("newsroom-status").textContent = last
    ? "Last fresh observation " + formatTime(last)
    : "No fresh official items recorded. Run the official news collector to populate this view.";
  element("newsroom-rows").replaceChildren(...signals.map((signal) => {
    const article = document.createElement("article");
    article.className = "record";
    const event = signal.event;
    const link = textNode("a", event.headline);
    link.href = event.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    article.append(link, textNode("p", `${event.source} · ${signal.scope} · ${signal.recommendation} · ${signal.impact}`),
      textNode("p", signal.rationale),
      textNode("p", `Published ${formatTime(event.published_at)} · First seen ${formatTime(event.ingested_at)}`));
    return article;
  }));
}

async function refreshOperations() {
  try {
    const data = await requestOperation("/operations");
    const select = element("strategy");
    const previous = select.value;
    select.replaceChildren(...data.strategies.map((item) => {
      const option = textNode("option", `${item.name} v${item.version}`);
      option.value = item.id + "@" + item.version;
      return option;
    }));
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
    element("catalog-rows").replaceChildren(...data.strategies.map((item) =>
      tableRow([item.name, item.version, item.source, "Paper replay"])));
    element("shared-policy").textContent = `Shared policy · ${data.risk_per_trade_percent}% risk per trade · ${data.daily_loss_percent}% daily loss limit · ${money.format(data.max_gross_inr)} gross exposure cap · One active paper deployment`;
    renderEvidence(data.evidence, data.paper.some((item) => item.status !== "closed"), data.risk_per_trade_percent);
    renderPaper(data.paper);
    renderNews(data.news, data.latest_news_at);
    element("live-status").textContent = "Live execution disabled. No strategy is eligible for live deployment.";
  } catch (error) {
    opsStatus.textContent = "Unable to load operations. " + error.message;
  }
}

element("refresh-operations").addEventListener("click", () => {
  refreshOperations();
  refreshJobs();
});
refreshOperations();


let jobsBusy = false;
let lastCompleted = "";
let renderedJobs = "";

async function refreshJobs() {
  if (jobsBusy) return;
  jobsBusy = true;
  const status = element("jobs-status");
  try {
    const records = await requestOperation("/backtests/jobs");
    status.textContent = records.length ? "" : "Enable automatic backtests with a sample or validated CSV.";
    const completed = records.filter((job) => job.evidence_id).map((job) => job.evidence_id).join(",");
    if (lastCompleted !== completed) {
      lastCompleted = completed;
      await refreshOperations();
    }
    const signature = JSON.stringify(records);
    if (signature === renderedJobs) return;
    renderedJobs = signature;
    element("job-rows").replaceChildren(...records.map((job) => {
      const node = document.createElement("details");
      node.className = "record";
      const state = job.status[0].toUpperCase() + job.status.slice(1);
      node.append(textNode("summary", `${state} · ${job.symbol} · ${job.strategy_id} v${job.strategy_version}`));
      const settings = job.settings;
      node.append(textNode("p", `${settings.start} to ${settings.end} · EMA ${settings.fast}/${settings.slow} · ${settings.units} units · Suite v${job.suite_version}`));
      node.append(textNode("p", `Fees ${settings.costs.fee_bps} bps · Slippage ${settings.costs.slippage_bps} bps · Stop ${settings.stop_loss_percent}% · Cash ${money.format(settings.initial_cash)}`));
      node.append(textNode("p", `${job.sample ? "Synthetic sample" : "Uploaded CSV"} · SHA-256 ${job.dataset_hash}`));
      if (job.historical) {
        const result = job.historical;
        node.append(textNode("p", `Net P&L ${money.format(result.pnl)} · Drawdown ${compact.format(result.drawdown)}% · ${result.fills} fills`));
      }
      if (job.error) node.append(textNode("p", job.error, "negative"));
      if (job.evidence_id && element("evidence-" + job.evidence_id)) {
        const link = textNode("a", "View evidence");
        link.href = "#evidence-" + job.evidence_id;
        link.addEventListener("click", () => {
          const evidence = element("evidence-" + job.evidence_id);
          if (evidence) evidence.open = true;
        });
        node.append(link);
      }
      return node;
    }));
  } catch (error) {
    status.textContent = "Unable to load automatic backtests. " + error.message;
  } finally {
    jobsBusy = false;
  }
}

refreshJobs();
setInterval(refreshJobs, 3000);
