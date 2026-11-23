/* 配额页：调 GET /api/quota 渲染 A1 / E2 / 启动盘三项免费额度。 */
(function () {
  "use strict";

  function setBar(fillId, textId, used, limit, unit) {
    var fill = document.getElementById(fillId);
    var text = document.getElementById(textId);
    var pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    fill.style.width = pct + "%";
    fill.classList.toggle("full", pct >= 100);
    fill.classList.toggle("warn", pct >= 80 && pct < 100);
    text.textContent = used + " / " + limit + " " + unit +
      "（剩余 " + Math.max(0, limit - used) + " " + unit + "）";
  }

  function loadQuota() {
    document.getElementById("quota-loading").hidden = false;
    document.getElementById("quota-error").hidden = true;
    document.getElementById("quota-cards").hidden = true;
    RBot.api("/api/quota").then(function (res) {
      var s = res.summary;
      document.getElementById("quota-profile").textContent = res.profile || "—";
      document.getElementById("quota-overall").textContent = s.overall_ok
        ? "三项额度均未超限。"
        : "注意：有额度已超限，请检查！";
      setBar("a1-ocpu-fill", "a1-ocpu-text", s.a1.ocpu_used, s.a1.ocpu_limit, "核");
      setBar("a1-mem-fill", "a1-mem-text", s.a1.memory_used_gb, s.a1.memory_limit_gb, "GB");
      setBar("e2-fill", "e2-text", s.e2.count, s.e2.limit, "台");
      setBar("boot-fill", "boot-text", s.boot_volume.used_gb, s.boot_volume.limit_gb, "GB");
      document.getElementById("a1-instances").textContent =
        "实例：" + (s.a1.instances.join("、") || "无");
      document.getElementById("e2-instances").textContent =
        "实例：" + (s.e2.instances.join("、") || "无");
      document.getElementById("boot-volumes").textContent =
        "启动盘：" + (s.boot_volume.volumes.join("、") || "无");
      document.getElementById("quota-loading").hidden = true;
      document.getElementById("quota-cards").hidden = false;
      loadA1Health();
      loadA1Quota();
    }).catch(function (err) {
      document.getElementById("quota-loading").hidden = true;
      document.getElementById("quota-error-text").textContent =
        "加载配额失败：" + (err.message || err);
      document.getElementById("quota-error").hidden = false;
    });
  }

  // A1 配额（R-Bot 形状）：调 GET /api/quota/a1，超限时在 A1 卡片追加警告
  function loadA1Quota() {
    var el = document.getElementById("a1-over-warning");
    if (!el) return;
    RBot.api("/api/quota/a1").then(function (res) {
      var a1 = res.a1 || {};
      if (a1.over) {
        var parts = [];
        if (a1.overOcpu > 0) parts.push("OCPU 超 " + a1.overOcpu + " 核");
        if (a1.overMem > 0) parts.push("内存超 " + a1.overMem + " GB");
        el.textContent = "⚠️ A1 已超免费额度（" + parts.join("、") + "），超出的部分会按量计费！";
        el.hidden = false;
      } else {
        el.hidden = true;
      }
    }).catch(function () { el.hidden = true; });
  }

  // A1 配置体检：调 GET /api/quota/a1-health，按 severity 标色渲染建议
  var SEVERITY_MARK = { error: "❌", warning: "⚠️", info: "💡", ok: "✅" };
  var SEVERITY_COLOR = {
    error: "var(--danger)",
    warning: "var(--warning, #eab308)",
    info: "var(--text-muted)",
    ok: "var(--success, #22c55e)",
  };

  function loadA1Health() {
    var statusEl = document.getElementById("a1-health-status");
    var listEl = document.getElementById("a1-health-list");
    statusEl.textContent = "加载中…";
    listEl.innerHTML = "";
    RBot.api("/api/quota/a1-health").then(function (res) {
      var health = res.health || {};
      var suggestions = health.suggestions || [];
      statusEl.textContent = health.overall_ok
        ? "未发现超限问题。"
        : "发现超限问题，请检查下面建议。";
      listEl.innerHTML = suggestions.map(function (item) {
        var mark = SEVERITY_MARK[item.severity] || "•";
        var color = SEVERITY_COLOR[item.severity] || "inherit";
        return '<li style="color: ' + color + '; margin: 4px 0;">' +
          mark + " " + RBot.escapeHtml(item.message || "") + "</li>";
      }).join("");
    }).catch(function (err) {
      statusEl.textContent = "体检加载失败：" + (err.message || err);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadQuota();
    document.getElementById("btn-refresh-quota").addEventListener("click", loadQuota);
  });
})();
