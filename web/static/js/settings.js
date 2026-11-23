/* 设置：修改面板密码（base.html 顶栏齿轮按钮打开的弹窗） */
(function () {
  "use strict";
  var RBot = window.RBot;

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("change-password-form");
    if (!form) return; // 登录页等没有改密码弹窗的页面直接跳过
    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var oldPassword = document.getElementById("cp-old-password").value;
      var newPassword = document.getElementById("cp-new-password").value;
      var confirmPassword = document.getElementById("cp-confirm-password").value;
      if (newPassword !== confirmPassword) {
        RBot.toast("两次输入的新密码不一致", "error");
        return;
      }
      if (newPassword.length < 8) {
        RBot.toast("新密码至少 8 位", "error");
        return;
      }
      if (!/[A-Za-z]/.test(newPassword) || !/[0-9]/.test(newPassword)) {
        RBot.toast("新密码必须同时包含字母和数字", "error");
        return;
      }
      try {
        var data = await RBot.api("/api/auth/change-password", {
          method: "POST",
          body: { old_password: oldPassword, new_password: newPassword },
        });
        RBot.toast(data.message || "密码已更新，重启 Web 面板服务后生效", "success");
        RBot.closeModal("change-password-modal");
        form.reset();
      } catch (err) {
        RBot.toast("修改失败：" + err.message, "error");
      }
    });
  });
})();
