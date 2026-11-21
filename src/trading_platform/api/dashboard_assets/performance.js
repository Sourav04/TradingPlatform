let performanceBusy = false;
let performanceSignature = "";

function showSession(record) {
  const holder = element("session-cards");
  holder.replaceChildren();
  const status = element("session-status");
  if (!record) {
    status.textContent = "No active streaming paper deployment.";
  }
  const attention = [];
  if (record && record.feed_status !== "fresh") attention.push("Feed " + record.feed_status);
  if (record?.units) attention.push(record.units + " units open");
  if (record?.units && record.stop === null) attention.push("Protective stop missing");
  if (record && record.remaining_loss_budget !== null && Number(record.remaining_loss_budget) === 0) {
    attention.push("Daily loss limit reached");
  }
  status.classList.toggle("attention", Boolean(attention.length));
  if (record) status.textContent = [`${record.symbol} · ${record.status}`, ...attention,
    record.current ? "Last mark " + formatTime(record.current.as_of) : "No marks recorded today"].join(" · ");
  const items = [
    ["Today’s net P&L", record?.current?.pnl],
    ["Today’s realized P&L", record?.current?.realized],
    ["Today’s unrealized change", record?.current?.unrealized],
    ["Remaining daily loss budget", record?.remaining_loss_budget],
  ];
  const cards = document.createElement("div");
  cards.className = "metrics";
  for (const [label, value] of items) {
    const card = textNode("article", "", "panel");
    card.append(textNode("span", label), textNode("strong", value == null ? "—" : money.format(value)));
    cards.append(card);
  }
  holder.append(cards);
}

function showPerformance(records) {
  const holder = element("performance-rows");
  const open = new Set([...holder.querySelectorAll("details[open]")].map((node) => node.dataset.id));
  holder.replaceChildren(...records.map((record, index) => {
    const node = document.createElement("details");
    node.dataset.id = record.deployment_id;
    node.className = "record";
    node.open = open.has(record.deployment_id) || (!performanceSignature && index === 0);
    node.append(textNode("summary", `${record.symbol} · ${record.strategy} v${record.version} · ${record.status}`));
    node.append(textNode("p", "Paper history is based on observed marks. Compare the date range and fill model before interpreting backtest differences."));
    if (!record.history_complete) node.append(textNode("p", "Earlier observations predate session reporting."));
    function historical(label, value) {
      return value ? [label, money.format(value.pnl), compact.format(value.drawdown) + "%", value.fills]
        : [label, "—", "—", "—"];
    }
    node.append(tableWithRows(["Evidence", "Net P&L", "Max drawdown", "Fills"], [
      record.sessions.length ? ["Recorded streaming paper", money.format(record.recorded_pnl),
        compact.format(record.max_drawdown) + "%", record.fills] : ["Recorded streaming paper", "—", "—", "—"],
      historical("Approved historical evidence", record.historical),
      historical("Later validation window", record.validation),
    ]));
    node.append(textNode("p", `${record.sessions.length} observed sessions · ${record.losing_sessions} losing sessions`));
    if (record.sessions.length) node.append(tableWithRows(
      ["Date (IST)", "Net P&L", "Realized change", "Unrealized change", "Equity", "Fills"],
      [...record.sessions].reverse().map((row) => [
        row.date, money.format(row.pnl), money.format(row.realized), money.format(row.unrealized),
        money.format(row.equity), row.fills,
      ]),
    ));
    return node;
  }));
}

async function refreshPerformance() {
  if (performanceBusy) return;
  performanceBusy = true;
  try {
    const records = await requestOperation("/operations/paper-performance");
    const signature = JSON.stringify(records);
    if (signature !== performanceSignature) {
      showSession(records.find((item) => item.status !== "closed"));
      showPerformance(records);
      performanceSignature = signature;
    }
    element("performance-status").textContent = records.length ? "" : "Paper results appear after a streaming deployment is approved.";
  } catch {
    element("performance-status").textContent = "Unable to refresh paper performance.";
    element("session-status").textContent = "Unable to refresh session data.";
    performanceSignature = "";
  } finally {
    performanceBusy = false;
  }
}

refreshPerformance();
setInterval(refreshPerformance, 3000);
