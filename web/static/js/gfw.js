/* GFW 自动换 IP 页（菜单 16 的网页版）。
 * 数据：GET /api/gfw/status（Key 状态 + 已开启 + 上次检查）、
 * GET /api/gfw/instances?profile=（当前账号主区 RUNNING 实例）。
 * 操作：POST /api/gfw/key（保存 Key）、POST /api/gfw/toggle（开关）、
 * POST /api/gfw/check-now（后台立即检查，完成后发通知）。
 */
(function () {
  "use strict";

  var currentProfile = "DEFAULT";
  var statusData = null;
  var instances = [];

  var STATUS_MARK = {
    healthy: "✅ 正常",
    recovered: "🔁 已换 IP 恢复",
    exhausted: "⚠️ 换满仍被墙",
    unknown: "❓ 检测无结论",
    error: "❌ 换 IP 失败",
    skipped: "⏭️ 跳过"
  };

  function showError(message) {
    document.getElementById("gfw-loading").hidden = true;
    document.getElementById("gfw-body").hidden = true;
    var box = document.getElementById("gfw-error");
    document.getElementById("gfw-error-text").textContent = message;
    box.hidden = false;
  }

  function lastCheckText(instanceId) {
    if (!statusData || !statusData.last_check) return "—";
    var entry = statusData.last_check[instanceId];
    if (!entry) return "—";
    var mark = STATUS_MARK[entry.status] || entry.status || "—";
    var at = (entry.at || "").slice(0, 16).replace("T", " ");
    return mark + (at ? "（" + at + " UTC）" : "") +
      (entry.detail ? " " + entry.detail : "");
  }

  function render() {
    var statusEl = document.getElementById("gfw-key-status");
    if (statusData.configured) {
      statusEl.textContent = "✅ 已设置（" + statusData.key_masked + "）";
    } else {
      statusEl.textContent = "❌ 未设置";
    }
    var enabled = statusData.enabled || {};
    var enabledCount = Object.keys(enabled).length;
    document.getElementById("gfw-enabled-count").textContent =
      "（已开启 " + enabledCount + " 台，每 " +
      (statusData.interval_minutes || 30) + " 分钟检查一次）";
    document.getElementById("btn-gfw-check-now").disabled =
      !(statusData.configured && enabledCount > 0);

    var tbody = document.getElementById("gfw-tbody");
    if (!instances.length) {
      tbody.innerHTML = "<tr><td colspan=\"4\" style=\"color: var(--text-muted);\">" +
        "当前账号没有运行中的实例。</td></tr>";
      return;
    }
    var html = "";
    instances.forEach(function (inst) {
      var on = !!enabled[inst.id];
      html += "<tr>" +
        "<td>" + RBot.escapeHtml(inst["display-name"] || "?") + "</td>" +
        "<td>" + RBot.escapeHtml(inst["public-ip"] || "—") + "</td>" +
        "<td><button type=\"button\" class=\"btn\" data-gfw-toggle=\"" +
        RBot.escapeHtml(inst.id) + "\" data-enabled=\"" + (on ? "1" : "0") +
        "\">" + (on ? "✅ 已开启" : "☐ 未开启") + "</button></td>" +
        "<td>" + RBot.escapeHtml(lastCheckText(inst.id)) + "</td>" +
        "</tr>";
    });
    tbody.innerHTML = html;
    tbody.querySelectorAll("[data-gfw-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        toggleInstance(btn.getAttribute("data-gfw-toggle"),
                       btn.getAttribute("data-enabled") !== "1");
      });
    });
  }

  function loadAll(force) {
    document.getElementById("gfw-loading").hidden = false;
    document.getElementById("gfw-error").hidden = true;
    document.getElementById("gfw-body").hidden = true;
    Promise.all([
      RBot.api("/api/gfw/status"),
      RBot.cachedApi("gfw:instances:" + currentProfile,
        RBot.withProfile("/api/gfw/instances", currentProfile), !!force)
    ]).then(function (results) {
      statusData = results[0];
      instances = (results[1] && results[1].instances) || [];
      document.getElementById("gfw-loading").hidden = true;
      document.getElementById("gfw-body").hidden = false;
      render();
    }).catch(function (err) {
      showError("加载失败：" + (err && err.message ? err.message : err));
    });
  }

  function toggleInstance(instanceId, enable) {
    var inst = null;
    instances.forEach(function (item) {
      if (item.id === instanceId) inst = item;
    });
    RBot.api("/api/gfw/toggle", {
      method: "POST",
      body: JSON.stringify({
        instance_id: instanceId,
        enabled: enable,
        profile: currentProfile,
        name: inst ? inst["display-name"] : "",
        ip: inst ? inst["public-ip"] : ""
      })
    }).then(function () {
      RBot.toast(enable ? "已开启监控" : "已关闭监控", "success");
      loadAll(true);
    }).catch(function (err) {
      RBot.toast("切换失败：" + (err && err.message ? err.message : err), "error");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var profileSelect = document.getElementById("profile-select");
    RBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile || "DEFAULT";
      loadAll(false);
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      RBot.setProfile(currentProfile);
      loadAll(true);
    });
    document.getElementById("btn-refresh-gfw").addEventListener("click", function () {
      loadAll(true);
    });
    document.getElementById("btn-save-gfw-key").addEventListener("click", function () {
      var input = document.getElementById("gfw-key-input");
      var key = (input.value || "").trim();
      if (!key) {
        RBot.toast("请先粘贴 API Key", "error");
        return;
      }
      RBot.api("/api/gfw/key", {
        method: "POST",
        body: JSON.stringify({ key: key })
      }).then(function (res) {
        input.value = "";
        RBot.toast("API Key 已保存（" + res.key_masked + "）", "success");
        loadAll(true);
      }).catch(function (err) {
        RBot.toast("保存失败：" + (err && err.message ? err.message : err), "error");
      });
    });
    document.getElementById("btn-gfw-check-now").addEventListener("click", function () {
      RBot.api("/api/gfw/check-now", { method: "POST" }).then(function () {
        RBot.toast("已开始检查，完成后发通知", "success");
      }).catch(function (err) {
        RBot.toast("检查失败：" + (err && err.message ? err.message : err), "error");
      });
    });
  });
})();
