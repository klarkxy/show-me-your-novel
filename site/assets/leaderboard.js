(() => {
  "use strict";

  const body = document.querySelector("#leaderboard-body");
  const buttons = Array.from(document.querySelectorAll("[data-sort]"));
  const slider = document.querySelector("#rank-limit");
  const output = document.querySelector("#rank-limit-output");
  const table = document.querySelector(".leaderboard");

  if (!body || !slider || !output) return;

  const rows = Array.from(body.querySelectorAll("tr"));
  const pinnedMetric =
    (table && table.dataset && table.dataset.pinnedMetric) || "tscore";
  let activeMetric = pinnedMetric;

  const isRankable = (row) => row.dataset.rankable === "true";
  const configOrder = (row) => {
    const value = Number(row.dataset.configOrder);
    return Number.isFinite(value) ? value : Number.MAX_SAFE_INTEGER;
  };

  const numericValue = (row, metric) => {
    const raw = row.dataset[metric.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())];
    if (raw === undefined || raw === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };

  const setMetricColumns = (metric) => {
    if (!table || typeof table.querySelectorAll !== "function") return;
    table.querySelectorAll(".metric-col").forEach((cell) => {
      const key = cell.getAttribute("data-metric");
      if (!key) return;
      const show = key === pinnedMetric || key === metric;
      if (show) cell.removeAttribute("hidden");
      else cell.setAttribute("hidden", "");
    });
  };

  const applyLimit = () => {
    const limit = Number(slider.value);
    rows.forEach((row) => {
      if (!isRankable(row)) {
        row.hidden = false;
        return;
      }
      const rank = Number(row.querySelector("[data-rank]")?.textContent || 0);
      row.hidden = rank > limit;
    });
    const rankedCount = rows.filter(isRankable).length;
    if (!rankedCount) {
      output.textContent = "—";
      return;
    }
    const shown = Math.min(limit, rankedCount);
    output.textContent = shown >= rankedCount ? `全部 ${rankedCount}` : `前 ${shown}`;
  };

  const sortRows = (metric) => {
    activeMetric = metric;
    const control = buttons.find((button) => button.dataset.sort === metric);
    const direction = control?.dataset.direction === "asc" ? "asc" : "desc";
    rows.sort((left, right) => {
      const leftRankable = isRankable(left);
      const rightRankable = isRankable(right);
      if (leftRankable !== rightRankable) return leftRankable ? -1 : 1;
      if (!leftRankable) return configOrder(left) - configOrder(right);

      const a = numericValue(left, metric);
      const b = numericValue(right, metric);
      if (a === null && b === null) return configOrder(left) - configOrder(right);
      if (a === null) return 1;
      if (b === null) return -1;
      if (a !== b) return direction === "asc" ? a - b : b - a;
      return configOrder(left) - configOrder(right);
    });

    let rankNumber = 0;
    rows.forEach((row) => {
      const rank = row.querySelector("[data-rank]");
      if (rank) {
        rank.textContent = isRankable(row)
          ? String(++rankNumber).padStart(2, "0")
          : "";
      }
      body.appendChild(row);
    });
    rows.forEach((row) => {
      const mark = row.querySelector("[data-tie-mark]");
      if (mark) mark.hidden = activeMetric !== pinnedMetric;
    });
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.sort === activeMetric));
    });
    setMetricColumns(activeMetric);
    applyLimit();
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => sortRows(button.dataset.sort || pinnedMetric));
  });
  slider.addEventListener("input", applyLimit);

  const rankedCount = rows.filter(isRankable).length;
  slider.max = String(Math.max(rankedCount, 1));
  slider.value = String(Math.max(rankedCount, 1));
  slider.disabled = rankedCount === 0;
  sortRows(activeMetric);
})();
