/* Email Delivery 逻辑：发件人列表/创建、SMTP 凭据生成（只显示一次） */
(function () {
  "use strict";
  var MBot = window.MBot;
  var senders = [];
  var users = [];

  function stateBadge(state) {
    var s = (state || "").toUpperCase();
    if (s === "ACTIVE") return '<span class="badge badge-green">ACTIVE</span>';
    if (s === "CREATING") return '<span class="badge badge-amber">CREATING</span>';
    if (s === "INACTIVE" || s === "DELETED") return '<span class="badge badge-red">' + MBot.escapeHtml(state) + "</span>";
    return '<span class="badge badge-gray">' + MBot.escapeHtml(state || "—") + "</span>";
  }

  function renderSenders() {
    var tbody = document.getElementById("sender-tbody");
    if (senders.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: var(--text-muted);">还没有发件人</td></tr>';
      return;
    }
    tbody.innerHTML = senders.map(function (s) {
      var esc = MBot.escapeHtml;
      var email = s["email-address"] || s.email_address || "—";
      var state = s["lifecycle-state"] || s.lifecycle_state || "";
      return (
        "<tr><td><b>" + esc(email) + "</b></td>" +
        "<td>" + stateBadge(state) + "</td>" +
        "<td>" + esc(MBot.formatTime(s["time-created"] || s.time_created)) + "</td></tr>"
      );
    }).join("");
  }

  function renderUsers() {
    var select = document.getElementById("smtp-user");
    if (users.length === 0) {
      select.innerHTML = '<option value="">没有可用的 IAM 用户</option>';
      return;
    }
    select.innerHTML = users.map(function (u) {
      var esc = MBot.escapeHtml;
      var label = u.name || u.id;
      if (u.email) label += "（" + u.email + "）";
      return '<option value="' + esc(u.id) + '">' + esc(label) + "</option>";
    }).join("");
  }

  // Clear the credential result area (after closing, the password no longer stays on the page). 清掉凭据结果区（关闭后密码不再留在页面上）。
  function clearSmtpResult() {
    document.getElementById("smtp-username-result").value = "";
    document.getElementById("smtp-password-result").value = "";
    document.getElementById("smtp-result").hidden = true;
  }

  async function loadAll() {
    try {
      var data = await MBot.api("/api/email/senders");
      senders = data.senders || [];
    } catch (err) {
      document.getElementById("sender-tbody").innerHTML =
        '<tr><td colspan="3" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
    }
    try {
      var u = await MBot.api("/api/email/users");
      users = u.users || [];
    } catch (err) {
      document.getElementById("smtp-user").innerHTML =
        '<option value="">用户列表加载失败</option>';
    }
    renderSenders();
    renderUsers();
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadAll();
    document.getElementById("btn-refresh-email").addEventListener("click", loadAll);

    document.getElementById("create-sender-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var email = document.getElementById("sender-email").value.trim();
      if (!email) return;
      try {
        await MBot.api("/api/email/senders", {
          method: "POST",
          body: { email_address: email },
        });
        MBot.toast("发件人已创建，请到该邮箱点击验证链接", "success");
        document.getElementById("create-sender-form").reset();
        await loadAll();
      } catch (err) {
        MBot.toast("创建失败：" + err.message, "error");
      }
    });

    // Generate SMTP credentials (the password is shown only once). 生成 SMTP 凭据（密码只显示一次）。
    document.getElementById("create-smtp-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var userId = document.getElementById("smtp-user").value;
      var description = document.getElementById("smtp-description").value.trim();
      if (!userId) {
        MBot.toast("请先选择 IAM 用户", "error");
        return;
      }
      var ok = await MBot.confirmDialog(
        "生成 SMTP 凭据",
        "<p>将为选中的 IAM 用户生成一组新的 SMTP 凭据。</p>" +
        "<p>密码只在生成后显示一次，请立即保存，关闭后无法再次查看。</p>",
        "确认生成", false
      );
      if (!ok) return;
      try {
        var data = await MBot.api("/api/email/smtp-credentials", {
          method: "POST",
          body: { user_id: userId, description: description || "mbot" },
        });
        var cred = data.credential || {};
        document.getElementById("smtp-username-result").value = cred.username || "";
        document.getElementById("smtp-password-result").value = cred.password || "";
        document.getElementById("smtp-result").hidden = false;
        MBot.toast("SMTP 凭据已生成，请立即保存", "success");
      } catch (err) {
        MBot.toast("生成失败：" + err.message, "error");
      }
    });

    document.getElementById("btn-copy-smtp").addEventListener("click", async function () {
      var username = document.getElementById("smtp-username-result").value;
      var password = document.getElementById("smtp-password-result").value;
      await MBot.copyText("用户名：" + username + "\n密码：" + password, "SMTP 凭据");
    });

    // Close the credential result area (clearing the password from the page at the same time). 关闭凭据结果区（同时清掉页面上的密码）。
    document.getElementById("btn-close-smtp").addEventListener("click", clearSmtpResult);
  });
})();
