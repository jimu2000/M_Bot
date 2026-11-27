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
      ["账号", "主区域", "订阅区域数", "显示名", "私钥状态", "上传私钥", "操作"].forEach(function (h) {
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
        row.appendChild(el("td", p.key_ready ? "已就位"
          : (p.key_file ? "缺失，需上传" : "未配置"), null));
        var uploadCell = document.createElement("td");
        if (p.key_file) {
          var input = document.createElement("input");
          input.type = "file";
          input.id = "key-" + p.name;
          input.accept = ".pem";
          uploadCell.appendChild(input);
          var btn = el("button", p.key_ready ? "替换" : "上传", "btn");
          btn.type = "button";
          btn.addEventListener("click", function () {
            uploadProfileKey(p.name, p.key_ready);
          });
          uploadCell.appendChild(btn);
        } else {
          uploadCell.textContent = "—";
        }
        row.appendChild(uploadCell);
        var opCell = document.createElement("td");
        if (data.profiles.length > 1 && p.name !== data.default) {
          var delBtn = el("button", "删除", "btn");
          delBtn.type = "button";
          delBtn.addEventListener("click", function () {
            deleteProfile(p.name);
          });
          opCell.appendChild(delBtn);
        } else {
          opCell.textContent = "—";
        }
        row.appendChild(opCell);
        body.appendChild(row);
      });
      table.appendChild(body);
      /* 手机上 7 列表格超出卡片宽：包进 .table-wrap 横向滚动，不再把页面撑宽。 */
      var wrap = document.createElement("div");
      wrap.className = "table-wrap";
      wrap.appendChild(table);
      box.appendChild(wrap);
    } catch (err) {
      box.textContent = "加载失败：" + (err.message || err);
    }
  }

  /* 给已配置的账号补传/替换私钥：文件写到该账号配置里的 key_file 路径。 */
  async function uploadProfileKey(profile, keyReady) {
    var input = document.getElementById("key-" + profile);
    if (!input || !input.files || input.files.length === 0) {
      MBot.toast("请先选择 .pem 私钥文件。");
      return;
    }
    if (keyReady && !window.confirm("该账号私钥已就位，确定要替换吗？")) {
      return;
    }
    var formData = new FormData();
    formData.append("key_file", input.files[0]);
    try {
      var res = await fetch("/api/profiles/" + encodeURIComponent(profile) + "/key",
        { method: "POST", body: formData, credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "上传私钥失败"));
      MBot.toast("私钥已保存到 " + data.key_file + "。");
      loadAccounts();
    } catch (err) {
      MBot.toast("上传失败：" + (err.message || err));
    }
  }

  /* 删除已配置的账号：删配置段 + compartment 变量；私钥无共用时一并删除。 */
  async function deleteProfile(profile) {
    if (!window.confirm("确定要删除账号 " + profile + " 吗？\n该账号的配置将从服务器上删除，此操作不可撤销。")) {
      return;
    }
    try {
      var res = await fetch("/api/profiles/" + encodeURIComponent(profile),
        { method: "DELETE", credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "删除失败"));
      MBot.toast("已删除账号 " + profile + "（机器人需重启后生效）"
        + (data.key_removed ? "，私钥文件已一并删除。" : "。"));
      loadAccounts();
    } catch (err) {
      MBot.toast("删除失败：" + (err.message || err));
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
    if (data.renamed && data.renamed.length > 0) {
      data.renamed.forEach(function (r) {
        notes.appendChild(el("div", "「" + r.from + "」已存在，已改名为「" + r.to + "」导入。", null));
      });
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

  /* ---------- IAM 用户管理 ---------- */

  var iamProfile = "";

  function iamOnceHtml() {
    var box = document.getElementById("iam-once");
    box.innerHTML = "";
    box.hidden = false;
    return box;
  }

  function downloadText(filename, text) {
    var blob = new Blob([text], { type: "text/plain" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function showOnceSecrets(items) {
    // 一个操作可能同时返回私钥 PEM 与 OCI config。必须一次性渲染，
    // 不能连续调用 showOnceSecret()，否则第二次会清空并覆盖第一个文件。
    var box = iamOnceHtml();
    var intro = el("div", "敏感信息仅本次显示，请立即分别下载保存；关闭或刷新页面后无法再次取回。", null);
    intro.className = "iam-secret-intro";
    box.appendChild(intro);
    (items || []).forEach(function (item) {
      if (!item || !item.value) return;
      var section = document.createElement("section");
      section.className = "iam-secret-item";
      var heading = el("div", item.title || item.filename, null);
      heading.className = "iam-secret-title";
      section.appendChild(heading);
      var ta = document.createElement("textarea");
      ta.rows = item.filename && item.filename.toLowerCase().endsWith(".pem") ? 8 : 6;
      ta.readOnly = true;
      ta.value = item.value;
      ta.setAttribute("aria-label", item.title || item.filename);
      ta.className = "iam-secret-value";
      section.appendChild(ta);
      var row = document.createElement("div");
      row.className = "iam-secret-actions";
      var dl = el("button", "下载 " + item.filename, "btn btn-primary");
      dl.type = "button";
      dl.addEventListener("click", function () {
        downloadText(item.filename, item.value);
      });
      row.appendChild(dl);
      var cp = el("button", "复制", "btn");
      cp.type = "button";
      cp.addEventListener("click", function () {
        ta.focus();
        ta.select();
        var copied = false;
        try { copied = document.execCommand("copy"); } catch (_err) {}
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(item.value).then(function () {
            MBot.toast("已复制。");
          }).catch(function () {
            MBot.toast(copied ? "已复制。" : "复制失败，请手动选择文本。", copied ? "success" : "error");
          });
        } else {
          MBot.toast(copied ? "已复制。" : "复制失败，请手动选择文本。", copied ? "success" : "error");
        }
      });
      row.appendChild(cp);
      section.appendChild(row);
      box.appendChild(section);
    });
    var hide = el("button", "隐藏全部", "btn");
    hide.type = "button";
    hide.className = "btn iam-secret-hide";
    hide.addEventListener("click", function () {
      box.innerHTML = "";
      box.hidden = true;
    });
    box.appendChild(hide);
  }

  function showOnceSecret(title, value, filename) {
    showOnceSecrets([{ title: title, value: value, filename: filename }]);
  }

  async function loadIamProfiles() {
    var sel = document.getElementById("iam-profile");
    try {
      var res = await fetch("/api/profiles", { credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "加载账号失败"));
      sel.innerHTML = "";
      (data.profiles || []).forEach(function (p) {
        var opt = document.createElement("option");
        opt.value = p.name;
        opt.textContent = p.displayName || p.name;
        if (p.name === data.default) opt.selected = true;
        sel.appendChild(opt);
      });
      iamProfile = sel.value || data.default || "";
      loadIamUsers();
    } catch (err) {
      document.getElementById("iam-users").textContent = "加载失败：" + (err.message || err);
    }
  }

  async function loadIamUsers() {
    var box = document.getElementById("iam-users");
    box.textContent = "加载中…";
    try {
      var res = await fetch("/api/iam/users?profile=" + encodeURIComponent(iamProfile),
        { credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "加载用户失败"));
      box.innerHTML = "";
      var users = data.users || [];
      if (users.length === 0) {
        box.textContent = "该账号下暂无 IAM 用户。";
        return;
      }
      // Mobile-first IAM user cards: header (name + state badge),
      // email, then a 3-button grid. Subtle tinted buttons keep the
      // card readable on narrow screens without wrapping.
      var list = el("div", null, "iam-user-list");
      users.forEach(function (u) {
        var card = el("article", null, "iam-user-card");
        var header = el("div", null, "iam-user-header");
        header.appendChild(el("div", u.name || "未知用户", "iam-user-name"));
        var state = (u["lifecycle-state"] || u.lifecycle_state || "UNKNOWN").toUpperCase();
        header.appendChild(el("span", state, "iam-user-badge" + (state === "ACTIVE" ? " is-active" : "")));
        card.appendChild(header);
        card.appendChild(el("div", u.email || "未设置邮箱", "iam-user-email"));
        var actions = el("div", null, "iam-user-actions");
        var keyBtn = el("button", "密钥管理", "btn-subtle");
        keyBtn.type = "button";
        keyBtn.addEventListener("click", function () { showUserKeys(u.id, u.name); });
        actions.appendChild(keyBtn);
        var pwdBtn = el("button", "重置密码", "btn-subtle btn-subtle-primary");
        pwdBtn.type = "button";
        pwdBtn.addEventListener("click", function () { resetIamPassword(u.id, u.name); });
        actions.appendChild(pwdBtn);
        var delBtn = el("button", "删除", "btn-subtle btn-subtle-danger");
        delBtn.type = "button";
        delBtn.addEventListener("click", function () { deleteIamUser(u.id, u.name); });
        actions.appendChild(delBtn);
        card.appendChild(actions);
        list.appendChild(card);
      });
      box.appendChild(list);
    } catch (err) {
      box.textContent = "加载失败：" + (err.message || err);
    }
  }

  async function addIamUser() {
    var name = document.getElementById("iam-new-name").value.trim();
    var email = document.getElementById("iam-new-email").value.trim();
    var desc = document.getElementById("iam-new-desc").value.trim();
    if (!name || !email) {
      MBot.toast("请填写用户名与邮箱。");
      return;
    }
    try {
      var res = await fetch("/api/iam/users", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: iamProfile, name: name, email: email, description: desc }),
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "新增用户失败"));
      MBot.toast("用户 " + name + " 已创建。");
      document.getElementById("iam-new-name").value = "";
      document.getElementById("iam-new-email").value = "";
      document.getElementById("iam-new-desc").value = "";
      loadIamUsers();
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  async function createBotUser() {
    if (!window.confirm("将在账号 " + iamProfile + " 创建专用 Bot API 用户：\n\n用户名：api_user\n邮箱：api@example.com\n组：Group_for_Api_used\n策略：Api_User_Policy\n\n权限：管理实例、卷、虚拟网络，以及 `manage users in tenancy`（可管理租户内所有 IAM 用户及其凭据，属于高权限）。\n将自动生成 RSA API Key，私钥只显示一次。")) {
      return;
    }
    try {
      var res = await fetch("/api/iam/users/bot", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: iamProfile }),
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "创建 Bot 用户失败"));
      MBot.toast("Bot 用户已创建。");
      loadIamUsers();
      showOnceSecrets([
        data.private_key_pem ? {
          title: "Bot API 用户私钥（PEM）",
          value: data.private_key_pem,
          filename: "oci_api_key.pem"
        } : null,
        data.oci_config ? {
          title: "OCI 配置文件（可直接粘贴到下方“添加租户”）",
          value: data.oci_config,
          filename: "oci_config.txt"
        } : null
      ]);
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  async function showUserKeys(userId, userName) {
    var box = iamOnceHtml();
    box.appendChild(el("div", "用户「" + userName + "」的 API Key：", null));
    try {
      var res = await fetch("/api/iam/users/" + encodeURIComponent(userId) + "/keys?profile=" + encodeURIComponent(iamProfile),
        { credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "加载密钥失败"));
      var keys = data.keys || [];
      if (keys.length === 0) {
        box.appendChild(el("div", "暂无 API Key。", null));
      } else {
        keys.forEach(function (k) {
          var line = document.createElement("div");
          line.style.marginTop = "8px";
          line.style.fontFamily = "monospace";
          line.style.wordBreak = "break-all";
          line.textContent = (k.fingerprint || "?") + "（" + (k["lifecycle-state"] || k.lifecycle_state || "") + "）";
          var del = el("button", "删除", "btn");
          del.type = "button";
          del.style.marginLeft = "8px";
          del.addEventListener("click", function () { deleteUserKey(userId, userName, k.fingerprint); });
          line.appendChild(del);
          box.appendChild(line);
        });
      }
      var addBtn = el("button", "新增 API Key", "btn btn-primary");
      addBtn.type = "button";
      addBtn.style.marginTop = "12px";
      addBtn.addEventListener("click", function () { addUserKey(userId, userName); });
      box.appendChild(addBtn);
    } catch (err) {
      box.appendChild(el("div", "加载失败：" + (err.message || err), null));
    }
  }

  async function addUserKey(userId, userName) {
    try {
      var res = await fetch("/api/iam/users/" + encodeURIComponent(userId) + "/keys", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: iamProfile }),
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "新增 API Key 失败"));
      MBot.toast("API Key 已创建。");
      showOnceSecrets([
        data.private_key_pem ? {
          title: "用户「" + userName + "」的新 API Key 私钥（PEM）",
          value: data.private_key_pem,
          filename: "oci_api_key.pem"
        } : null,
        data.oci_config ? {
          title: "OCI 配置文件（可直接粘贴到下方“添加租户”）",
          value: data.oci_config,
          filename: "oci_config.txt"
        } : null
      ]);
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  async function deleteUserKey(userId, userName, fingerprint) {
    if (!window.confirm("确定要删除用户「" + userName + "」的 API Key " + fingerprint + " 吗？")) {
      return;
    }
    try {
      var res = await fetch("/api/iam/users/" + encodeURIComponent(userId) + "/keys/" + encodeURIComponent(fingerprint) + "?profile=" + encodeURIComponent(iamProfile),
        { method: "DELETE", credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "删除 API Key 失败"));
      MBot.toast("API Key 已删除。");
      showUserKeys(userId, userName);
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  async function resetIamPassword(userId, userName) {
    if (!window.confirm("确定要重置用户「" + userName + "」的控制台密码吗？\n将生成一个临时密码，只显示一次。")) {
      return;
    }
    try {
      var res = await fetch("/api/iam/users/" + encodeURIComponent(userId) + "/reset-password", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile: iamProfile }),
      });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "重置密码失败"));
      if (data.password) {
        showOnceSecret("用户「" + userName + "」的控制台临时密码", data.password, "temp_password.txt");
      } else {
        MBot.toast("密码已重置（该租户通过邮件发送重置链接，未返回密码）。");
      }
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  async function deleteIamUser(userId, userName) {
    if (!window.confirm("确定要删除 IAM 用户「" + userName + "」吗？\n此操作不可撤销。")) {
      return;
    }
    try {
      var res = await fetch("/api/iam/users/" + encodeURIComponent(userId) + "?profile=" + encodeURIComponent(iamProfile),
        { method: "DELETE", credentials: "same-origin" });
      var data = await res.json();
      if (!data.ok) throw new Error(apiError(data, "删除用户失败"));
      MBot.toast("用户「" + userName + "」已删除。");
      loadIamUsers();
    } catch (err) {
      MBot.toast(err.message || err, "error");
    }
  }

  /* ---------- 初始化 ---------- */

  document.getElementById("btn-refresh-accounts").addEventListener("click", loadAccounts);
  document.getElementById("iam-profile").addEventListener("change", function (e) {
    iamProfile = e.target.value;
    loadIamUsers();
  });
  document.getElementById("btn-refresh-iam").addEventListener("click", loadIamUsers);
  document.getElementById("btn-iam-add").addEventListener("click", addIamUser);
  document.getElementById("btn-iam-bot").addEventListener("click", createBotUser);
  loadAccounts();
  loadIamProfiles();
  document.getElementById("btn-parse").addEventListener("click", onParse);
  document.getElementById("btn-commit").addEventListener("click", onCommit);
  document.getElementById("btn-cancel-parse").addEventListener("click", resetParse);
})();
