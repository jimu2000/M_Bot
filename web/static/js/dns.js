/* DNS 管理页：Zone 选择、记录表格、添加/编辑/删除、代理状态切换。
 * 记录内容一律用 textContent 渲染（防 XSS），错误用 MBot.toast 提示。 */
(function () {
  "use strict";
  var MBot = window.MBot;

  var zones = [];
  var records = [];
  var currentZoneId = "";

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function ttlText(ttl) {
    return ttl === 1 ? "自动" : String(ttl);
  }

  /* ---------- Zone 下拉 ---------- */

  function renderZones() {
    var select = document.getElementById("zone-select");
    select.innerHTML = "";
    if (zones.length === 0) {
      select.appendChild(el("option", "暂无 Zone", null));
      select.options[0].value = "";
      currentZoneId = "";
      renderRecords();
      return;
    }
    zones.forEach(function (zone) {
      var opt = document.createElement("option");
      opt.value = zone.id;
      opt.textContent = zone.name + (zone.status === "active" ? "" : "（" + (zone.status || "未知") + "）");
      select.appendChild(opt);
    });
    // 保持上次选中的 Zone（刷新后不跳回第一个）
    if (currentZoneId && zones.some(function (z) { return z.id === currentZoneId; })) {
      select.value = currentZoneId;
    } else {
      currentZoneId = select.value;
    }
    loadRecords();
  }

  function loadZones() {
    MBot.api("/api/dns/zones").then(function (res) {
      zones = res.zones || [];
      renderZones();
    }).catch(function (err) {
      MBot.toast("加载 Zone 失败：" + (err.message || err), "error");
    });
  }

  /* ---------- 记录表格 ---------- */

  function renderRecords() {
    var tbody = document.getElementById("dns-tbody");
    var empty = document.getElementById("dns-empty");
    tbody.innerHTML = "";
    empty.hidden = records.length !== 0;
    if (records.length === 0) {
      var row = document.createElement("tr");
      var cell = el("td", currentZoneId ? "该 Zone 下没有符合条件的记录" : "请先选择 Zone", null);
      cell.colSpan = 6;
      cell.style.textAlign = "center";
      cell.style.color = "var(--text-muted)";
      row.appendChild(cell);
      tbody.appendChild(row);
      return;
    }
    records.forEach(function (rec) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", rec.type || "—", "mono"));

      var nameCell = document.createElement("td");
      nameCell.appendChild(el("b", rec.name || "—"));
      tr.appendChild(nameCell);

      // 内容用 textContent 渲染，不走 innerHTML，防 XSS。
      tr.appendChild(el("td", rec.content || "—", "mono"));

      var proxiedCell = document.createElement("td");
      var proxiedBtn = document.createElement("button");
      proxiedBtn.type = "button";
      proxiedBtn.className = "btn btn-sm " + (rec.proxied ? "proxied-on" : "proxied-off");
      proxiedBtn.textContent = rec.proxied ? "🟠 已代理" : "⚪ 仅 DNS";
      proxiedBtn.title = "点击切换代理状态";
      proxiedBtn.addEventListener("click", function () { toggleProxied(rec); });
      proxiedCell.appendChild(proxiedBtn);
      tr.appendChild(proxiedCell);

      tr.appendChild(el("td", ttlText(rec.ttl)));

      var actCell = document.createElement("td");
      var actions = document.createElement("div");
      actions.className = "row-actions";
      var editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn btn-sm";
      editBtn.textContent = "编辑";
      editBtn.addEventListener("click", function () { openForm(rec); });
      var delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-sm btn-danger";
      delBtn.textContent = "删除";
      delBtn.addEventListener("click", function () { deleteRecord(rec); });
      actions.appendChild(editBtn);
      actions.appendChild(delBtn);
      actCell.appendChild(actions);
      tr.appendChild(actCell);

      tbody.appendChild(tr);
    });
  }

  function loadRecords() {
    if (!currentZoneId) {
      records = [];
      renderRecords();
      return;
    }
    var type = document.getElementById("filter-type").value;
    var name = document.getElementById("filter-name").value.trim();
    var url = "/api/dns/zones/" + encodeURIComponent(currentZoneId) + "/records";
    var params = [];
    if (type) params.push("type=" + encodeURIComponent(type));
    if (name) params.push("name=" + encodeURIComponent(name));
    if (params.length) url += "?" + params.join("&");
    MBot.api(url).then(function (res) {
      records = res.records || [];
      renderRecords();
    }).catch(function (err) {
      MBot.toast("加载记录失败：" + (err.message || err), "error");
    });
  }

  /* ---------- 添加 / 编辑表单 ---------- */

  function openForm(rec) {
    document.getElementById("record-id").value = rec ? rec.id : "";
    document.getElementById("record-modal-title").textContent =
      rec ? "编辑记录" : "添加记录";
    document.getElementById("record-type").value = rec ? rec.type : "A";
    document.getElementById("record-name").value = rec ? rec.name : "";
    document.getElementById("record-content").value = rec ? rec.content : "";
    document.getElementById("record-ttl").value = rec && rec.ttl != null ? rec.ttl : 1;
    document.getElementById("record-proxied").checked = !!(rec && rec.proxied);
    MBot.openModal("record-modal");
  }

  function saveRecord() {
    if (!currentZoneId) {
      MBot.toast("请先选择 Zone", "error");
      return;
    }
    var recordId = document.getElementById("record-id").value;
    var body = {
      type: document.getElementById("record-type").value,
      name: document.getElementById("record-name").value.trim(),
      content: document.getElementById("record-content").value.trim(),
      proxied: document.getElementById("record-proxied").checked,
      ttl: parseInt(document.getElementById("record-ttl").value, 10) || 1,
    };
    if (!body.name || !body.content) {
      MBot.toast("名称和内容不能为空", "error");
      return;
    }
    var url = "/api/dns/zones/" + encodeURIComponent(currentZoneId) + "/records";
    var method = "POST";
    if (recordId) {
      url += "/" + encodeURIComponent(recordId);
      method = "PUT";
    }
    MBot.api(url, { method: method, body: body }).then(function () {
      MBot.closeModal("record-modal");
      MBot.toast(recordId ? "记录已更新" : "记录已添加", "success");
      loadRecords();
    }).catch(function (err) {
      MBot.toast("保存失败：" + (err.message || err), "error");
    });
  }

  /* ---------- 删除 / 代理切换 ---------- */

  function deleteRecord(rec) {
    MBot.confirmDialog(
      "删除 DNS 记录",
      "<p>确定删除记录 <b>" + MBot.escapeHtml(rec.type + " " + rec.name) +
        "</b> 吗？此操作不可撤销。</p>",
      "确认删除", true
    ).then(function (ok) {
      if (!ok) return;
      var url = "/api/dns/zones/" + encodeURIComponent(currentZoneId) +
        "/records/" + encodeURIComponent(rec.id);
      return MBot.api(url, { method: "DELETE" }).then(function () {
        MBot.toast("记录已删除", "success");
        loadRecords();
      }).catch(function (err) {
        MBot.toast("删除失败：" + (err.message || err), "error");
      });
    });
  }

  function toggleProxied(rec) {
    var url = "/api/dns/zones/" + encodeURIComponent(currentZoneId) +
      "/records/" + encodeURIComponent(rec.id) + "/proxied";
    MBot.api(url, { method: "POST", body: { proxied: !rec.proxied } })
      .then(function () {
        MBot.toast(rec.proxied ? "已关闭代理" : "已开启代理", "success");
        loadRecords();
      }).catch(function (err) {
        MBot.toast("切换代理状态失败：" + (err.message || err), "error");
      });
  }

  /* ---------- Cloudflare 凭据（v1.26.0） ---------- */

  var cfProfile = "";
  var cfRegion = "";

  function renderCfStatus(res) {
    var status = document.getElementById("cf-status");
    var delBtn = document.getElementById("btn-cf-delete");
    if (!res.configured) {
      status.textContent = "未配置：请手动添加，或从服务器提取。";
      delBtn.hidden = true;
      return;
    }
    var where = res.source === "file" ? "面板/机器人里保存的" : "服务器环境变量里的";
    var mode = res.auth_mode === "token" ? "API Token" : "Global API Key";
    status.textContent = "已配置（" + where + "，" + mode +
      (res.email_masked ? "，" + res.email_masked : "") + "）。";
    delBtn.hidden = res.source !== "file";
  }

  function loadCfStatus() {
    MBot.api("/api/dns/credentials").then(renderCfStatus).catch(function (err) {
      document.getElementById("cf-status").textContent =
        "状态加载失败：" + (err.message || err);
    });
  }

  function loadCfInstances() {
    var select = document.getElementById("cf-extract-instance");
    MBot.api("/api/instances").then(function (res) {
      cfProfile = res.profile || "";
      cfRegion = res.region || "";
      var list = (res.instances || []).filter(function (inst) { return inst.publicIp; });
      select.innerHTML = "";
      if (list.length === 0) {
        select.appendChild(el("option", "暂无运行中且有公网 IP 的实例", null));
        select.options[0].value = "";
        return;
      }
      list.forEach(function (inst) {
        var opt = document.createElement("option");
        opt.value = inst.id;
        opt.textContent = (inst.displayName || inst.id) + "（" + inst.publicIp + "）";
        select.appendChild(opt);
      });
    }).catch(function () {
      select.innerHTML = "";
      select.appendChild(el("option", "实例加载失败", null));
      select.options[0].value = "";
    });
  }

  function toggleCfMode() {
    var mode = document.getElementById("cf-mode").value;
    document.getElementById("cf-token-wrap").hidden = mode !== "token";
    document.getElementById("cf-key-wrap").hidden = mode !== "key";
    document.getElementById("cf-email-wrap").hidden = mode !== "key";
  }

  function saveCfCredentials() {
    var mode = document.getElementById("cf-mode").value;
    var body = { mode: mode };
    if (mode === "token") {
      body.api_token = document.getElementById("cf-token").value.trim();
      if (!body.api_token) { MBot.toast("请先输入 Token", "error"); return; }
    } else {
      body.api_key = document.getElementById("cf-key").value.trim();
      body.email = document.getElementById("cf-email").value.trim();
      if (!body.api_key || !body.email) { MBot.toast("请先输入 Key 与邮箱", "error"); return; }
    }
    MBot.api("/api/dns/credentials", { method: "POST", body: body }).then(function (res) {
      document.getElementById("cf-token").value = "";
      document.getElementById("cf-key").value = "";
      document.getElementById("cf-email").value = "";
      MBot.toast("凭据已保存并校验通过（" + (res.zones || 0) + " 个 Zone）", "success");
      loadCfStatus();
      loadZones();
    }).catch(function (err) {
      MBot.toast("保存失败：" + (err.message || err), "error");
    });
  }

  function extractCfCredentials() {
    var instanceId = document.getElementById("cf-extract-instance").value;
    if (!instanceId) { MBot.toast("请先选择一台服务器", "error"); return; }
    MBot.api("/api/dns/credentials/extract", {
      method: "POST",
      body: { instance_id: instanceId, profile: cfProfile || null, region: cfRegion || null }
    }).then(function (res) {
      MBot.toast("已提取并保存（" + (res.zones || 0) + " 个 Zone" +
        (res.email_masked ? "，" + res.email_masked : "") + "）", "success");
      loadCfStatus();
      loadZones();
    }).catch(function (err) {
      MBot.toast("提取失败：" + (err.message || err), "error");
    });
  }

  function deleteCfCredentials() {
    if (!window.confirm("确认删除面板/机器人里保存的 Cloudflare 凭据？\n（只删保存的文件，不动环境变量。）")) return;
    MBot.api("/api/dns/credentials", { method: "DELETE" }).then(function () {
      MBot.toast("已保存的凭据已删除", "success");
      loadCfStatus();
      loadZones();
    }).catch(function (err) {
      MBot.toast("删除失败：" + (err.message || err), "error");
    });
  }

  /* ---------- 初始化 ---------- */

  document.addEventListener("DOMContentLoaded", function () {
    loadZones();
    loadCfStatus();
    loadCfInstances();
    document.getElementById("cf-mode").addEventListener("change", toggleCfMode);
    document.getElementById("btn-cf-save").addEventListener("click", saveCfCredentials);
    document.getElementById("btn-cf-extract").addEventListener("click", extractCfCredentials);
    document.getElementById("btn-cf-delete").addEventListener("click", deleteCfCredentials);
    document.getElementById("zone-select").addEventListener("change", function (e) {
      currentZoneId = e.target.value;
      loadRecords();
    });
    document.getElementById("btn-refresh-dns").addEventListener("click", function () {
      loadZones();
    });
    document.getElementById("btn-filter").addEventListener("click", loadRecords);
    document.getElementById("filter-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") loadRecords();
    });
    document.getElementById("btn-add-record").addEventListener("click", function () {
      openForm(null);
    });
    document.getElementById("btn-add-record-empty").addEventListener("click", function () {
      openForm(null);
    });
    document.getElementById("btn-save-record").addEventListener("click", saveRecord);
  });
})();
