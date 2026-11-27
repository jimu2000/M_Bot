/* M_Bot Oracle开源管理面板 · 通用工具
 * api() / toast() / copyText() / 主题切换 / 资源栏轮询 / 弹窗工具
 * 各页面脚本都依赖这里的 MBot 全局对象。 */
(function () {
  "use strict";

  /* ---------- API 封装 ---------- */
  // api("/api/hosts") -> parsed JSON; on failure throws an Error (with status and data). api("/api/hosts") -> 解析后的 JSON；失败抛 Error（带 status 与 data）。
  // 401 redirects to the login page by default; pass { noRedirect: true } to handle it yourself (e.g. the login page). 401 默认跳登录页；传 { noRedirect: true } 可自行处理（如登录页）。
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
  // CloudCache model: in-memory Map, default 15-minute TTL, keys split by resource + profile (e.g. "inst:DEFAULT", "vol:list:DEFAULT"), and writes clear the cache by prefix. It lives in page memory and dies on refresh; the server has another cache layer (instances 30s, cloud report 60s), so the two stack. CloudCache 模型：内存 Map，默认 15 分钟 TTL，键按资源 + profile 区分（如 "inst:DEFAULT"、"vol:list:DEFAULT"），写操作后按前缀清缓存。页面内存里的缓存，刷新页面即失效；服务端还有一层缓存（实例 30 秒、云报告 60 秒），两层叠加。
  var CACHE_DEFAULT_TTL_MS = 15 * 60 * 1000;
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

  // Clear by prefix (clears everything when no prefix is given). Call after a successful write so lists do not show stale data. 按前缀清除（不传前缀时清全部）。写操作成功后调用，避免列表显示旧数据。
  function cacheClear(prefix) {
    if (prefix == null) {
      _cache.clear();
      return;
    }
    _cache.forEach(function (_, key) {
      if (key.indexOf(prefix) === 0) _cache.delete(key);
    });
  }

  // Cached GET: return the cache on a hit; force=true skips the cache read, re-requests, and refreshes the cache (used by frontend refresh buttons). 带缓存的 GET：命中时直接返回缓存；force=true 时跳过缓存读，重新请求并刷新缓存（前端刷新按钮用）。
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
  // One profile = one account; accounts with multi-region subscriptions also have a second-level region menu (see the "working region" section below); a region is only a display field in the dropdown. Current-choice priority: memory -> localStorage -> default profile -> first. 一个 profile = 一个账号；多区订阅的账号还有二级区域菜单（见下面“工作区域”一节），区域只是下拉里的显示字段。当前选择优先级：内存 -> localStorage -> 默认 profile -> 第一个。
  var PROFILE_KEY = "cloud_active_profile";
  var _profiles = null;
  var _profilesDefault = null;
  var _currentProfile = null;

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

  // Fill the profile dropdown: options show displayName (home-region display name, with a region count suffix for multi-region accounts, e.g. "新加坡（3 个区）", matching the bot's switch menu; when the old API has no displayName, fall back to "name · region display name"); restore the saved choice and return the selected name (null when the list is empty; the caller loads with the default account). 填充 profile 下拉：选项显示 displayName（主区中文名，多区账号后缀区数，如“新加坡（3 个区）”，与机器人切换菜单一致；旧接口没有 displayName 时回退到“名字 · 区域中文名”）；恢复保存的选择，返回选中的名字（列表为空时返回 null，调用方按默认账号加载）。
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

  // Append ?profile= and ?region= to a URL (using & when a query string already exists). 给 URL 拼 ?profile= 与 ?region= 参数（已有查询串时用 & 拼接）。
  // When region is not passed, only profile is appended (the backend uses that account's home region). region 不传时只拼 profile（后端用该账号的主区）。
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
  // Accounts with multi-region subscriptions get a second-level region menu: the region dropdown only shows when the selected account subscribes to multiple regions (single-region accounts hide it, same as the bot). Region is bound to the account: switching accounts calls setRegion(null) first, then fillRegionSelect, resetting the region to the new account's home region; a saved choice outside the account's subscription list also falls back to the home region. 多区订阅的账号有二级区域菜单：区域下拉只在所选账号订阅了多个区域时显示（单区账号不显示，与机器人端一致）。区域与账号绑定：切账号时页面先 setRegion(null) 再调 fillRegionSelect，区域重置为新账号的主区；保存的选择不在该账号订阅列表里时也回落到主区。
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

  // An account's subscribed region list ([{code, label, isHome}]), from loadProfiles' cache; null when not loaded or the account has no regions. 某账号的订阅区域列表（[{code, label, isHome}]），来自 loadProfiles 的缓存；未加载或该账号无 regions 时返回 null。
  function profileRegions(profile) {
    if (!_profiles) return null;
    for (var i = 0; i < _profiles.length; i++) {
      if (_profiles[i].name === profile) return _profiles[i].regions || null;
    }
    return null;
  }

  // Fill the region dropdown: multi-region accounts show the dropdown and restore the saved choice (falling back to the home region when it is outside the subscription list); single-region accounts hide the dropdown (the select and its #region-wrap wrapper hide together). Return the selected region code (null when the subscription list is unknown; the caller treats it as the home region). 填充区域下拉：多区账号显示下拉并恢复保存的选择（不在订阅列表里时回落主区），单区账号隐藏下拉（select 与 #region-wrap 包裹层一起隐藏）。返回选中的区域代码（订阅列表未知时返回 null，调用方按主区处理）。
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
      // Fall back to the legacy method when the clipboard API is unavailable (e.g. non-secure contexts). clipboard API 不可用时（如非安全上下文）退回传统方式。
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

  // Time -> local "2026/11/23 02:30:00". 时间 -> 本地 "2026/11/23 02:30:00"。
  // Accepts an ISO string or a Unix timestamp (seconds or milliseconds; backend tasks use second-level timestamps). 接受 ISO 字符串，也接受 Unix 时间戳（秒或毫秒，后端任务用秒级时间戳）。
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

  function formatRate(kbps) {
    if (kbps == null || isNaN(kbps)) return "--";
    if (kbps >= 1024) return (kbps / 1024).toFixed(1) + " MB/s";
    return Math.round(kbps) + " KB/s";
  }

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
  // Create-backup flow (a core user requirement): 创建备份流程（核心用户要求）：
  //   1. Check the quota API first; create directly when not full. 1. 先调配额接口；未满直接创建。
  //   2. When full, pop a dialog showing the earliest backup; "delete earliest and continue" makes the backend delete the earliest backup first, then create; cancel does nothing. 2. 已满则弹窗提示最早备份，点“删除最早备份并继续”时后端先删最早备份再创建；点取消什么都不做。
  // Returns the backup object on success, null on failure/cancel; the caller refreshes the list after a success. 成功返回备份对象，失败/取消返回 null；调用方成功后自行刷新列表。
  async function backupBootVolume(volumeId, profile) {
    // Paid accounts have no 5-backup cap: create directly; on a free account the server auto-deletes the earliest backup and creates (no confirmation wait), and the response carries deleted_oldest saying which was deleted. The frontend no longer pre-checks the quota or pops a confirm dialog. 付费账号不限 5 个，直接创建；免费账号满了服务端自动删最早备份再建（不用等确认），响应里带 deleted_oldest 说明删了哪个。前端不再预查配额、不再弹窗确认。
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
  /* Fixed themes and their forced mode (only classic follows the light/dark switch). 固定明暗的主题（只有经典主题跟随明暗切换）。 */
  var FIXED_THEME_MODE = { starry: "dark", forest: "dark", ocean: "dark", obsidian: "dark", sunset: "light", sakura: "light", mint: "light", ice: "light", desert: "light" };
  function applyTheme(theme, mode) {
    var root = document.documentElement;
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-mode", FIXED_THEME_MODE[theme] || mode);
    try {
      localStorage.setItem("mbot-theme", theme);
      localStorage.setItem("mbot-mode", mode);
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
      // Compatible with two response formats: 兼容两种返回格式：
      // nested (what the backend actually returns): {cpu:{percent}, memory:{percent}, disk:{percent}, network:{rx_bytes_per_sec, tx_bytes_per_sec}} 嵌套（后端实际）：{cpu:{percent}, memory:{percent}, disk:{percent}, network:{rx_bytes_per_sec, tx_bytes_per_sec}}
      // flat (the draft contract): {cpu_percent, mem_percent, disk_percent, net_down_kbps, net_up_kbps} 扁平（契约草案）：{cpu_percent, mem_percent, disk_percent, net_down_kbps, net_up_kbps}
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
      // Resource bar failures do not bother the user; silently wait for the next poll. 资源栏失败不打扰用户，静默等待下次轮询。
    }
  }

  function highlightSidebar() {
    var sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    if (sidebar.querySelector("a.active")) return;
    var path = window.location.pathname.replace(/\/$/, "") || "/overview";
    var page = path.replace(/^\//, "");
    if (page === "") page = "overview";
    var link = sidebar.querySelector('[data-page="' + page + '"]');
    if (link) link.classList.add("active");
  }

  // Username fallback: when the template did not pass a username, call /api/auth/me to fill it in. 用户名兜底：模板没传 username 时调 /api/auth/me 补填。
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
        var fixed = FIXED_THEME_MODE[currentTheme()];
        if (fixed) {
          toast(fixed === "dark" ? "该主题固定为暗色" : "该主题固定为亮色", "info");
          return;
        }
        applyTheme(currentTheme(), currentMode() === "dark" ? "light" : "dark");
      });
    }
    var settingsBtn = document.getElementById("settings-btn");
    if (settingsBtn) {
      settingsBtn.addEventListener("click", function () {
        // The change-password modal lives in base.html (settings.js handles the submit); pages without that modal, like the login page, keep the original prompt. 改密码弹窗在 base.html 里（settings.js 处理提交）；登录页等没有该弹窗的页面保持原提示。
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

    // Mobile nav drawer: the hamburger button toggles the sidebar; tapping the backdrop or a nav link closes it (on desktop .nav-toggle is display: none and unreachable, so this does nothing). 手机端导航抽屉：汉堡按钮开关侧边栏，点遮罩或导航链接关闭（桌面端 .nav-toggle 是 display: none，点不到，这段不生效）。
    var navToggle = document.getElementById("nav-toggle");
    var navSidebar = document.querySelector(".sidebar");
    var navBackdrop = document.getElementById("sidebar-backdrop");
    function closeNavDrawer() {
      if (navSidebar) navSidebar.classList.remove("open");
      if (navBackdrop) navBackdrop.hidden = true;
      if (navToggle) navToggle.setAttribute("aria-expanded", "false");
    }
    if (navToggle && navSidebar) {
      navToggle.addEventListener("click", function () {
        var open = navSidebar.classList.toggle("open");
        if (navBackdrop) navBackdrop.hidden = !open;
        navToggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
    if (navBackdrop) navBackdrop.addEventListener("click", closeNavDrawer);
    if (navSidebar) {
      navSidebar.querySelectorAll("a").forEach(function (a) {
        a.addEventListener("click", closeNavDrawer);
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeNavDrawer();
    });

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

    highlightSidebar();
    fillUsername();

    if (document.getElementById("gauge-cpu")) {
      refreshSystem();
      setInterval(refreshSystem, 5000);
    }
  });

  /* 暴露给各页面脚本 */
  window.MBot = {
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
