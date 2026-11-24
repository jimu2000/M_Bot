/* 主机面板逻辑：加载主机卡片、搜索过滤、添加/编辑/删除主机 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var hosts = []; // 全部主机缓存
  var filterText = "";

  function authBadge(authType) {
    if (authType === "key") return '<span class="badge badge-blue">密钥</span>';
    return '<span class="badge badge-green">密码</span>';
  }

  // 渲染一张主机卡片
  function hostCard(h) {
    var esc = MBot.escapeHtml;
    var tags = Array.isArray(h.tags) ? h.tags : [];
    var tagsHtml = tags.map(function (t) {
      return '<span class="tag-chip">' + esc(t) + "</span>";
    }).join("");
    var addr = h.host + (h.port && h.port !== 22 ? ":" + h.port : "");
    var lastUsed = h.last_used ? MBot.timeAgo(h.last_used) : "从未使用";
    var online = h.last_used && (Date.now() - new Date(h.last_used).getTime() < 24 * 3600 * 1000);
    // 平台信息行：后端没有 platform 字段，显示备注（notes）或密钥路径提示
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
          '<button type="button" class="copy-btn" data-copy="' + esc(addr) + '" title="复制地址">⧉ 复制</button></div>' +
        '<div class="host-meta">👤 <span>' + esc(h.username || "") + "</span></div>" +
        '<div class="host-card-foot">' +
          '<span class="last-used"><span class="dot' + (online ? "" : " off") + '"></span>' + esc(lastUsed) + "</span>" +
          '<span style="flex:1"></span>' +
          '<button type="button" class="btn btn-sm btn-primary" data-act="connect">▶ 连接</button>' +
          '<button type="button" class="btn btn-sm" data-act="edit">编辑</button>' +
          '<button type="button" class="btn btn-sm btn-ghost" data-act="delete">删除</button>' +
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
      grid.innerHTML = '<p style="color: var(--text-muted);">没有匹配的主机，换个关键词试试。</p>';
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
      document.getElementById("host-error-text").textContent = "加载主机列表失败：" + err.message;
      errBox.hidden = false;
    }
  }

  // 添加 / 编辑弹窗
  function openHostModal(host) {
    var isEdit = !!host;
    document.getElementById("host-modal-title").textContent = isEdit ? "编辑主机" : "添加主机";
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
    // 后端安全规则：密码绝不存文件（请求里带 password 也会被丢弃），
    // 密钥认证必须提供 key_path（面板服务器上的私钥文件路径）。
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

  // 云报告卡片：调 GET /api/cloud-report/summary，按账号渲染汇总。
  // 走前端缓存（force=true 时跳过前端缓存并带 refresh=1 绕过服务端缓存）。
  // 后端各账号并行汇总 + 60 秒服务端缓存，首次约一个账号的耗时。
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
        loadingEl.textContent = "暂无已配置账号。";
        return;
      }
      var esc = MBot.escapeHtml;
      bodyEl.innerHTML = groups.map(function (g) {
        // 显示名优先（后端 v1.15.0 起带 profileLabel），旧缓存没有时回落配置键名。
        var label = g.profileLabel || g.profile;
        if (g.error) {
          return '<div style="margin: 8px 0;"><b>' + esc(label) + '</b>：' +
            '<span style="color: var(--danger);">查询失败：' + esc(g.error) + "</span></div>";
        }
        var byState = g.instances.byState || {};
        var states = Object.keys(byState).map(function (s) {
          return s + " " + byState[s];
        }).join("、") || "无实例";
        var a1 = (g.quota && g.quota.a1) || {};
        var e2 = (g.quota && g.quota.e2) || {};
        return '<div style="margin: 8px 0;"><b>' + esc(label) + "</b>" +
          (g.region ? "（" + esc(g.region) + "）" : "") + "：" +
          "实例 " + g.instances.total + " 台（" + esc(states) + "）；" +
          "启动盘 " + g.volumes.count + " 块 / " + g.volumes.totalGB + " GB；" +
          "A1 " + a1.usedOcpu + "/" + a1.capOcpu + " 核、" +
          a1.usedMem + "/" + a1.capMem + " GB；" +
          "E2 " + e2.count + "/" + e2.limit + " 台</div>";
      }).join("");
      loadingEl.hidden = true;
      bodyEl.hidden = false;
      if (res.generatedAtEpochSec) {
        genEl.textContent = "生成于 " + MBot.formatTime(res.generatedAtEpochSec * 1000);
        genEl.hidden = false;
      }
    }).catch(function (err) {
      loadingEl.textContent = "云报告加载失败：" + (err.message || err);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadHosts();
    loadCloudReport();
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

    // 卡片内按钮：事件委托
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
