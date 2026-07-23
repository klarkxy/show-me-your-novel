(() => {
  "use strict";

  const body = document.querySelector("#leaderboard-body");
  const buttons = Array.from(document.querySelectorAll("[data-sort]"));
  const slider = document.querySelector("#rank-limit");
  const output = document.querySelector("#rank-limit-output");

  if (!body || !slider || !output) return;

  const rows = Array.from(body.querySelectorAll("tr"));
  const metricDirection = {
    average: "desc",
    sol: "desc",
    fable: "desc",
    kimi: "desc",
    "ai-flavor": "asc",
  };
  let activeMetric = "average";

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
    output.textContent = rankedCount
      ? `显示前 ${Math.min(limit, rankedCount)} 名；未完成始终显示`
      : "暂无可排名作品；未完成始终显示";
  };

  const sortRows = (metric) => {
    activeMetric = metric;
    const direction = metricDirection[metric] || "desc";
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
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.sort === activeMetric));
    });
    applyLimit();
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => sortRows(button.dataset.sort || "average"));
  });
  slider.addEventListener("input", applyLimit);

  const rankedCount = rows.filter(isRankable).length;
  slider.max = String(Math.max(rankedCount, 1));
  slider.value = String(Math.max(rankedCount, 1));
  slider.disabled = rankedCount === 0;
  sortRows(activeMetric);
})();
