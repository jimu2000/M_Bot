/* M_Bot Oracle开源管理面板 · 中英文切换（v2.01）
 * 顶栏 #lang-select（内页）与 #login-lang-select（登录页）共用同一套逻辑：
 * 默认简体中文；切换后整站静态文案（导航、按钮、标题、提示）与 JS 动态
 * 渲染的文案（云报告表格、实例状态徽标等）跟着变；选择存 localStorage
 *（键 mbot-lang），刷新不丢。动态页面监听 window 的 mbot-lang-change
 * 事件后自行重渲染（概览页重拉云报告、实例页重拉列表）。
 * 注意：英文模式下 OCI 原始状态码（RUNNING 等）保持原文显示，只在
 * 中文模式下映射成中文（见 instanceStateLabel）。
 * 另有“跟随浏览器语言”（auto）：按 navigator.language 在中文与英文
 * 之间切换，其他语言一律回落英文；模式本身也存 localStorage。
 * "Follow browser language" (auto) switches between Chinese and
 * English from navigator.language; any other browser language falls
 * back to English. The mode itself is also persisted. */
(function () {
  "use strict";

  var LANG_KEY = "mbot-lang";
  var MODE_AUTO = "auto";
  var SUPPORTED_LANGS = ["zh-CN", "en"];

  /* 翻译字典：键 -> {zh, en}。英文要准确自然，不要机翻腔；
   * 新增 data-i18n 的元素必须在这里补一对文案，否则切英文会留白。 */
  var I18N = {
    /* ---- 顶栏 / 导航 ---- */
    "app.subtitle": { zh: "Oracle管理面板", en: "Oracle Panel" },
    "app.logout": { zh: "退出", en: "Log out" },
    "app.settings": { zh: "设置", en: "Settings" },
    "app.language": { zh: "语言", en: "Language" },
    "app.language_follow": { zh: "跟随浏览器语言", en: "Follow browser language" },
    "app.theme": { zh: "主题", en: "Theme" },
    "app.theme_classic": { zh: "经典", en: "Classic" },
    "app.theme_starry": { zh: "星夜", en: "Starry" },
    "app.theme_forest": { zh: "森林", en: "Forest" },
    "app.theme_ocean": { zh: "海洋", en: "Ocean" },
    "app.theme_obsidian": { zh: "曜石", en: "Obsidian" },
    "app.theme_sunset": { zh: "落日", en: "Sunset" },
    "app.theme_sakura": { zh: "樱花", en: "Sakura" },
    "app.theme_mint": { zh: "薄荷", en: "Mint" },
    "app.theme_ice": { zh: "冰蓝", en: "Ice" },
    "app.theme_desert": { zh: "沙漠", en: "Desert" },
    "app.toggle_mode": { zh: "切换明暗", en: "Toggle light / dark" },
    "app.open_nav": { zh: "打开导航", en: "Open navigation" },
    "app.github": { zh: "GitHub 项目", en: "GitHub Project" },
    "nav.hosts": { zh: "主机", en: "Hosts" },
    "nav.instances": { zh: "实例管理", en: "Instances" },
    "nav.accounts": { zh: "云账户", en: "Cloud Accounts" },
    "nav.volumes": { zh: "启动盘", en: "Boot Volumes" },
    "nav.create": { zh: "创建实例", en: "Create Instance" },
    "nav.quota": { zh: "配额", en: "Quota" },
    "nav.tasks": { zh: "任务管理", en: "Tasks" },
    "nav.gfw": { zh: "GFW 换 IP", en: "GFW IP Change" },
    "nav.bark": { zh: "Bark 通知", en: "Bark Notifications" },
    "nav.dns": { zh: "DNS 管理", en: "DNS" },
    "nav.objectstorage": { zh: "对象存储", en: "Object Storage" },
    "nav.email": { zh: "Email", en: "Email" },
    "nav.cert": { zh: "证书管理", en: "Certificates" },
    "nav.inspect": { zh: "巡检计划", en: "Inspection" },
    "nav.rescue": { zh: "救机", en: "Rescue" },
    /* ---- 改密码弹窗 ---- */
    "settings.change_password": { zh: "修改面板密码", en: "Change Panel Password" },
    "settings.old_password": { zh: "旧密码", en: "Current password" },
    "settings.new_password": { zh: "新密码", en: "New password" },
    "settings.confirm_password": { zh: "确认新密码", en: "Confirm new password" },
    "settings.hint_password": { zh: "至少 8 位，且同时包含字母和数字。", en: "At least 8 characters, with both letters and numbers." },
    "settings.hint_restart": { zh: "修改后需重启 Web 面板服务，新密码才生效。", en: "Takes effect after the web panel service restarts." },
    "common.cancel": { zh: "取消", en: "Cancel" },
    "common.close": { zh: "关闭", en: "Close" },
    "common.confirm": { zh: "确认修改", en: "Confirm" },
    /* ---- 登录页 ---- */
    "login.subtitle": { zh: "登录到你的账号", en: "Sign in to your account" },
    "login.tab_password": { zh: "密码登录", en: "Password Login" },
    "login.tab_telegram": { zh: "Telegram", en: "Telegram" },
    "login.username": { zh: "用户名", en: "Username" },
    "login.password": { zh: "密码", en: "Password" },
    "login.captcha": { zh: "图形验证码", en: "CAPTCHA" },
    "login.remember": { zh: "记住我", en: "Remember me" },
    "login.forgot": { zh: "忘记密码？", en: "Forgot password?" },
    "login.submit": { zh: "登录", en: "Log in" },
    "login.tg_note": { zh: "验证码将发送到白名单用户的 Telegram（需面板服务器已配置机器人）。", en: "The code will be sent to the allowlisted user's Telegram (the panel server must have a bot configured)." },
    "login.tg_send": { zh: "发送验证码", en: "Send code" },
    "login.tg_code": { zh: "6 位验证码", en: "6-digit code" },
    "login.tg_submit": { zh: "验证码登录", en: "Log in with code" },
    "login.default_cred": { zh: "默认凭据请查看部署文档", en: "See the deployment docs for the default credentials" },
    "login.github_project": { zh: "GitHub 免费开源项目", en: "GitHub Free Open Source Project" },
    /* ---- 概览页 / 云报告 ---- */
    "overview.saved": { zh: "已保存会话", en: "Saved Sessions" },
    "overview.search_ph": { zh: "按名称 / IP 搜索", en: "Search by name / IP" },
    "overview.add_host": { zh: "＋ 添加主机", en: "＋ Add Host" },
    "overview.add_host_plain": { zh: "添加主机", en: "Add Host" },
    "overview.cloud_report": { zh: "云报告", en: "Cloud Report" },
    "overview.refresh": { zh: "↻ 刷新", en: "↻ Refresh" },
    "overview.loading": { zh: "加载中…", en: "Loading…" },
    "overview.no_account": { zh: "暂无已配置账号。", en: "No accounts configured yet." },
    "overview.load_failed": { zh: "云报告加载失败：", en: "Failed to load cloud report: " },
    "overview.generated": { zh: "生成于 ", en: "Generated " },
    "overview.query_failed": { zh: "查询失败：", en: "Query failed: " },
    "overview.no_hosts": { zh: "还没有保存的主机", en: "No saved hosts yet" },
    "overview.add_hint": { zh: "点右上角「添加主机」，把常用服务器保存下来，一键连终端。", en: "Click “Add Host” at the top right to save a server you use often, then connect to its terminal in one click." },
    "overview.retry": { zh: "重试", en: "Retry" },
    "overview.load_hosts_failed": { zh: "加载主机列表失败：", en: "Failed to load hosts: " },
    "overview.no_match": { zh: "没有匹配的主机，换个关键词试试。", en: "No matching hosts — try a different keyword." },
    "overview.never_used": { zh: "从未使用", en: "Never used" },
    "overview.auth_key": { zh: "密钥", en: "Key" },
    "overview.auth_password": { zh: "密码", en: "Password" },
    "overview.connect": { zh: "▶ 连接", en: "▶ Connect" },
    "overview.edit": { zh: "编辑", en: "Edit" },
    "overview.delete": { zh: "删除", en: "Delete" },
    "overview.copy": { zh: "⧉ 复制", en: "⧉ Copy" },
    "overview.copy_addr": { zh: "复制地址", en: "Copy address" },
    "overview.add_host_title": { zh: "添加主机", en: "Add Host" },
    "overview.edit_host_title": { zh: "编辑主机", en: "Edit Host" },
    "cloud.th_region": { zh: "区域", en: "Region" },
    "cloud.th_instances": { zh: "实例", en: "Instances" },
    "cloud.th_volumes": { zh: "启动盘", en: "Boot Volumes" },
    "cloud.th_a1": { zh: "A1", en: "A1" },
    "cloud.th_e2": { zh: "E2", en: "E2" },
    "cloud.unit_instances": { zh: "台", en: "instances" },
    "cloud.unit_volumes": { zh: "块", en: "volumes" },
    "cloud.unit_cores": { zh: "核", en: "cores" },
    /* ---- 实例页 ---- */
    "instances.title": { zh: "实例管理", en: "Instance Management" },
    "instances.profile": { zh: "Profile", en: "Profile" },
    "instances.region": { zh: "区域", en: "Region" },
    "instances.refresh": { zh: "↻ 刷新", en: "↻ Refresh" },
    "instances.create": { zh: "＋ 创建实例", en: "＋ Create Instance" },
    "instances.select_all": { zh: "全选", en: "Select all" },
    "instances.selected": { zh: "已选 {n} 台", en: "{n} selected" },
    "instances.loading": { zh: "加载中…", en: "Loading…" },
    "instances.empty": { zh: "该 Profile 下没有实例", en: "No instances under this profile" },
    "instances.batch_start": { zh: "▶️ 批量开机", en: "▶️ Batch Start" },
    "instances.batch_stop": { zh: "⏹️ 批量关机", en: "⏹️ Batch Stop" },
    "instances.batch_restart": { zh: "🔄 批量重启", en: "🔄 Batch Restart" },
    "instances.batch_terminate": { zh: "🗑️ 批量删除", en: "🗑️ Batch Delete" },
    "instances.terminate": { zh: "终止实例", en: "Terminate Instance" },
    "instances.terminate_preserve": { zh: "终止实例（保留启动盘）", en: "Terminate Instance (Keep Boot Volume)" },
    "instances.confirm_terminate": { zh: "确认终止", en: "Confirm Termination" },
    "instances.confirm_terminate_preserve": { zh: "确认终止（保留启动盘）", en: "Confirm (Keep Boot Volume)" },
    "instances.batch_terminate_title": { zh: "批量删除实例", en: "Batch Delete Instances" },
    "instances.rename": { zh: "更改实例名", en: "Rename Instance" },
    "instances.reinstall": { zh: "重装系统", en: "Reinstall OS" },
    "instances.console": { zh: "串行控制台", en: "Serial Console" },
    "instances.change_ip": { zh: "换公网 IP", en: "Change Public IP" },
    "instances.ip_manage": { zh: "IP 管理", en: "IP Management" },
    "instances.boot_volume": { zh: "启动盘", en: "Boot Volume" },
    "instances.resize": { zh: "升 / 降配", en: "Resize" },
    "instances.repair": { zh: "尝试修复", en: "Try Repair" },
    "instances.detail": { zh: "实例详情", en: "Instance Details" },
    "instances.bootswap": { zh: "引导卷恢复", en: "Restore Boot Volume" },
    "instances.bootbackup_del": { zh: "删除历史备份", en: "Delete Old Backups" },
    /* ---- 绑定域名（实例详情，v2.01） ---- */
    "instances.bind_domain": { zh: "🌐 绑定域名", en: "🌐 Bind Domain" },
    "instances.bind_domain_title": { zh: "绑定域名", en: "Bind Domain" },
    "instances.bind_domain_zone": { zh: "主域名", en: "Main domain" },
    "instances.bind_domain_subdomain": { zh: "二级域名", en: "Subdomain" },
    "instances.bind_domain_subdomain_hint": { zh: "如 www、app；@ 表示根域名", en: "e.g. www, app; @ means the zone apex" },
    "instances.bind_domain_submit": { zh: "确认绑定", en: "Bind" },
    "instances.bind_domain_overwrite": { zh: "确认覆盖", en: "Confirm Overwrite" },
    "instances.bind_domain_loading_zones": { zh: "加载 Zone 中…", en: "Loading zones…" },
    "instances.bind_domain_no_ip": { zh: "该实例没有公网 IP，无法绑定域名。", en: "This instance has no public IP, so no domain can be bound to it." },
    "instances.bind_domain_no_credentials": { zh: "Cloudflare 凭据未配置，请先在 DNS 管理页配置凭据。", en: "Cloudflare credentials are not configured. Set them up on the DNS page first." },
    "instances.bind_domain_invalid": { zh: "二级域名格式不对：只允许字母、数字、-、_、.，或单独一个 @。", en: "Invalid subdomain: only letters, digits, -, _ and . are allowed, or a single @." },
    "instances.bind_domain_exists": { zh: "{fqdn} 已有一条 A 记录指向 {old_ip}，确认要改成指向 {ip} 吗？", en: "{fqdn} already has an A record pointing to {old_ip}. Change it to point to {ip}?" },
    "instances.bind_domain_exists_same": { zh: "✅ {fqdn} 已指向 {ip}，不用再建。", en: "✅ {fqdn} already points to {ip}; nothing to create." },
    "instances.bind_domain_done": { zh: "✅ 已绑定：{fqdn} → {ip}。DNS 传播需要一点时间（通常几分钟），请稍后再访问验证。", en: "✅ Bound: {fqdn} → {ip}. DNS propagation takes a while (usually a few minutes); please check again later." },
    "instances.bind_domain_updated": { zh: "✅ 已更新：{fqdn} → {ip}。DNS 传播需要一点时间（通常几分钟），请稍后再访问验证。", en: "✅ Updated: {fqdn} → {ip}. DNS propagation takes a while (usually a few minutes); please check again later." },
    /* ---- 创建页 ---- */
    "create.title": { zh: "创建实例", en: "Create Instance" },
    "create.basic": { zh: "🖥️ 基础配置", en: "🖥️ Basic Settings" },
    "create.system": { zh: "⚙️ 系统配置", en: "⚙️ System" },
    "create.network": { zh: "🛡️ 网络与安全", en: "🛡️ Network & Security" },
    "create.advanced": { zh: "⌨️ 高级选项", en: "⌨️ Advanced Options" },
    "create.quick_boot": { zh: "⚡ 快捷开机", en: "⚡ Quick Boot" },
    "create.submit": { zh: "开始创建", en: "Create" }
  };

  /* 实例状态中文映射（v2.01 A1）：只在中文模式下用；英文模式与
   * 未知状态都兜底显示 OCI 原文，不改后端数据。 */
  var INSTANCE_STATE_ZH = {
    RUNNING: "运行中",
    TERMINATED: "已终止",
    STOPPED: "已停止",
    PROVISIONING: "创建中",
    STARTING: "启动中",
    STOPPING: "停止中",
    TERMINATING: "终止中",
    CREATING: "创建中"
  };

  /* 启动盘状态中文映射（实例页格子用，同上：英文模式显示原文）。 */
  var VOLUME_STATE_ZH = {
    AVAILABLE: "可用",
    "IN-USE": "使用中",
    CREATING: "创建中",
    PROVISIONING: "配置中",
    RESTORING: "恢复中",
    REQUEST_RECEIVED: "已接收请求",
    TERMINATED: "已终止",
    FAULTY: "故障",
    ACTIVE: "启用",
    INACTIVE: "停用",
    DELETED: "已删除"
  };

  /* 任务状态中文映射（任务管理页状态徽标用，同上：英文模式显示原文）。 */
  var TASK_STATUS_ZH = {
    RUNNING: "运行中",
    PENDING: "等待中",
    IN_PROGRESS: "进行中",
    DONE: "已完成",
    SUCCESS: "成功",
    COMPLETED: "已完成",
    FAILED: "失败",
    ERROR: "出错",
    CANCELLED: "已取消",
    CANCELED: "已取消",
    UNKNOWN: "未知"
  };

  var _mode = null;  // In-memory cache: the mode the user picked ("auto" / "zh-CN" / "en"). 内存缓存：用户选的模式（"auto" / "zh-CN" / "en"）。

  /* 跟随模式按浏览器语言取生效语言：只认中文（zh 开头，如 zh-CN、
   * zh-TW）与英文（en 开头，如 en-US、en-GB）；俄文、日文、法文等
   * 其他语言面板没有翻译，一律回落英文，面板只在中英之间切换。
   * Follow mode resolves the browser language: only Chinese (zh-*)
   * and English (en-*) are recognised; any other language (Russian,
   * Japanese, French, …) falls back to English, because the panel has
   * no translations for it — follow mode only switches between
   * Chinese and English, never to an unsupported language. */
  function resolveBrowserLang() {
    var nav = "";
    try {
      nav = (window.navigator && (window.navigator.language || window.navigator.userLanguage)) || "";
    } catch (e) { /* navigator 不可用时走回落 */ }
    nav = String(nav).toLowerCase();
    if (nav.indexOf("zh") === 0) return "zh-CN";
    if (nav.indexOf("en") === 0) return "en";
    return "en";
  }

  function getMode() {
    if (_mode) return _mode;
    try {
      var saved = window.localStorage.getItem(LANG_KEY);
      if (saved === MODE_AUTO || saved === "en" || saved === "zh-CN") {
        _mode = saved;
        return _mode;
      }
    } catch (e) { /* 隐私模式下 localStorage 不可用，用默认中文 */ }
    _mode = "zh-CN";
    return _mode;
  }

  function getLang() {
    return getMode() === MODE_AUTO ? resolveBrowserLang() : getMode();
  }

  function t(key, vars) {
    var entry = I18N[key];
    var text = entry ? (getLang() === "en" ? entry.en : entry.zh) : key;
    if (vars) {
      Object.keys(vars).forEach(function (k) {
        text = text.split("{" + k + "}").join(String(vars[k]));
      });
    }
    return text;
  }

  /* 实例状态显示文字：中文模式查映射表，未知状态与英文模式显示原文。 */
  function instanceStateLabel(state) {
    var s = state == null ? "" : String(state);
    if (getLang() === "en") return s;
    return INSTANCE_STATE_ZH[s.toUpperCase()] || s;
  }

  function volumeStateLabel(state) {
    var s = state == null ? "" : String(state);
    if (getLang() === "en") return s;
    return VOLUME_STATE_ZH[s.toUpperCase()] || s;
  }

  /* 任务状态显示文字：中文模式查映射表，未知状态与英文模式显示原文。 */
  function taskStatusLabel(status) {
    var s = status == null ? "" : String(status);
    if (getLang() === "en") return s;
    return TASK_STATUS_ZH[s.toUpperCase()] || s;
  }

  /* 把一处翻译写进元素：有子元素（如侧边栏链接里的 svg 图标）时只换
   * 直接文本节点，不动子元素；纯文本元素直接换 textContent。 */
  function applyToElement(el, text) {
    var hasElementChild = false;
    for (var i = 0; i < el.childNodes.length; i++) {
      if (el.childNodes[i].nodeType === 1) { hasElementChild = true; break; }
    }
    if (!hasElementChild) {
      el.textContent = text;
      return;
    }
    var replaced = false;
    for (var j = 0; j < el.childNodes.length; j++) {
      var node = el.childNodes[j];
      if (node.nodeType === 3) {
        if (!replaced && node.nodeValue.trim() !== "") {
          node.nodeValue = text;
          replaced = true;
        } else if (replaced) {
          node.nodeValue = "";
        }
      }
    }
    if (!replaced) el.appendChild(document.createTextNode(text));
  }

  function applyI18n() {
    var lang = getLang();
    var mode = getMode();
    document.documentElement.setAttribute("lang", lang === "en" ? "en" : "zh-CN");
    document.querySelectorAll("#lang-select, #login-lang-select").forEach(function (sel) {
      sel.value = mode;
    });
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      applyToElement(el, t(el.getAttribute("data-i18n")));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    document.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    document.querySelectorAll("[data-i18n-aria]").forEach(function (el) {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });
  }

  function setLang(lang) {
    var next;
    if (lang === MODE_AUTO) {
      next = MODE_AUTO;
    } else {
      next = (lang === "en" || lang === "en-US" || lang === "en-GB") ? "en" : "zh-CN";
    }
    var changed = next !== getMode();
    _mode = next;
    try {
      window.localStorage.setItem(LANG_KEY, next);
    } catch (e) { /* 隐私模式下 localStorage 不可用 */ }
    applyI18n();
    if (!changed) return;
    /* 通知各页面重渲染动态文案（云报告表格、实例列表等）。 */
    try {
      window.dispatchEvent(new CustomEvent("mbot-lang-change", { detail: { lang: getLang(), mode: next } }));
    } catch (e) {
      var evt = document.createEvent("Event");
      evt.initEvent("mbot-lang-change", true, true);
      window.dispatchEvent(evt);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyI18n();
    document.querySelectorAll("#lang-select, #login-lang-select").forEach(function (sel) {
      sel.addEventListener("change", function () { setLang(sel.value); });
    });
  });

  window.MBotI18n = {
    LANG_KEY: LANG_KEY,
    MODE_AUTO: MODE_AUTO,
    SUPPORTED_LANGS: SUPPORTED_LANGS,
    I18N: I18N,
    INSTANCE_STATE_ZH: INSTANCE_STATE_ZH,
    VOLUME_STATE_ZH: VOLUME_STATE_ZH,
    TASK_STATUS_ZH: TASK_STATUS_ZH,
    getLang: getLang,
    getMode: getMode,
    resolveBrowserLang: resolveBrowserLang,
    setLang: setLang,
    t: t,
    applyI18n: applyI18n,
    instanceStateLabel: instanceStateLabel,
    volumeStateLabel: volumeStateLabel,
    taskStatusLabel: taskStatusLabel
  };
})();
