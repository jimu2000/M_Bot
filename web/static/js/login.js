/* 登录页逻辑：密码登录（含可选图形验证码）+ Telegram 验证码登录 + 429 锁定倒计时 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var lockoutTimer = null;
  var captchaId = null;      // 当前图形验证码 id（后端开启验证码时才有）
  var captchaEnabled = false;
  var tgCodeId = null;       // 当前 Telegram 验证码 id
  var tgCooldownTimer = null;

  // 锁定倒计时：seconds 秒后解除，期间禁用登录按钮
  function startLockoutCountdown(seconds) {
    var info = document.getElementById("lockout-info");
    var countEl = document.getElementById("lockout-countdown");
    var btn = document.getElementById("login-btn");
    if (!info || !countEl) return;
    info.hidden = false;
    if (btn) btn.disabled = true;
    if (lockoutTimer) clearInterval(lockoutTimer);
    var remaining = Math.max(1, Math.floor(seconds));
    function tick() {
      var m = Math.floor(remaining / 60);
      var s = remaining % 60;
      countEl.textContent = String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
      if (remaining <= 0) {
        clearInterval(lockoutTimer);
        info.hidden = true;
        if (btn) btn.disabled = false;
        return;
      }
      remaining -= 1;
    }
    tick();
    lockoutTimer = setInterval(tick, 1000);
  }

  function showError(msg) {
    var el = document.getElementById("login-error");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
  }
  function hideError() {
    var el = document.getElementById("login-error");
    if (el) el.hidden = true;
  }

  // 图形验证码：拉取 /api/auth/captcha，开启时显示图片与输入框
  async function loadCaptcha() {
    var block = document.getElementById("captcha-block");
    var img = document.getElementById("captcha-image");
    var input = document.getElementById("captcha-answer");
    if (!block || !img) return;
    try {
      var res = await MBot.api("/api/auth/captcha", { noRedirect: true });
      if (res.enabled) {
        captchaEnabled = true;
        captchaId = res.captchaId;
        img.src = res.image;
        block.hidden = false;
        if (input) input.value = "";
      } else {
        captchaEnabled = false;
        captchaId = null;
        block.hidden = true;
      }
    } catch (err) {
      // 拉取失败时当作未开启（后端关闭验证码时该接口仍返回 enabled=false）
      captchaEnabled = false;
      block.hidden = true;
    }
  }

  // Telegram 发码冷却倒计时（后端 60 秒冷却，这里同步显示）
  function startTgCooldown(seconds) {
    var btn = document.getElementById("tg-send-btn");
    if (!btn) return;
    btn.disabled = true;
    if (tgCooldownTimer) clearInterval(tgCooldownTimer);
    var remaining = Math.max(1, Math.floor(seconds));
    function tick() {
      btn.textContent = "重新发送（" + remaining + "s）";
      if (remaining <= 0) {
        clearInterval(tgCooldownTimer);
        btn.disabled = false;
        btn.textContent = "发送验证码";
        return;
      }
      remaining -= 1;
    }
    tick();
    tgCooldownTimer = setInterval(tick, 1000);
  }

  // 标签页切换：密码登录 / Telegram 验证码登录
  function switchTab(which) {
    var tabPassword = document.getElementById("tab-password");
    var tabTelegram = document.getElementById("tab-telegram");
    var form = document.getElementById("login-form");
    var tgForm = document.getElementById("telegram-form");
    var isTg = which === "telegram";
    if (tabPassword) {
      tabPassword.classList.toggle("active", !isTg);
      tabPassword.setAttribute("aria-selected", String(!isTg));
    }
    if (tabTelegram) {
      tabTelegram.classList.toggle("active", isTg);
      tabTelegram.setAttribute("aria-selected", String(isTg));
    }
    if (form) form.hidden = isTg;
    if (tgForm) tgForm.hidden = !isTg;
    hideError();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("login-form");
    var btn = document.getElementById("login-btn");
    var tgForm = document.getElementById("telegram-form");
    var tabTelegram = document.getElementById("tab-telegram");
    var tabPassword = document.getElementById("tab-password");

    loadCaptcha();

    var captchaImg = document.getElementById("captcha-image");
    if (captchaImg) captchaImg.addEventListener("click", loadCaptcha);

    // 忘记密码：面板没有自助找回，提示用 Telegram 机器人 /raninfo 重置
    var forgotLink = document.getElementById("forgot-password-link");
    if (forgotLink) {
      forgotLink.addEventListener("click", function (e) {
        e.preventDefault();
        MBot.toast("面板密码请在 Telegram 机器人里用 /raninfo 命令重置", "info");
      });
    }

    if (tabTelegram) tabTelegram.addEventListener("click", function () { switchTab("telegram"); });
    if (tabPassword) tabPassword.addEventListener("click", function () { switchTab("password"); });

    // Telegram：发送验证码
    var tgSendBtn = document.getElementById("tg-send-btn");
    if (tgSendBtn) {
      tgSendBtn.addEventListener("click", async function () {
        hideError();
        tgSendBtn.disabled = true;
        tgSendBtn.textContent = "发送中…";
        try {
          var res = await MBot.api("/api/auth/telegram/send-code", {
            method: "POST",
            body: {},
            noRedirect: true,
          });
          tgCodeId = res.codeId;
          MBot.toast("验证码已发送到 Telegram，5 分钟内有效", "success");
          startTgCooldown(60);
          var codeInput = document.getElementById("tg-code");
          if (codeInput) codeInput.focus();
        } catch (err) {
          tgSendBtn.disabled = false;
          tgSendBtn.textContent = "发送验证码";
          if (err.status === 429) {
            var seconds = (err.data && err.data.retry_after_seconds) || 60;
            showError(err.message || "发送太频繁，请稍后再试");
            startTgCooldown(seconds);
          } else {
            showError(err.message || "发送失败，请检查机器人配置");
          }
        }
      });
    }

    // Telegram：验证码登录
    if (tgForm) {
      tgForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        hideError();
        var code = document.getElementById("tg-code").value.trim();
        if (!tgCodeId || !code) {
          showError("请先发送验证码，再输入 6 位验证码");
          return;
        }
        var tgBtn = document.getElementById("tg-login-btn");
        tgBtn.disabled = true;
        tgBtn.textContent = "登录中…";
        try {
          await MBot.api("/api/auth/telegram/verify", {
            method: "POST",
            body: { code_id: tgCodeId, code: code },
            noRedirect: true,
          });
          window.location.href = "/overview";
        } catch (err) {
          showError(err.message || "验证码错误或已过期");
        } finally {
          tgBtn.disabled = false;
          tgBtn.textContent = "验证码登录";
        }
      });
    }

    if (!form) return;
    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      hideError();
      var username = document.getElementById("username").value.trim();
      var password = document.getElementById("password").value;
      if (!username || !password) {
        showError("请输入用户名和密码");
        return;
      }
      var body = { username: username, password: password };
      var rememberBox = document.getElementById("remember-me");
      if (rememberBox && rememberBox.checked) body.remember = true;
      if (captchaEnabled) {
        var answer = document.getElementById("captcha-answer").value.trim();
        if (!answer || !captchaId) {
          showError("请填写图形验证码");
          return;
        }
        body.captcha_id = captchaId;
        body.captcha_answer = answer;
      }
      btn.disabled = true;
      btn.textContent = "登录中…";
      try {
        // noRedirect：登录页自己处理 401/429，不跳回登录页
        await MBot.api("/api/auth/login", {
          method: "POST",
          body: body,
          noRedirect: true,
        });
        window.location.href = "/overview";
      } catch (err) {
        if (err.status === 429) {
          // 锁定：后端返回 remaining_seconds；兼容其他常见键名，缺失时默认 30 分钟
          var seconds = (err.data && (err.data.remaining_seconds || err.data.retry_after_seconds || err.data.retry_after)) || 1800;
          var msg = err.data && (err.data.detail || err.data.message);
          if (msg) showError(msg);
          startLockoutCountdown(seconds);
        } else {
          showError(err.message || "登录失败，请重试");
          // 验证码错误时刷新一张新的（旧的已一次性作废）
          if (captchaEnabled) loadCaptcha();
        }
      } finally {
        btn.disabled = false;
        btn.textContent = "登录";
      }
    });
  });
})();
