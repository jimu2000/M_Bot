/* Bark 通知设置页（菜单 43 的网页版）。
 * 数据：GET /api/bark/status（开关、是否配齐、地址脱敏、Key 是否已设）。
 * 操作：POST /api/bark/config（保存地址/Key）、POST /api/bark/toggle
 * （开关）、POST /api/bark/test（测试推送，不依赖开关）、
 * POST /api/bark/generate-key（自动生成 32 位 Key，返回 Key 原文 +
 * 二维码，只显示一次；生成后旧 Key 立即失效）。
 * 与机器人端共用 tgbot/data/bark.json，两边改的配置互相可见。
 * Key 与地址原文绝不在页面上显示（自动生成的 Key 例外：生成后
 * 必须同步到 Bark App，所以弹窗显示一次 + 二维码扫码添加）。
 */
(function () {
  "use strict";

  var statusData = null;

  function showError(message) {
    document.getElementById("bark-loading").hidden = true;
    document.getElementById("bark-body").hidden = true;
    var box = document.getElementById("bark-error");
    document.getElementById("bark-error-text").textContent = message;
    box.hidden = false;
  }

  function render() {
    if (!statusData) return;
    document.getElementById("bark-loading").hidden = true;
    document.getElementById("bark-error").hidden = true;
    document.getElementById("bark-body").hidden = false;
    document.getElementById("bark-enabled-status").textContent =
      statusData.enabled ? "✅ 已开启" : "❌ 已关闭";
    document.getElementById("bark-url-masked").textContent =
      statusData.url_masked || "未设置";
    document.getElementById("bark-key-status").textContent =
      statusData.key_set ? "✅ 已设置（32 位）" : "❌ 未设置";
    var hint = document.getElementById("bark-hint");
    if (!statusData.configured) {
      hint.textContent = "先设置 Bark 地址和 32 位加密 Key，再点“测试推送”" +
        "确认能收到，最后打开开关。";
    } else if (!statusData.enabled) {
      hint.textContent = "配置已配齐，打开开关后开始推送。";
    } else {
      hint.textContent = "Bark 推送已开启：开机成功、账单超支等通知会" +
        "同时推到 Bark 和 Telegram。";
    }
    var toggleBtn = document.getElementById("btn-bark-toggle");
    toggleBtn.textContent = statusData.enabled ? "关闭推送" : "开启推送";
    toggleBtn.disabled = !statusData.configured && !statusData.enabled;
    document.getElementById("btn-bark-test").disabled =
      !statusData.configured;
  }

  async function load() {
    try {
      statusData = await MBot.api("/api/bark/status");
      render();
    } catch (err) {
      showError("加载失败：" + err.message);
    }
  }

  async function saveConfig() {
    var url = document.getElementById("bark-url-input").value.trim();
    var key = document.getElementById("bark-key-input").value.trim();
    if (!url && !key) {
      MBot.toast("请至少填写 Bark 地址或加密 Key", "error");
      return;
    }
    try {
      statusData = await MBot.api("/api/bark/config", {
        method: "POST", body: { url: url, key: key },
      });
      document.getElementById("bark-url-input").value = "";
      document.getElementById("bark-key-input").value = "";
      MBot.toast("已保存", "success");
      render();
    } catch (err) {
      MBot.toast("保存失败：" + err.message, "error");
    }
  }

  async function toggle() {
    if (!statusData) return;
    var target = !statusData.enabled;
    try {
      statusData = await MBot.api("/api/bark/toggle", {
        method: "POST", body: { enabled: target },
      });
      MBot.toast(target ? "Bark 推送已开启" : "Bark 推送已关闭", "success");
      render();
    } catch (err) {
      MBot.toast("操作失败：" + err.message, "error");
    }
  }

  async function testPush() {
    try {
      await MBot.api("/api/bark/test", { method: "POST", body: {} });
      MBot.toast("测试推送已发送，请查看手机 Bark", "success");
    } catch (err) {
      MBot.toast("测试推送失败：" + err.message, "error");
    }
  }

  async function generateKey() {
    var ok = await MBot.confirmDialog(
      "🎲 生成新的 Bark 加密 Key？",
      "<p>生成后旧 Key 立即失效：Bark App 里要同步改成新 Key，" +
      "否则推送解不开；账单脚本、各主机流量脚本、邮件转发器还在用旧 Key，也要同步更新。</p>",
      "确定生成", true);
    if (!ok) return;
    try {
      var result = await MBot.api("/api/bark/generate-key", { method: "POST", body: {} });
      statusData = result;
      render();
      // Key 只显示这一次（后端保存后不再可查；必须同步到 Bark App）。
      var body = "<p>请用 Bark App 扫下面的二维码添加（也可以手动复制 Key）。" +
        "App 里的 Key 必须和它一致，否则推送解不开：</p>";
      if (result.qr_png) {
        body += "<p style=\"text-align: center;\"><img src=\"" + result.qr_png +
          "\" alt=\"Bark 加密 Key 二维码\" style=\"width: 180px; height: 180px;\"></p>";
      }
      body += "<p><code>" + MBot.escapeHtml(result.key) + "</code></p>" +
        "<p>旧 Key 已失效，账单脚本、各主机流量脚本、邮件转发器还在用旧 Key，需要同步更新。</p>";
      await MBot.confirmDialog("🔑 新的 Bark 加密 Key（只显示这一次）", body, "已记下");
    } catch (err) {
      MBot.toast("生成失败：" + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("btn-refresh-bark").addEventListener("click", load);
    document.getElementById("btn-save-bark-config").addEventListener("click", saveConfig);
    document.getElementById("btn-bark-toggle").addEventListener("click", toggle);
    document.getElementById("btn-bark-test").addEventListener("click", testPush);
    document.getElementById("btn-gen-bark-key").addEventListener("click", generateKey);
    load();
  });
})();
