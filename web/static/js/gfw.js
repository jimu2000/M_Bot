/* GFW 自动换 IP 页（菜单 16 的网页版）。
 * 数据：GET /api/gfw/status（Key 状态 + 已开启 + 上次检查）、
 * GET /api/gfw/instances?profile=&region=（当前账号当前区域的 RUNNING 实例）。
 * 操作：POST /api/gfw/key（保存 Key）、POST /api/gfw/toggle（开关）、
 * POST /api/gfw/check-now（后台立即检查，完成后发通知）。
 */
(function () {
  "use strict";

  var currentProfile = "DEFAULT";
  var currentRegion = null; // 当前工作区域，由 fillRegionSelect 恢复（多区账号才有二级下拉）
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
        "<td>" + MBot.escapeHtml(inst["display-name"] || "?") + "</td>" +
        "<td>" + MBot.escapeHtml(inst["public-ip"] || "—") + "</td>" +
        "<td><button type=\"button\" class=\"btn\" data-gfw-toggle=\"" +
        MBot.escapeHtml(inst.id) + "\" data-enabled=\"" + (on ? "1" : "0") +
        "\">" + (on ? "✅ 已开启" : "☐ 未开启") + "</button></td>" +
        "<td>" + MBot.escapeHtml(lastCheckText(inst.id)) + "</td>" +
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
      MBot.api("/api/gfw/status"),
      MBot.cachedApi("gfw:instances:" + currentProfile + ":" + (currentRegion || ""),
        MBot.withProfile("/api/gfw/instances", currentProfile, currentRegion), !!force)
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
    MBot.api("/api/gfw/toggle", {
      method: "POST",
      body: JSON.stringify({
        instance_id: instanceId,
        enabled: enable,
        profile: currentProfile,
        region: currentRegion,
        name: inst ? inst["display-name"] : "",
        ip: inst ? inst["public-ip"] : ""
      })
    }).then(function () {
      MBot.toast(enable ? "已开启监控" : "已关闭监控", "success");
      loadAll(true);
    }).catch(function (err) {
      MBot.toast("切换失败：" + (err && err.message ? err.message : err), "error");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var profileSelect = document.getElementById("profile-select");
    var regionSelect = document.getElementById("region-select");
    MBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile || "DEFAULT";
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadAll(false);
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      MBot.setProfile(currentProfile);
      // 切账号时区域重置为新账号的主区（与机器人端一致）。
      MBot.setRegion(null);
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadAll(true);
    });
    regionSelect.addEventListener("change", function (e) {
      currentRegion = e.target.value;
      MBot.setRegion(currentRegion);
      loadAll(true);
    });
    document.getElementById("btn-refresh-gfw").addEventListener("click", function () {
      loadAll(true);
    });
    document.getElementById("btn-save-gfw-key").addEventListener("click", function () {
      var input = document.getElementById("gfw-key-input");
      var key = (input.value || "").trim();
      if (!key) {
        MBot.toast("请先粘贴 API Key", "error");
        return;
      }
      MBot.api("/api/gfw/key", {
        method: "POST",
        body: JSON.stringify({ key: key })
      }).then(function (res) {
        input.value = "";
        MBot.toast("API Key 已保存（" + res.key_masked + "）", "success");
        loadAll(true);
      }).catch(function (err) {
        MBot.toast("保存失败：" + (err && err.message ? err.message : err), "error");
      });
    });
    document.getElementById("btn-gfw-check-now").addEventListener("click", function () {
      MBot.api("/api/gfw/check-now", { method: "POST" }).then(function () {
        MBot.toast("已开始检查，完成后发通知", "success");
      }).catch(function (err) {
        MBot.toast("检查失败：" + (err && err.message ? err.message : err), "error");
      });
    });
  });
})();
