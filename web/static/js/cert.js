/* 证书管理页：调 GET /api/cert/status 渲染证书状态，续期走 POST /api/cert/renew（异步任务 + 轮询状态）。 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var renewTimer = null;
  var currentDomain = null;

  var STATUS_TEXT = {
    valid: "有效",
    expiring: "即将过期",
    expired: "已过期",
    none: "无证书",
  };

  var SOURCE_TEXT = {
    "acme.sh": "acme.sh",
    certbot: "certbot",
    lego: "lego",
    nginx: "nginx 配置",
  };

  function fmtHours(hours) {
    if (hours == null) return "—";
    if (hours < 0) return "已过期 " + Math.abs(hours) + " 小时";
    var days = Math.floor(hours / 24);
    if (days > 0) return days + " 天 " + (hours % 24) + " 小时";
    return hours + " 小时";
  }

  function loadCert() {
    document.getElementById("cert-loading").hidden = false;
    document.getElementById("cert-error").hidden = true;
    document.getElementById("cert-cards").hidden = true;
    MBot.api("/api/cert/status").then(function (res) {
      currentDomain = res.domain;
      document.getElementById("cert-acme").textContent = res.acmeEnabled ? "已安装" : "未安装";
      document.getElementById("cert-source").textContent =
        SOURCE_TEXT[res.source] || (res.source || "—");
      document.getElementById("cert-domain").textContent = res.domain || "—";
      document.getElementById("cert-issuer").textContent = res.issuer || "—";
      document.getElementById("cert-not-before").textContent =
        res.notBefore ? MBot.formatTime(res.notBefore) : "—";
      document.getElementById("cert-not-after").textContent =
        res.notAfter ? MBot.formatTime(res.notAfter) : "—";
      document.getElementById("cert-remaining").textContent = fmtHours(res.remainingHours);
      var statusEl = document.getElementById("cert-status");
      statusEl.textContent = STATUS_TEXT[res.status] || res.status || "—";
      statusEl.style.color = res.status === "valid" ? "var(--success, #22c55e)"
        : res.status === "expiring" ? "var(--warning, #eab308)"
        : res.status === "expired" ? "var(--danger)" : "inherit";
      var renewBtn = document.getElementById("btn-renew-cert");
      var canRenew = res.domain && res.source === "acme.sh" && res.acmeEnabled;
      renewBtn.disabled = !canRenew;
      var hint = "";
      if (!res.domain) hint = "没有可续期的证书。";
      else if (res.source === "certbot") hint = "该证书由 certbot 管理续期，面板不做续期。";
      else if (res.source === "lego") hint = "该证书由 lego 自动续期，面板不做续期。";
      else if (res.source === "nginx") hint = "该证书不在 acme.sh 管理下，面板无法续期。";
      else if (!res.acmeEnabled) hint = "acme.sh 未安装，无法续期。";
      document.getElementById("cert-renew-hint").textContent = hint;
      document.getElementById("cert-loading").hidden = true;
      document.getElementById("cert-cards").hidden = false;
    }).catch(function (err) {
      document.getElementById("cert-loading").hidden = true;
      document.getElementById("cert-error-text").textContent =
        "加载证书状态失败：" + (err.message || err);
      document.getElementById("cert-error").hidden = false;
    });
  }

  function pollRenew(taskId) {
    var statusEl = document.getElementById("cert-renew-status");
    var outputEl = document.getElementById("cert-renew-output");
    document.getElementById("cert-renew-card").hidden = false;
    if (renewTimer) clearTimeout(renewTimer);
    MBot.api("/api/cert/renew/" + encodeURIComponent(taskId)).then(function (res) {
      var task = res.task || {};
      statusEl.textContent = "状态：" + (task.status || "—") +
        (task.error ? "（" + task.error + "）" : "");
      outputEl.textContent = task.output || "";
      if (task.status === "running") {
        renewTimer = setTimeout(function () { pollRenew(taskId); }, 2000);
      } else if (task.status === "done") {
        MBot.toast("证书续期成功", "success");
        loadCert();
      } else {
        MBot.toast("证书续期失败：" + (task.error || "未知错误"), "error");
      }
    }).catch(function (err) {
      statusEl.textContent = "查询续期状态失败：" + (err.message || err);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadCert();
    document.getElementById("btn-refresh-cert").addEventListener("click", loadCert);
    document.getElementById("btn-renew-cert").addEventListener("click", async function () {
      if (!currentDomain) return;
      var ok = await MBot.confirmDialog(
        "续期证书",
        "确认对域名 <b>" + MBot.escapeHtml(currentDomain) + "</b> 执行 acme.sh 续期吗？",
        "确认续期", false);
      if (!ok) return;
      try {
        var res = await MBot.api("/api/cert/renew", {
          method: "POST",
          body: { domain: currentDomain, confirm: true },
        });
        MBot.toast("续期任务已提交，正在执行…", "info");
        pollRenew(res.taskId);
      } catch (err) {
        MBot.toast("提交续期失败：" + err.message, "error");
      }
    });
  });
})();
