const workspaceViews = new Map([
  ["session-overview", ["Overview", "Session overview", "Monitor your paper deployment, portfolio risk and session results."]],
  ["catalog", ["Strategies", "Strategies", "Review available strategies, automatic runs and saved evidence."]],
  ["research", ["Backtest", "Run a backtest", "Test a strategy on historical candles and review the resulting trades."]],
  ["paper", ["Paper trading", "Paper trading", "Inspect deployments and manage simulated positions using shared risk limits."]],
  ["paper-performance", ["Performance", "Paper performance", "Compare recorded paper sessions with the evidence approved for deployment."]],
  ["newsroom", ["News signals", "News signals", "Review official releases and their relevance to current exposure."]],
  ["live", ["Live orders", "Live orders", "Review the status of real-money execution."]],
]);

function selectWorkspaceView() {
  const target = location.hash.slice(1);
  const evidence = target.match(/^evidence-[a-f0-9]{32}$/);
  const viewId = evidence ? "catalog" : workspaceViews.has(target) ? target : "session-overview";
  const [locationLabel, title, description] = workspaceViews.get(viewId);
  for (const id of workspaceViews.keys()) element(id).hidden = id !== viewId;
  for (const link of document.querySelectorAll(".workspace-nav a")) {
    if (link.hash === "#" + viewId) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  element("workspace-location").textContent = locationLabel;
  element("workspace-title").textContent = title;
  element("workspace-description").textContent = description;
  document.title = title + " | TradingPlatform";
  if (evidence) {
    const item = element(target);
    if (item) {
      item.open = true;
      item.scrollIntoView({ block: "nearest" });
    }
  } else if (target !== "workspace-title") {
    window.scrollTo(0, 0);
  }
}

document.querySelector(".skip-link").addEventListener("click", (event) => {
  event.preventDefault();
  element("workspace-title").focus();
});
window.addEventListener("hashchange", selectWorkspaceView);
selectWorkspaceView();
