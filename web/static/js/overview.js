/* 主机面板逻辑：加载主机卡片、搜索过滤、添加/编辑/删除主机 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var I18n = window.MBotI18n;
  // Translation lookup: fall back to the Chinese original when i18n.js is not loaded (every page includes i18n.js first). 翻译取词：i18n.js 未加载时回退中文原文（各页面都先引 i18n.js）。
  function tr(key, vars) {
    return I18n ? I18n.t(key, vars) : key;
  }

  function authBadge(authType) {
    if (authType === "key") return '<span class="badge badge-blue">' + tr("overview.auth_key") + "</span>";
    return '<span class="badge badge-green">' + tr("overview.auth_password") + "</span>";
  }

  var hosts = [];
  var filterText = "";

  function hostCard(h) {
    var esc = MBot.escapeHtml;
    var tags = Array.isArray(h.tags) ? h.tags : [];
    var tagsHtml = tags.map(function (t) {
      return '<span class="tag-chip">' + esc(t) + "</span>";
    }).join("");
    var addr = h.host + (h.port && h.port !== 22 ? ":" + h.port : "");
    var lastUsed = h.last_used ? MBot.timeAgo(h.last_used) : tr("overview.never_used");
    var online = h.last_used && (Date.now() - new Date(h.last_used).getTime() < 24 * 3600 * 1000);
    // Platform info row: the backend has no platform field, so show the notes or a key-path hint. 平台信息行：后端没有 platform 字段，显示备注（notes）或密钥路径提示。
    var platform = h.notes || h.platform || "";
    return (
      '<article class="card host-card" data-host-id="' + esc(h.id) + '">' +
        '<div class="host-card-head">' +
          '<span class="host-card-name">' + esc(h.name) + "</span>" +
          authBadge(h.auth_type) +
        "</div>" +
        (platform ? '<div class="host-platform">' + esc(platform) + "</div>" : "") +
        (tagsHtml ? '<div>' + tagsHtml + "</div>" : "") +
        '<div class="host-meta">🖥️ <span class="mono">' + esc(addr) + "</span>" +
          '<button type="button" class="copy-btn" data-copy="' + esc(addr) + '" title="' + tr("overview.copy_addr") + '">' + tr("overview.copy") + "</button></div>" +
        '<div class="host-meta">👤 <span>' + esc(h.username || "") + "</span></div>" +
        '<div class="host-card-foot">' +
          '<span class="last-used"><span class="dot' + (online ? "" : " off") + '"></span>' + esc(lastUsed) + "</span>" +
          '<span style="flex:1"></span>' +
          '<button type="button" class="btn btn-sm btn-primary" data-act="connect">' + tr("overview.connect") + "</button>" +
          '<button type="button" class="btn btn-sm" data-act="edit">' + tr("overview.edit") + "</button>" +
          '<button type="button" class="btn btn-sm btn-ghost" data-act="delete">' + tr("overview.delete") + "</button>" +
        "</div>" +
      "</article>"
    );
  }

  function render() {
    var grid = document.getElementById("host-grid");
    var empty = document.getElementById("host-empty");
    var countEl = document.getElementById("host-count");
    var q = filterText.trim().toLowerCase();
    var filtered = hosts.filter(function (h) {
      if (!q) return true;
      return (h.name || "").toLowerCase().includes(q) ||
             (h.host || "").toLowerCase().includes(q);
    });
    countEl.textContent = hosts.length;
    grid.innerHTML = filtered.map(hostCard).join("");
    empty.hidden = hosts.length !== 0;
    grid.hidden = filtered.length === 0 && hosts.length === 0;
    if (hosts.length > 0 && filtered.length === 0) {
      grid.innerHTML = '<p style="color: var(--text-muted);">' + tr("overview.no_match") + "</p>";
    }
  }

  async function loadHosts() {
    var errBox = document.getElementById("host-error");
    errBox.hidden = true;
    try {
      var data = await MBot.api("/api/hosts");
      hosts = data.hosts || [];
      render();
    } catch (err) {
      document.getElementById("host-error-text").textContent = tr("overview.load_hosts_failed") + err.message;
      errBox.hidden = false;
    }
  }

  function openHostModal(host) {
    var isEdit = !!host;
    document.getElementById("host-modal-title").textContent = isEdit ? tr("overview.edit_host_title") : tr("overview.add_host_title");
    document.getElementById("host-id").value = host ? host.id : "";
    document.getElementById("host-name").value = host ? host.name : "";
    document.getElementById("host-addr").value = host ? host.host : "";
    document.getElementById("host-port").value = host ? host.port || 22 : 22;
    document.getElementById("host-username").value = host ? host.username : "";
    document.getElementById("host-auth-type").value = host ? host.auth_type || "password" : "password";
    document.getElementById("host-key-path").value = host ? host.key_path || "" : "";
    document.getElementById("host-notes").value = host ? host.notes || "" : "";
    document.getElementById("host-tags").value = host && Array.isArray(host.tags) ? host.tags.join(", ") : "";
    toggleAuthFields();
    MBot.openModal("host-modal");
  }

  function toggleAuthFields() {
    var type = document.getElementById("host-auth-type").value;
    document.getElementById("host-password-group").hidden = type !== "password";
    document.getElementById("host-key-group").hidden = type !== "key";
  }

  async function saveHost(e) {
    e.preventDefault();
    var id = document.getElementById("host-id").value;
    var authType = document.getElementById("host-auth-type").value;
    // Backend security rule: passwords are never stored in files (a password in the request is discarded), and key auth requires key_path (the private key file path on the panel server). 后端安全规则：密码绝不存文件（请求里带 password 也会被丢弃），密钥认证必须提供 key_path（面板服务器上的私钥文件路径）。
    var payload = {
      name: document.getElementById("host-name").value.trim(),
      host: document.getElementById("host-addr").value.trim(),
      port: parseInt(document.getElementById("host-port").value, 10) || 22,
      username: document.getElementById("host-username").value.trim(),
      auth_type: authType,
      notes: document.getElementById("host-notes").value.trim(),
      tags: document.getElementById("host-tags").value.split(",").map(function (s) { return s.trim(); }).filter(Boolean),
    };
    if (authType === "key") {
      payload.key_path = document.getElementById("host-key-path").value.trim();
      if (!payload.key_path) {
        MBot.toast("密钥认证必须填写密钥文件路径", "error");
        return;
      }
    }
    var btn = document.getElementById("host-save-btn");
    btn.disabled = true;
    try {
      if (id) {
        await MBot.api("/api/hosts/" + encodeURIComponent(id), { method: "PUT", body: payload });
        MBot.toast("主机已更新", "success");
      } else {
        await MBot.api("/api/hosts", { method: "POST", body: payload });
        MBot.toast("主机已添加", "success");
      }
      MBot.closeModal("host-modal");
      await loadHosts();
    } catch (err) {
      MBot.toast("保存失败：" + err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteHost(id, name) {
    var ok = await MBot.confirmDialog(
      "删除主机",
      "<p>确定要删除主机 <b>" + MBot.escapeHtml(name) + "</b> 吗？只删除本面板的保存记录，不影响服务器本身。</p>",
      "确认删除", true
    );
    if (!ok) return;
    try {
      await MBot.api("/api/hosts/" + encodeURIComponent(id), { method: "DELETE" });
      MBot.toast("主机已删除", "success");
      await loadHosts();
    } catch (err) {
      MBot.toast("删除失败：" + err.message, "error");
    }
  }

  // Cloud report card: call GET /api/cloud-report/summary and render the summary per account. 云报告卡片：调 GET /api/cloud-report/summary，按账号渲染汇总。
  // Goes through the frontend cache (force=true skips the frontend cache and sends refresh=1 to bypass the server cache). 走前端缓存（force=true 时跳过前端缓存并带 refresh=1 绕过服务端缓存）。
  // The backend summarizes each account in parallel + a 60s server cache, so the first load costs about one account's time. 后端各账号并行汇总 + 60 秒服务端缓存，首次约一个账号的耗时。
  // Instance states in Chinese: RUNNING -> 运行中、TERMINATED -> 已终止、STOPPED -> 已停止、PROVISIONING/CREATING -> 创建中、STARTING -> 启动中、STOPPING -> 停止中、TERMINATING -> 终止中; unknown states fall back to the original text; English mode shows the OCI original. Only the display text changes; the data from /api/cloud-report/summary is untouched. 实例状态中文显示：RUNNING→运行中、TERMINATED→已终止、STOPPED→已停止、PROVISIONING/CREATING→创建中、STARTING→启动中、STOPPING→停止中、TERMINATING→终止中，未知状态兜底显示原文；英文模式下显示 OCI 原文。只改显示文字，数据与 /api/cloud-report/summary 不变。
  // The Chinese state map lives in i18n.js as INSTANCE_STATE_ZH; display goes through MBotI18n.instanceStateLabel() (returns the original in English mode). 实例状态中文映射表在 i18n.js 的 INSTANCE_STATE_ZH，显示走 MBotI18n.instanceStateLabel()（英文模式返回原文）。
  function cloudStateLabel(state) {
    return I18n ? I18n.instanceStateLabel(state) : (state || "");
  }

  function loadCloudReport(force) {
    var loadingEl = document.getElementById("cloud-report-loading");
    var bodyEl = document.getElementById("cloud-report-body");
    var genEl = document.getElementById("cloud-report-generated");
    if (!loadingEl || !bodyEl) return;
    var url = "/api/cloud-report/summary";
    if (force) url += "?refresh=1";
    MBot.cachedApi("cloudreport", url, !!force).then(function (res) {
      var groups = res.groups || [];
      if (groups.length === 0) {
        loadingEl.textContent = tr("overview.no_account");
        return;
      }
      var esc = MBot.escapeHtml;
      // Cloud report as a table: one row per region, columns covering all the info; .table-wrap scrolls horizontally so phones never overflow or squeeze together. 云报告改表格：每区域一行，列覆盖全部信息；.table-wrap 横向滚动，手机上不溢出、不挤成一团。
      // Columns: region (display name + region code) / instances (total, RUNNING / TERMINATED split) / boot volumes (count / GB) / A1 (OCPUs used/total, memory used/total) / E2 (used/total). An account whose query failed still takes a row, with the error spanning the other 4 columns (查询失败的账号占一行）. 列：区域（中文名 + region 代码）/ 实例（总数，RUNNING / TERMINATED 分开）/ 启动盘（块数 / GB）/ A1（核 已用/总量、内存 已用/总量）/ E2（已用/总量）。查询失败的账号占一行，错误信息横跨其余 4 列。
      var rows = groups.map(function (g) {
        var label = g.profileLabel || g.profile;
        var regionCell = "<b>" + esc(label) + "</b>" +
          (g.region ? '<br><span class="mono" style="font-size: 0.85em; color: var(--text-muted);">' + esc(g.region) + "</span>" : "");
        if (g.error) {
          return "<tr><td>" + regionCell + '</td><td colspan="4" style="color: var(--danger);">' + tr("overview.query_failed") + esc(g.error) + "</td></tr>";
        }
        var byState = g.instances.byState || {};
        var stateText = cloudStateLabel("RUNNING") + " " + (byState.RUNNING || 0) +
          "、" + cloudStateLabel("TERMINATED") + " " + (byState.TERMINATED || 0);
        Object.keys(byState).forEach(function (s) {
          if (s !== "RUNNING" && s !== "TERMINATED") stateText += "、" + cloudStateLabel(s) + " " + byState[s];
        });
        var a1 = (g.quota && g.quota.a1) || {};
        var e2 = (g.quota && g.quota.e2) || {};
        return "<tr>" +
          "<td>" + regionCell + "</td>" +
          '<td>' + g.instances.total + " " + tr("cloud.unit_instances") + '<br><span style="font-size: 0.85em; color: var(--text-muted);">' + esc(stateText) + "</span></td>" +
          '<td>' + g.volumes.count + " " + tr("cloud.unit_volumes") + '<br><span style="font-size: 0.85em; color: var(--text-muted);">' + g.volumes.totalGB + " GB</span></td>" +
          '<td>' + a1.usedOcpu + "/" + a1.capOcpu + " " + tr("cloud.unit_cores") + '<br><span style="font-size: 0.85em; color: var(--text-muted);">' + a1.usedMem + "/" + a1.capMem + " GB</span></td>" +
          "<td>" + e2.count + "/" + e2.limit + " " + tr("cloud.unit_instances") + "</td>" +
        "</tr>";
      }).join("");
      bodyEl.innerHTML = '<div class="table-wrap"><table class="data cloud-report-table">' +
        "<thead><tr><th>" + tr("cloud.th_region") + "</th><th>" + tr("cloud.th_instances") + "</th><th>" + tr("cloud.th_volumes") + "</th><th>" + tr("cloud.th_a1") + "</th><th>" + tr("cloud.th_e2") + "</th></tr></thead>" +
        "<tbody>" + rows + "</tbody></table></div>";
      loadingEl.hidden = true;
      bodyEl.hidden = false;
      if (res.generatedAtEpochSec) {
        genEl.textContent = tr("overview.generated") + MBot.formatTime(res.generatedAtEpochSec * 1000);
        genEl.hidden = false;
      }
    }).catch(function (err) {
      loadingEl.textContent = tr("overview.load_failed") + (err.message || err);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadHosts();
    loadCloudReport();
    // Re-render dynamic text after a language switch (host cards + cloud report table; the cloud report goes through the frontend cache, so no extra backend request is sent). 语言切换后重渲染动态文案（主机卡片 + 云报告表格；云报告走前端缓存，不会多发一次后端请求）。
    window.addEventListener("mbot-lang-change", function () {
      render();
      loadCloudReport();
    });
    document.getElementById("btn-refresh-cloud-report").addEventListener("click", function () { loadCloudReport(true); });

    document.getElementById("host-search").addEventListener("input", function (e) {
      filterText = e.target.value;
      render();
    });
    document.getElementById("btn-add-host").addEventListener("click", function () { openHostModal(null); });
    document.getElementById("btn-add-host-empty").addEventListener("click", function () { openHostModal(null); });
    document.getElementById("btn-retry-hosts").addEventListener("click", loadHosts);
    document.getElementById("host-auth-type").addEventListener("change", toggleAuthFields);
    document.getElementById("host-form").addEventListener("submit", saveHost);

    document.getElementById("host-grid").addEventListener("click", function (e) {
      var copyBtn = e.target.closest("[data-copy]");
      if (copyBtn) {
        MBot.copyText(copyBtn.getAttribute("data-copy"), "地址");
        return;
      }
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var card = btn.closest("[data-host-id]");
      var id = card.getAttribute("data-host-id");
      var host = hosts.find(function (h) { return String(h.id) === id; });
      if (!host) return;
      var act = btn.getAttribute("data-act");
      if (act === "connect") {
        window.location.href = "/terminal?host_id=" + encodeURIComponent(id);
      } else if (act === "edit") {
        openHostModal(host);
      } else if (act === "delete") {
        deleteHost(id, host.name);
      }
    });
  });
})();
