/* 云账户页：已配置账号列表、添加租户（解析 config、上传 pem、确认添加）。
 * 服务端返回内容一律用 textContent 渲染（防 XSS），错误用 MBot.toast 提示。 */
(function () {
  "use strict";
  var MBot = window.MBot;

  var pendingId = "";
  var pendingProfiles = [];

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function apiError(data, fallback) {
    return (data && data.error) || fallback || "请求失败";
  }

  /* ---------- 已配置账号 ---------- */

  async function loadAccounts() {
    var box = document.getElementById("accounts-list");
    box.textContent = "加载中…";
    try {
      var res = await fetch("/api/profiles", { credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "加载账号失败"));
      box.innerHTML = "";
      if (!data.profiles || data.profiles.length === 0) {
        box.textContent = "暂无已配置账号。";
        return;
      }
      var table = el("table", null, "table");
      var head = document.createElement("thead");
      var headRow = document.createElement("tr");
      ["账号", "主区域", "订阅区域数", "显示名"].forEach(function (h) {
        headRow.appendChild(el("th", h, null));
      });
      head.appendChild(headRow);
      table.appendChild(head);
      var body = document.createElement("tbody");
      data.profiles.forEach(function (p) {
        var row = document.createElement("tr");
        row.appendChild(el("td", p.name, null));
        row.appendChild(el("td", p.regionLabel || p.region, null));
        row.appendChild(el("td", p.regionCount == null ? "—" : String(p.regionCount), null));
        row.appendChild(el("td", p.displayName || p.name, null));
        body.appendChild(row);
      });
      table.appendChild(body);
      box.appendChild(table);
    } catch (err) {
      box.textContent = "加载失败：" + (err.message || err);
    }
  }

  /* ---------- 解析配置 ---------- */

  function resetParse() {
    pendingId = "";
    pendingProfiles = [];
    document.getElementById("parse-result").hidden = true;
    document.getElementById("parse-rows").innerHTML = "";
    document.getElementById("parse-notes").innerHTML = "";
  }

  function renderParse(data) {
    pendingId = data.pending_id;
    pendingProfiles = data.profiles || [];
    var notes = document.getElementById("parse-notes");
    notes.innerHTML = "";
    if (data.skipped && data.skipped.length > 0) {
      notes.appendChild(el("div", "已存在，已跳过：" + data.skipped.join("、"), null));
    }
    if (data.cloudflare && data.cloudflare.present) {
      var cfText = "配置含 [cloudflare] 段，将一并保存"
        + (data.cloudflare.has_token ? "（API Token）" : "")
        + (data.cloudflare.email_masked ? "，邮箱 " + data.cloudflare.email_masked : "")
        + "。";
      notes.appendChild(el("div", cfText, null));
    }
    if (pendingProfiles.length === 0) {
      notes.appendChild(el("div", "没有新账号，只保存 [cloudflare] 段。", null));
    }
    var rows = document.getElementById("parse-rows");
    rows.innerHTML = "";
    pendingProfiles.forEach(function (p) {
      var row = document.createElement("tr");
      row.appendChild(el("td", p.profile, null));
      row.appendChild(el("td", p.regionLabel || p.region, null));
      var keyCell = el("td", p.key_file, null);
      keyCell.style.fontFamily = "monospace";
      keyCell.style.wordBreak = "break-all";
      row.appendChild(keyCell);
      row.appendChild(el("td", p.key_ready ? "已就位" : "需上传私钥", null));
      var upCell = document.createElement("td");
      if (!p.key_ready) {
        var input = document.createElement("input");
        input.type = "file";
        input.id = "pem-" + p.profile;
        input.accept = ".pem";
        upCell.appendChild(input);
      } else {
        upCell.textContent = "—";
      }
      row.appendChild(upCell);
      rows.appendChild(row);
    });
    document.getElementById("parse-result").hidden = false;
    document.getElementById("commit-result").hidden = true;
  }

  async function onParse() {
    var text = document.getElementById("cfg-text").value;
    var fileInput = document.getElementById("cfg-file");
    var file = fileInput.files && fileInput.files[0];
    if (!text.trim() && !file) {
      MBot.toast("请粘贴 config 文本或上传 config 文件", "error");
      return;
    }
    var formData = new FormData();
    formData.append("config_text", text);
    if (file) formData.append("config_file", file);
    try {
      var res = await fetch("/api/profiles/parse", {
        method: "POST",
        credentials: "same-origin",
        body: formData,
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "解析失败"));
      renderParse(data);
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  /* ---------- 确认添加 ---------- */

  function renderCommit(data) {
    var box = document.getElementById("commit-result");
    box.innerHTML = "";
    box.hidden = false;
    if (data.added && data.added.length > 0) {
      box.appendChild(el("div", "已添加账号：" + data.added.join("、"), null));
    }
    (data.keys || []).forEach(function (k) {
      var line = "账号 " + k.profile + "：私钥"
        + (k.key_ready ? "已就位（" + k.key_file + "）" : "未就位，请自行 scp 到 " + k.key_file);
      box.appendChild(el("div", line, null));
    });
    if (data.pem_failed && data.pem_failed.length > 0) {
      box.appendChild(el("div", "以下账号的私钥保存失败，请自行 scp：" + data.pem_failed.join("、"), null));
    }
    if (data.cloudflare === "saved") {
      box.appendChild(el("div", "[cloudflare] 段已保存。", null));
    } else if (data.cloudflare === "exists") {
      box.appendChild(el("div", "[cloudflare] 段已存在，未覆盖。", null));
    }
    if (data.restart_needed) {
      box.appendChild(el("div", "新账号已写入配置，重启机器人与面板服务后生效。", null));
    }
    MBot.toast("添加成功", "success");
  }

  async function onCommit() {
    if (!pendingId) {
      MBot.toast("请先解析配置", "error");
      return;
    }
    var formData = new FormData();
    formData.append("pending_id", pendingId);
    pendingProfiles.forEach(function (p) {
      var input = document.getElementById("pem-" + p.profile);
      if (input && input.files && input.files[0]) {
        formData.append("pem_" + p.profile, input.files[0]);
      }
    });
    try {
      var res = await fetch("/api/profiles/commit", {
        method: "POST",
        credentials: "same-origin",
        body: formData,
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "添加失败"));
      resetParse();
      renderCommit(data);
      loadAccounts();
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  /* ---------- 初始化 ---------- */

  document.getElementById("btn-refresh-accounts").addEventListener("click", loadAccounts);
  document.getElementById("btn-parse").addEventListener("click", onParse);
  document.getElementById("btn-commit").addEventListener("click", onCommit);
  document.getElementById("btn-cancel-parse").addEventListener("click", resetParse);
  loadAccounts();
})();
