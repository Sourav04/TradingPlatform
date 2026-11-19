const element = (id) => document.getElementById(id);
const form = element("config");
const fileInput = element("csv");
const maxBytes = 2_000_000;
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const compact = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const dateLabel = new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeZone: "Asia/Kolkata" });
let equity = [];

function drawChart() {
  const canvas = element("chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.font = "12px system-ui";
  if (!equity.length) {
    ctx.fillStyle = "#afbed2";
    ctx.fillText("Run a backtest to see equity over time.", 6, 25);
    return;
  }
  const values = equity.map((row) => Number(row.value));
  const min = values.reduce((a, b) => Math.min(a, b));
  const max = values.reduce((a, b) => Math.max(a, b));
  const left = Math.max(ctx.measureText(compact.format(min)).width, ctx.measureText(compact.format(max)).width) + 12;
  const right = width - 10;
  const top = 13;
  const bottom = height - 26;
  const range = max - min;
  ctx.fillStyle = "#afbed2";
  for (let i = 0; i <= 3; i++) {
    const y = top + (bottom - top) * i / 3;
    ctx.strokeStyle = "#2b374b";
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(right, y);
    ctx.stroke();
    ctx.fillText(compact.format(max - range * i / 3), 0, y + 4);
    if (!range) break;
  }
  ctx.strokeStyle = "#66e4b7";
  ctx.lineWidth = 2;
  ctx.beginPath();
  const stride = Math.max(1, Math.floor(values.length / (2 * width)));
  values.forEach((value, index) => {
    if (index % stride && index !== values.length - 1) return;
    const x = values.length === 1 ? (left + right) / 2 : left + (right - left) * index / (values.length - 1);
    const y = range ? bottom - (bottom - top) * (value - min) / range : (bottom + top) / 2;
    if (values.length === 1) ctx.arc(x, y, 3, 0, Math.PI * 2);
    else if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  const first = dateLabel.format(new Date(equity[0].bar));
  const last = dateLabel.format(new Date(equity.at(-1).bar));
  ctx.fillText(first, left, height - 5);
  if (first !== last) ctx.fillText(last, right - ctx.measureText(last).width, height - 5);
}

function listItem(text) {
  const row = document.createElement("li");
  row.textContent = text;
  return row;
}

function render({ backtest, news, news_as_of, news_mode }) {
  const pnl = Number(backtest.net_pnl);
  element("pnl").textContent = money.format(pnl);
  element("pnl").className = pnl < 0 ? "negative" : "positive";
  element("equity").textContent = money.format(backtest.final_equity);
  element("drawdown").textContent = compact.format(backtest.max_drawdown_percent) + "%";
  element("fills").textContent = backtest.trade_count;
  equity = backtest.equity_curve;
  drawChart();

  element("trades").replaceChildren(...backtest.fills.map((fill) => {
    const row = document.createElement("tr");
    const values = [
      dateLabel.format(new Date(fill.fill_bar)), fill.units > 0 ? "Buy" : "Sell", Math.abs(fill.units),
      money.format(fill.execution_price ?? fill.open_price),
      fill.stop_price === null ? "—" : money.format(fill.stop_price),
      fill.reason === "stop_loss" ? "Stop loss" : "EMA crossover",
    ];
    values.forEach((value) => { row.insertCell().textContent = value; });
    return row;
  }));
  const units = equity.at(-1).units;
  element("trade-note").textContent = (backtest.fills.length ? "" : "No fills. ") +
    "Final position: " + units + " units · Costs: " + money.format(backtest.total_cost);
  element("rejections").replaceChildren(...backtest.rejections.map((row) =>
    listItem(dateLabel.format(new Date(row.execution_bar)) + " · " + row.reason)
  ));
  element("news-note").textContent = (news_mode === "mock" ? "Mock news · " : "") +
    (news.length ? "" : "No fresh events at candle opens · ") +
    "Through " + new Date(news_as_of).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" }) + " IST";
  element("news").replaceChildren(...news.map((event) => {
    const row = listItem(event.source + " · ");
    const link = document.createElement("a");
    link.href = event.url;
    link.textContent = event.headline;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(link);
    return row;
  }));
}

element("sample").addEventListener("click", () => {
  form.reset();
  element("dataset").textContent = "Synthetic daily candles";
  element("status").textContent = "Sample selected.";
});
fileInput.addEventListener("change", () => {
  element("dataset").textContent = fileInput.files[0]?.name ?? "Synthetic daily candles";
});
new ResizeObserver(drawChart).observe(element("chart"));

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const run = element("run");
  const status = element("status");
  const payload = Object.fromEntries(new FormData(form));
  const file = fileInput.files[0];
  run.disabled = true;
  status.textContent = "Running backtest…";
  try {
    if (file && file.size > maxBytes) throw new Error("CSV must be 2 MB or smaller.");
    if (file) {
      payload.csv = await file.text();
    } else {
      const sample = await fetch("/dashboard/assets/sample.csv");
      if (!sample.ok) throw new Error("Sample CSV unavailable.");
      payload.csv = await sample.text();
    }
    payload.mock_news = !file;
    const response = await fetch("/backtests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      const detail = result.detail;
      throw new Error(typeof detail === "string" ? detail : detail?.map((item) => item.msg).join(" · ") || "Backtest failed.");
    }
    render(result);
    status.textContent = "Backtest complete.";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    run.disabled = false;
  }
});
