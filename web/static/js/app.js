/* M_Bot Oracle开源管理面板 · 通用工具
 * api() / toast() / copyText() / 主题切换 / 资源栏轮询 / 弹窗工具
 * 各页面脚本都依赖这里的 RBot 全局对象。 */
(function () {
  "use strict";

  /* ---------- API 封装 ---------- */
  // api("/api/hosts") -> 解析后的 JSON；失败抛 Error（带 status 与 data）
  // 401 默认跳登录页；传 { noRedirect: true } 可自行处理（如登录页）。
  async function api(url, options) {
    options = options || {};
    var res = await fetch(url, {
      method: options.method || "GET",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
    var data = null;
    try { data = await res.json(); } catch (e) { /* 非 JSON 响应时 data 保持 null */ }
    if (res.status === 401 && !options.noRedirect) {
      window.location.href = "/login";
      throw new Error("未登录");
    }
    if (!res.ok || (data && data.ok === false)) {
      var err = new Error(
        (data && (data.detail || data.message || data.error)) ||
        ("请求失败（HTTP " + res.status + "）")
      );
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  /* ---------- 前端缓存（Map + TTL + 按前缀清除） ---------- */
  // 与 R-Bot 的 CloudCache 同款模型：内存 Map，默认 15 分钟 TTL，
  // 键按资源 + profile 区分（如 "inst:DEFAULT"、"vol:list:DEFAULT"），
  // 写操作后按前缀清缓存。页面内存里的缓存，刷新页面即失效；
  // 服务端还有一层缓存（实例 30 秒、云报告 60 秒），两层叠加。
  var CACHE_DEFAULT_TTL_MS = 15 * 60 * 1000; // 默认 15 分钟
  var _cache = new Map();

  function cacheGet(key) {
    var entry = _cache.get(key);
    if (!entry) return null;
    if (Date.now() > entry.expires) {
      _cache.delete(key);
      return null;
    }
    return entry.value;
  }

  function cacheSet(key, value, ttlMs) {
    _cache.set(key, {
      value: value,
      expires: Date.now() + (ttlMs || CACHE_DEFAULT_TTL_MS),
    });
  }

  // 按前缀清除（不传前缀时清全部）。写操作成功后调用，避免列表显示旧数据。
  function cacheClear(prefix) {
    if (prefix == null) {
      _cache.clear();
      return;
    }
    _cache.forEach(function (_, key) {
      if (key.indexOf(prefix) === 0) _cache.delete(key);
    });
  }

  // 带缓存的 GET：命中时直接返回缓存；force=true 时跳过缓存读，
  // 重新请求并刷新缓存（前端刷新按钮用）。
  async function cachedApi(key, url, force) {
    if (!force) {
      var hit = cacheGet(key);
      if (hit !== null) return hit;
    }
    var data = await api(url);
    cacheSet(key, data);
    return data;
  }

  /* ---------- 账号（profile）切换 ---------- */
  // 一个 profile = 一个账号；多区订阅的账号还有二级区域菜单（见下
  // 面“工作区域”一节），区域只是下拉里的显示字段。当前选择优先级：
  // 内存 -> localStorage -> 默认 profile -> 第一个。
  var PROFILE_KEY = "cloud_active_profile";
  var _profiles = null; // 内存中的 profile 列表（GET /api/profiles）
  var _profilesDefault = null; // 后端默认 profile
  var _currentProfile = null; // 内存中的当前选择

  function getProfile() {
    if (_currentProfile) return _currentProfile;
    try {
      _currentProfile = window.localStorage.getItem(PROFILE_KEY) || null;
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
    return _currentProfile;
  }

  function setProfile(name) {
    _currentProfile = name;
    try {
      window.localStorage.setItem(PROFILE_KEY, name);
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
  }

  async function loadProfiles() {
    if (_profiles) return _profiles;
    var res = await api("/api/profiles");
    _profiles = res.profiles || [];
    _profilesDefault = res.default || null;
    return _profiles;
  }

  // 填充 profile 下拉：选项显示 displayName（主区中文名，多区账号
  // 后缀区数，如“新加坡（3 个区）”，与机器人切换菜单一致；旧接口
  // 没有 displayName 时回退到“名字 · 区域中文名”）；恢复保存的选择，
  // 返回选中的名字（列表为空时返回 null，调用方按默认账号加载）。
  async function fillProfileSelect(selectEl) {
    var profiles = await loadProfiles();
    selectEl.innerHTML = "";
    profiles.forEach(function (p) {
      var opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = p.displayName ||
        (p.name + " · " + (p.regionLabel || p.region || ""));
      selectEl.appendChild(opt);
    });
    var saved = getProfile();
    var names = profiles.map(function (p) { return p.name; });
    var selected = null;
    if (saved && names.indexOf(saved) !== -1) selected = saved;
    else if (_profilesDefault && names.indexOf(_profilesDefault) !== -1) selected = _profilesDefault;
    else if (names.length > 0) selected = names[0];
    if (selected) {
      selectEl.value = selected;
      setProfile(selected);
    }
    return selected;
  }

  // 给 URL 拼 ?profile= 与 ?region= 参数（已有查询串时用 & 拼接）。
  // region 不传时只拼 profile（后端用该账号的主区）。
  function withProfile(url, profile, region) {
    if (profile) {
      var sep = url.indexOf("?") === -1 ? "?" : "&";
      url = url + sep + "profile=" + encodeURIComponent(profile);
    }
    if (region) {
      var sep2 = url.indexOf("?") === -1 ? "?" : "&";
      url = url + sep2 + "region=" + encodeURIComponent(region);
    }
    return url;
  }

  /* ---------- 工作区域（region）切换 ---------- */
  // 多区订阅的账号有二级区域菜单：区域下拉只在所选账号订阅了多个
  // 区域时显示（单区账号不显示，与机器人端一致）。区域与账号绑定：
  // 切账号时页面先 setRegion(null) 再调 fillRegionSelect，区域重置
  // 为新账号的主区；保存的选择不在该账号订阅列表里时也回落到主区。
  // 当前选择优先级：内存 -> localStorage -> 主区。
  var REGION_KEY = "cloud_active_region";
  var _currentRegion = null; // 内存中的当前选择（区域代码，如 ap-seoul-1）

  function getRegion() {
    if (_currentRegion) return _currentRegion;
    try {
      _currentRegion = window.localStorage.getItem(REGION_KEY) || null;
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
    return _currentRegion;
  }

  function setRegion(code) {
    _currentRegion = code;
    try {
      if (code) window.localStorage.setItem(REGION_KEY, code);
      else window.localStorage.removeItem(REGION_KEY);
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
  }

  // 某账号的订阅区域列表（[{code, label, isHome}]），来自 loadProfiles
  // 的缓存；未加载或该账号无 regions 时返回 null。
  function profileRegions(profile) {
    if (!_profiles) return null;
    for (var i = 0; i < _profiles.length; i++) {
      if (_profiles[i].name === profile) return _profiles[i].regions || null;
    }
    return null;
  }

  // 填充区域下拉：多区账号显示下拉并恢复保存的选择（不在订阅列表
  // 里时回落主区），单区账号隐藏下拉（select 与 #region-wrap 包裹
  // 层一起隐藏）。返回选中的区域代码（订阅列表未知时返回 null，
  // 调用方按主区处理）。
  function fillRegionSelect(selectEl, profile) {
    var regions = profileRegions(profile);
    var wrap = selectEl.closest ? selectEl.closest("#region-wrap") : null;
    var show = !!(regions && regions.length > 1);
    if (wrap) wrap.hidden = !show;
    selectEl.hidden = !show;
    selectEl.innerHTML = "";
    if (!regions || regions.length === 0) return null;
    regions.forEach(function (r) {
      var opt = document.createElement("option");
      opt.value = r.code;
      opt.textContent = r.label + (r.isHome ? "（主区）" : "");
      selectEl.appendChild(opt);
    });
    var home = null;
    regions.forEach(function (r) { if (r.isHome) home = r.code; });
    if (!home) home = regions[0].code;
    var saved = getRegion();
    var codes = regions.map(function (r) { return r.code; });
    var selected = (saved && codes.indexOf(saved) !== -1) ? saved : home;
    selectEl.value = selected;
    setRegion(selected);
    return selected;
  }

  /* ---------- Toast 提示 ---------- */
  function toast(message, type) {
    type = type || "info";
    var container = document.getElementById("toast-container");
    if (!container) return;
    var el = document.createElement("div");
    el.className = "toast " + type;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  /* ---------- 一键复制 ---------- */
  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // clipboard API 不可用时（如非安全上下文）退回传统方式
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    toast((label || "内容") + "已复制", "success");
  }

  /* ---------- HTML 转义（渲染后端数据时防 XSS） ---------- */
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /* ---------- 时间格式化 ---------- */
  // ISO 时间 -> "2天前" / "3小时前" / "刚刚"；无效值返回 "—"
  function timeAgo(iso) {
    if (!iso) return "—";
    var t = new Date(iso).getTime();
    if (isNaN(t)) return "—";
    var diff = Date.now() - t;
    if (diff < 0) diff = 0;
    var min = Math.floor(diff / 60000);
    if (min < 1) return "刚刚";
    if (min < 60) return min + "分钟前";
    var hour = Math.floor(min / 60);
    if (hour < 24) return hour + "小时前";
    var day = Math.floor(hour / 24);
    if (day < 30) return day + "天前";
    return new Date(t).toLocaleDateString("zh-CN");
  }

  // 时间 -> 本地 "2026/11/23 02:30:00"
  // 接受 ISO 字符串，也接受 Unix 时间戳（秒或毫秒，后端任务用秒级时间戳）
  function formatTime(value) {
    if (value == null || value === "") return "—";
    var d;
    if (typeof value === "number") {
      d = new Date(value < 1e12 ? value * 1000 : value);
    } else {
      d = new Date(value);
    }
    return isNaN(d.getTime()) ? String(value) : d.toLocaleString("zh-CN");
  }

  // 速率：kbps -> 可读字符串
  function formatRate(kbps) {
    if (kbps == null || isNaN(kbps)) return "--";
    if (kbps >= 1024) return (kbps / 1024).toFixed(1) + " MB/s";
    return Math.round(kbps) + " KB/s";
  }

  // 速率：字节/秒 -> 可读字符串（后端 /api/system 返回字节/秒）
  function formatBytesRate(bytesPerSec) {
    if (bytesPerSec == null || isNaN(bytesPerSec)) return "--";
    if (bytesPerSec >= 1024 * 1024) return (bytesPerSec / (1024 * 1024)).toFixed(1) + " MB/s";
    if (bytesPerSec >= 1024) return (bytesPerSec / 1024).toFixed(1) + " KB/s";
    return Math.round(bytesPerSec) + " B/s";
  }

  /* ---------- 弹窗工具 ---------- */
  function openModal(id) {
    var el = document.getElementById(id);
    if (el) el.hidden = false;
  }
  function closeModal(id) {
    var el = document.getElementById(id);
    if (el) el.hidden = true;
  }
  // 通用确认弹窗（动态生成，返回 Promise<boolean>）
  function confirmDialog(title, bodyHtml, confirmText, danger) {
    return new Promise(function (resolve) {
      var root = document.getElementById("modal-root");
      var overlay = document.createElement("div");
      overlay.className = "modal-overlay";
      overlay.innerHTML =
        '<div class="modal-box">' +
          '<div class="modal-head"><span>' + escapeHtml(title) + '</span>' +
          '<button type="button" class="modal-close" aria-label="关闭">×</button></div>' +
          '<div class="modal-body">' + bodyHtml + "</div>" +
          '<div class="modal-foot">' +
            '<button type="button" class="btn" data-act="cancel">取消</button>' +
            '<button type="button" class="btn ' + (danger ? "btn-danger" : "btn-primary") + '" data-act="ok">' +
              escapeHtml(confirmText || "确认") + "</button>" +
          "</div></div>";
      root.appendChild(overlay);
      function done(value) { overlay.remove(); resolve(value); }
      overlay.querySelector('[data-act="cancel"]').addEventListener("click", function () { done(false); });
      overlay.querySelector('[data-act="ok"]').addEventListener("click", function () { done(true); });
      overlay.querySelector(".modal-close").addEventListener("click", function () { done(false); });
      overlay.addEventListener("click", function (e) { if (e.target === overlay) done(false); });
    });
  }

  /* ---------- 启动卷备份（启动盘页与实例卡片弹窗共用） ---------- */
  // 创建备份流程（核心用户要求）：
  //   1. 先调配额接口；未满直接创建。
  //   2. 已满则弹窗提示最早备份，点"删除最早备份并继续"时后端先删
  //      最早备份再创建；点取消什么都不做。
  // 成功返回备份对象，失败/取消返回 null；调用方成功后自行刷新列表。
  async function backupBootVolume(volumeId, profile) {
    // 付费账号不限 5 个，直接创建；免费账号满了服务端自动删最早
    // 备份再建（用户要求 2026-11-24：不用等确认），响应里带
    // deleted_oldest 说明删了哪个。前端不再预查配额、不再弹窗确认。
    var prof = profile || getProfile();
    try {
      var res = await api(withProfile("/api/boot-backups", prof), {
        method: "POST",
        body: {
          boot_volume_id: volumeId,
          delete_oldest: true,
        },
      });
      var name = (res.backup && (res.backup.displayName || res.backup["display-name"])) || "";
      var deleted = res.deleted_oldest;
      toast(deleted
        ? "免费备份已满，已自动删除最早备份「" + deleted.name + "」，备份「" + name + "」创建中（几分钟后可用）。"
        : "备份「" + name + "」创建中（几分钟后可用）。", "success");
      cacheClear("bb:");
      return res.backup || null;
    } catch (e) {
      toast("创建备份失败：" + (e && e.message ? e.message : e), "error");
      return null;
    }
  }

  /* ---------- 主题切换 ---------- */
  function applyTheme(theme, mode) {
    var root = document.documentElement;
    root.setAttribute("data-theme", theme);
    // 星夜固定暗色
    root.setAttribute("data-mode", theme === "starry" ? "dark" : mode);
    try {
      localStorage.setItem("rbot-theme", theme);
      localStorage.setItem("rbot-mode", mode);
    } catch (e) { /* 忽略存储失败 */ }
    var sel = document.getElementById("theme-select");
    if (sel) sel.value = theme;
    updateModeIcon();
  }
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "classic";
  }
  function currentMode() {
    return document.documentElement.getAttribute("data-mode") || "light";
  }
  function updateModeIcon() {
    var dark = currentMode() === "dark";
    var moon = document.getElementById("icon-moon");
    var sun = document.getElementById("icon-sun");
    if (moon) moon.hidden = dark;
    if (sun) sun.hidden = !dark;
  }

  /* ---------- 资源栏轮询（每 5 秒 GET /api/system） ---------- */
  function setGauge(id, textId, pct) {
    var gauge = document.getElementById(id);
    var text = document.getElementById(textId);
    if (!gauge || !text) return;
    var p = Math.max(0, Math.min(100, Math.round(pct || 0)));
    gauge.style.setProperty("--p", p);
    gauge.classList.toggle("warn", p >= 70 && p < 90);
    gauge.classList.toggle("danger", p >= 90);
    text.textContent = p + "%";
  }
  async function refreshSystem() {
    try {
      var data = await api("/api/system");
      // 兼容两种返回格式：
      // 嵌套（后端实际）：{cpu:{percent}, memory:{percent}, disk:{percent},
      //                   network:{rx_bytes_per_sec, tx_bytes_per_sec}}
      // 扁平（契约草案）：{cpu_percent, mem_percent, disk_percent,
      //                   net_down_kbps, net_up_kbps}
      var cpu = data.cpu_percent != null ? data.cpu_percent : (data.cpu && data.cpu.percent);
      var mem = data.mem_percent != null ? data.mem_percent : (data.memory && data.memory.percent);
      var disk = data.disk_percent != null ? data.disk_percent : (data.disk && data.disk.percent);
      setGauge("gauge-cpu", "gauge-cpu-text", cpu);
      setGauge("gauge-mem", "gauge-mem-text", mem);
      setGauge("gauge-disk", "gauge-disk-text", disk);
      var down = document.getElementById("net-down");
      var up = document.getElementById("net-up");
      if (data.network) {
        if (down) down.textContent = formatBytesRate(data.network.rx_bytes_per_sec);
        if (up) up.textContent = formatBytesRate(data.network.tx_bytes_per_sec);
      } else {
        if (down) down.textContent = formatRate(data.net_down_kbps);
        if (up) up.textContent = formatRate(data.net_up_kbps);
      }
    } catch (e) {
      // 资源栏失败不打扰用户，静默等待下次轮询
    }
  }

  // 侧边栏高亮兜底：服务端没传 current_page 时按当前路径高亮
  function highlightSidebar() {
    var sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    if (sidebar.querySelector("a.active")) return; // 服务端已高亮，不动
    var path = window.location.pathname.replace(/\/$/, "") || "/overview";
    var page = path.replace(/^\//, "");
    if (page === "") page = "overview";
    var link = sidebar.querySelector('[data-page="' + page + '"]');
    if (link) link.classList.add("active");
  }

  // 用户名兜底：模板没传 username 时调 /api/auth/me 补填
  async function fillUsername() {
    var el = document.getElementById("res-username");
    if (!el || el.textContent.trim()) return;
    try {
      var data = await api("/api/auth/me");
      if (data.username) el.textContent = data.username;
    } catch (e) { /* 未登录等情况静默跳过 */ }
  }

  /* ---------- 退出登录 ---------- */
  async function logout() {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* 忽略登出失败 */ }
    window.location.href = "/login";
  }

  /* ---------- 初始化 ---------- */
  document.addEventListener("DOMContentLoaded", function () {
    // 主题下拉与本地存储同步
    var themeSel = document.getElementById("theme-select");
    if (themeSel) {
      themeSel.value = currentTheme();
      themeSel.addEventListener("change", function () {
        applyTheme(themeSel.value, currentMode());
      });
    }
    var modeBtn = document.getElementById("mode-toggle");
    if (modeBtn) {
      updateModeIcon();
      modeBtn.addEventListener("click", function () {
        if (currentTheme() === "starry") {
          toast("星夜主题固定为暗色", "info");
          return;
        }
        applyTheme(currentTheme(), currentMode() === "dark" ? "light" : "dark");
      });
    }
    var settingsBtn = document.getElementById("settings-btn");
    if (settingsBtn) {
      settingsBtn.addEventListener("click", function () {
        // 改密码弹窗在 base.html 里（settings.js 处理提交）；
        // 登录页等没有该弹窗的页面保持原提示。
        var modal = document.getElementById("change-password-modal");
        if (modal) {
          openModal("change-password-modal");
        } else {
          toast("设置页面暂未实现", "info");
        }
      });
    }
    var logoutBtn = document.getElementById("logout-btn");
    if (logoutBtn) logoutBtn.addEventListener("click", logout);

    // 弹窗关闭：data-close-modal 属性 + 点遮罩关闭
    document.querySelectorAll("[data-close-modal]").forEach(function (btn) {
      btn.addEventListener("click", function () { closeModal(btn.getAttribute("data-close-modal")); });
    });
    document.querySelectorAll(".modal-overlay").forEach(function (overlay) {
      overlay.addEventListener("click", function (e) {
        if (e.target === overlay) overlay.hidden = true;
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        document.querySelectorAll(".modal-overlay").forEach(function (o) { o.hidden = true; });
      }
    });

    // 侧边栏高亮兜底 + 用户名补填
    highlightSidebar();
    fillUsername();

    // 资源栏：立即刷新一次，再每 5 秒轮询（登录页没有资源栏，跳过）
    if (document.getElementById("gauge-cpu")) {
      refreshSystem();
      setInterval(refreshSystem, 5000);
    }
  });

  /* 暴露给各页面脚本 */
  window.RBot = {
    api: api,
    toast: toast,
    copyText: copyText,
    escapeHtml: escapeHtml,
    timeAgo: timeAgo,
    formatTime: formatTime,
    formatRate: formatRate,
    formatBytesRate: formatBytesRate,
    openModal: openModal,
    closeModal: closeModal,
    confirmDialog: confirmDialog,
    backupBootVolume: backupBootVolume,
    logout: logout,
    // 前端缓存（Map + TTL + 按前缀清除）
    cacheGet: cacheGet,
    cacheSet: cacheSet,
    cacheClear: cacheClear,
    cachedApi: cachedApi,
    CACHE_DEFAULT_TTL_MS: CACHE_DEFAULT_TTL_MS,
    // 账号（profile）切换
    PROFILE_KEY: PROFILE_KEY,
    getProfile: getProfile,
    setProfile: setProfile,
    loadProfiles: loadProfiles,
    fillProfileSelect: fillProfileSelect,
    withProfile: withProfile,
    // 工作区域（region）切换（多区订阅账号的二级菜单）
    REGION_KEY: REGION_KEY,
    getRegion: getRegion,
    setRegion: setRegion,
    profileRegions: profileRegions,
    fillRegionSelect: fillRegionSelect,
  };
})();
