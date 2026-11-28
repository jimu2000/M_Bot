/* 实例管理逻辑：M_Bot 风格卡片布局（每台实例一张卡片）、启动/停止/重启/
   终止（含保留启动盘）、批量操作、改名、重装、串行控制台、IP 管理弹窗
   （换 IP / 附加 IPv6 / 删除 IPv6）、启动盘弹窗（详情 + 扩容）、
   升降配、尝试修复、实例详情弹窗 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var instances = [];
  var volumesByInstance = {};  // Boot volumes: instance id -> /api/volumes entry (used by card rendering). 启动盘：实例 id -> /api/volumes 条目（卡片显示用）。
  var selected = {};
  var currentProfile = null;
  var currentRegion = null;  // Current working region, restored by fillRegionSelect (only multi-region accounts have the second dropdown). 当前工作区域，由 fillRegionSelect 恢复（多区账号才有二级下拉）。
  var loading = true;  // While loading, the card area shows placeholders instead of flashing stale data. 加载中时卡片区显示占位，不闪旧数据。
  var terminateTarget = null;
  var terminatePreserve = false;
  var renameTarget = null;
  var reinstallTarget = null;
  var changeIpTarget = null;
  var ipTarget = null;
  var bootVolumeTarget = null;  // Boot-volume modal's current instance {instance, volumeId, size}. 启动盘弹窗当前实例 {instance, volumeId, size}。
  var resizeTarget = null;
  var detailTarget = null;  // Detail modal's current instance {id, name, state}. 详情弹窗当前实例 {id, name, state}。
  var bootswapTarget = null;
  var bootswapSelected = null;
  var bootBackupDelTarget = null;
  var detailLoadSeq = 0;  // Detail-load sequence: guards against out-of-order responses when the user opens another instance's detail while one is still loading. 详情加载序号：用户快速连点两个实例时，旧请求的回包不再覆盖新弹窗。
  var cpuRefreshTimer = null;  // Timer for the delayed CPU-model refetch (at most one refetch per load round). CPU 型号延迟补拉的定时器（每轮只补拉一次）。
  var consoleCommand = "";

  // Short i18n lookup with a Chinese fallback (used for card buttons and action toasts, which are built in JS and must follow the language switch). 简短 i18n 取值（中文兜底；卡片按钮与操作提示在 JS 里拼，必须跟随语言切换）。
  function tr(key, fallback) {
    return window.MBotI18n ? window.MBotI18n.t(key) : fallback;
  }

  function batchLabel(action) {
    if (action === "start") return tr("instances.batch_start", "▶️ 批量开机");
    if (action === "stop") return tr("instances.batch_stop", "⏹️ 批量停止");
    if (action === "restart") return tr("instances.batch_restart", "🔄 批量重启");
    if (action === "terminate") return tr("instances.batch_terminate", "🗑️ 批量删除");
    return action;
  }

  // Field normalization: the backend returns panel shapes (camelCase) {id, name, publicIp, shape, state, ocpus, memoryInGBs, region, timeCreated}; during the transition both old and new fields are written, and new fields win here with old ones as fallback. 字段归一化：后端返回面板形状（camelCase）{id, name, publicIp, shape, state, ocpus, memoryInGBs, region, timeCreated}，过渡期同时双写旧字段，这里优先认新字段，旧字段作兜底。
  // Note: the list only contains RUNNING instances. 注意：列表只含 RUNNING 实例。
  function norm(inst) {
    return {
      id: inst.id,
      name: inst.name || inst["display-name"] || inst.display_name || "—",
      shape: inst.shape || "—",
      state: inst.state || inst["lifecycle-state"] || "",
      ocpus: inst.ocpus != null ? inst.ocpus
        : (inst["shape-config"] && inst["shape-config"].ocpus),
      memory_gb: inst.memoryInGBs != null ? inst.memoryInGBs
        : (inst.memory_gb != null ? inst.memory_gb
        : (inst["shape-config"] && inst["shape-config"]["memory-in-gbs"])),
      public_ip: inst.publicIp || inst["public-ip"] || inst.public_ip || null,
      created: inst.timeCreated || inst["time-created"] || inst.time_created || null,
    };
  }

  // State badge: always the OCI state code (STOPPED / STOPPING / RUNNING …) in both languages, so the card matches the OCI-style states shown elsewhere on the panel. The cloud report keeps its Chinese labels (v2.0 decision); only the instance cards use raw codes. 状态徽标：中英文都显示 OCI 原始状态码（STOPPED / STOPPING / RUNNING…），与面板其他英文状态一致；云报告仍用中文（v2.0 决定），只有实例卡片用原始码。
  function stateBadge(state) {
    var s = (state || "").toUpperCase();
    var label = s || "—";
    if (s === "RUNNING") return '<span class="badge badge-green">' + MBot.escapeHtml(label) + "</span>";
    if (s === "STOPPED") return '<span class="badge badge-gray">' + MBot.escapeHtml(label) + "</span>";
    if (s === "TERMINATED") return '<span class="badge badge-red">' + MBot.escapeHtml(label) + "</span>";
    return '<span class="badge badge-amber">' + MBot.escapeHtml(label || "—") + "</span>";
  }

  // E2.1.Micro is followed by "永久免费" (permanently free). E2.1.Micro 后面加“永久免费”。
  function fmtShape(shape) {
    if (shape === "VM.Standard.E2.1.Micro" || shape === "E2.1.Micro") {
      return shape + "永久免费";
    }
    return shape;
  }

  // CPU model display: A1 always shows the free-tier label (the chip is Ampere Altra on every A1, so the quota note is what matters); E2 shows the detected model, and falls back to the full "AMD EPYC 7J13" (M_Bot only keeps 7J13 E2s, so a bare "AMD" never appears). Unknown shapes show "未检测". CPU 型号显示：A1 一律显示免费额度标注（A1 的芯片都是 Ampere Altra，重要的是配额信息）；E2 显示检测到的型号，未检测到时回落到完整型号 "AMD EPYC 7J13"（M_Bot 只留 7J13 的 E2，不再出现光秃秃的 "AMD"）。未知形状显示“未检测”。
  function cpuModelLabel(model, shape) {
    var s = shape || "";
    if (s.indexOf("A1") !== -1) {
      return window.MBotI18n
        ? window.MBotI18n.t("instances.cpu_a1_free")
        : "A1配额内免费";
    }
    if (model) return model;
    if (s.indexOf("E2") !== -1) return "AMD EPYC 7J13";
    return "未检测";
  }

  function bootVolumeCell(inst) {
    var esc = MBot.escapeHtml;
    var vol = volumesByInstance[String(inst.id)];
    if (!vol) return "—";
    var size = vol.sizeInGBs != null ? vol.sizeInGBs + " GB" : "—";
    var vpu = vol.vpusPerGB != null ? " · VPU " + vol.vpusPerGB : "";
    var state = (vol.lifecycleState || vol["lifecycle-state"] || "").toUpperCase();
    var stateHtml = "";
    if (state === "AVAILABLE") {
      var availLabel = window.MBotI18n ? window.MBotI18n.volumeStateLabel(state) : state;
      stateHtml = '<span class="badge badge-green">' + esc(availLabel) + "</span>";
    } else if (state) {
      var otherLabel = window.MBotI18n ? window.MBotI18n.volumeStateLabel(state) : state;
      stateHtml = '<span class="badge badge-amber">' + esc(otherLabel) + "</span>";
    }
    var attachedHtml = vol.attached
      ? '<span class="badge badge-blue">已附加</span>' : "";
    return '<span class="tag-chip">' + esc(size + vpu) + "</span> "
      + stateHtml + " " + attachedHtml;
  }

  function selectedIds() {
    return Object.keys(selected);
  }

  function selectedInstances() {
    return instances.filter(function (raw) {
      return Object.prototype.hasOwnProperty.call(selected, String(raw.id));
    });
  }

  function updateBatchBar() {
    var count = selectedIds().length;
    var batchEl = document.getElementById("batch-count");
    batchEl.textContent = window.MBotI18n
      ? window.MBotI18n.t("instances.selected", { n: count })
      : "已选 " + count + " 台";
    var buttons = document.querySelectorAll("[data-batch]");
    buttons.forEach(function (btn) { btn.disabled = count === 0; });
    var selectAll = document.getElementById("select-all");
    if (selectAll) {
      selectAll.checked = instances.length > 0 && count === instances.length;
    }
  }

  function render() {
    var grid = document.getElementById("instance-grid");
    var empty = document.getElementById("instance-empty");
    if (loading) {
      grid.innerHTML = '<p class="instance-loading">加载中…</p>';
      empty.hidden = true;
      updateBatchBar();
      return;
    }
    empty.hidden = instances.length !== 0;
    if (instances.length === 0) {
      grid.innerHTML = "";
      updateBatchBar();
      return;
    }
    grid.innerHTML = instances.map(function (raw) {
      var esc = MBot.escapeHtml;
      var inst = norm(raw);
      var state = (inst.state || "").toUpperCase();
      var running = state === "RUNNING";
      var stopped = state === "STOPPED";
      var ip = inst.public_ip || "—";
      var ipCell = inst.public_ip
        ? '<span class="mono">' + esc(ip) + '</span> ' +
          '<button type="button" class="copy-btn" data-copy="' + esc(ip) + '" title="复制 IP">⧉</button>'
        : "—";
      var spec = (inst.ocpus != null && inst.memory_gb != null)
        ? inst.ocpus + " OCPU / " + inst.memory_gb + " GB" : "—";
      var created = inst.created ? esc(MBot.formatTime(inst.created)) : "—";
      // Button matrix (fixed order): power off/on, restart, serial console, rename, resize, repair, reset image, IP management, boot volume, delete, delete (keep boot volume). 按钮矩阵（固定顺序）：停止/启动、重启、串行控制台、重命名、升降级、修复、重置镜像、IP 管理、启动盘、删除、删除（保留启动盘）。
      // Power/restart follow the instance state; resize only shows for A1.Flex. 启停/重启按状态显示；升降级只对 A1.Flex 显示。
      var actions = "";
      if (stopped) actions += '<button type="button" class="btn btn-sm btn-primary" data-act="start">' + esc(tr("instances.start", "启动")) + "</button>";
      if (running) {
        actions += '<button type="button" class="btn btn-sm" data-act="stop">' + esc(tr("instances.stop", "停止")) + "</button>";
        actions += '<button type="button" class="btn btn-sm" data-act="restart">' + esc(tr("instances.restart", "重启")) + "</button>";
      }
      actions += '<button type="button" class="btn btn-sm" data-act="console">串行控制台</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="rename">重命名</button>';
      if (inst.shape === "VM.Standard.A1.Flex") {
        actions += '<button type="button" class="btn btn-sm" data-act="resize">升降级</button>';
      }
      actions += '<button type="button" class="btn btn-sm" data-act="repair">修复</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="reinstall">重置镜像</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="ip-manage">IP 管理</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="boot-volume">启动盘</button>';
      actions += '<button type="button" class="btn btn-sm btn-danger" data-act="terminate">删除</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="terminate-keep">删除（保留启动盘）</button>';
      var checked = Object.prototype.hasOwnProperty.call(selected, String(inst.id))
        ? " checked" : "";
      return (
        '<article class="card instance-card" data-instance-id="' + esc(inst.id) + '">' +
          '<div class="instance-card-head">' +
            '<input type="checkbox" class="row-select" data-select="' + esc(inst.id) + '"' + checked + ' aria-label="选择实例">' +
            '<button type="button" class="name-btn instance-card-name" data-act="detail" title="查看详情">' + esc(inst.name) + "</button>" +
            stateBadge(inst.state) +
          "</div>" +
          '<div class="instance-meta">' +
            '<div class="instance-meta-row"><span class="instance-meta-label">公网 IP</span><span class="instance-meta-value">' + ipCell + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">Shape</span><span class="instance-meta-value mono">' + esc(fmtShape(inst.shape)) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">CPU 型号</span><span class="instance-meta-value mono" data-cpu-model="1">' + esc(cpuModelLabel(inst.cpuModel, inst.shape)) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">规格</span><span class="instance-meta-value">' + esc(spec) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">启动盘</span><span class="instance-meta-value">' + bootVolumeCell(inst) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">创建时间</span><span class="instance-meta-value">' + created + "</span></div>" +
          "</div>" +
          '<button type="button" class="btn btn-primary instance-ssh" data-act="ssh">⌨️ SSH</button>' +
          '<div class="instance-actions">' + actions + "</div>" +
        "</article>"
      );
    }).join("");
    updateBatchBar();
  }

  // Load the instance list (instance requests bypass the frontend cache and re-fetch on every page entry: the CPU model is only detected after the backend receives its first request, so a cached old empty value would leave cards stuck on "not detected"; the backend's 30s server cache is protection enough. force=true also sends refresh=1 to bypass the server cache). The backend fetches public IPs in parallel; the first load takes ~3-5s. 加载实例列表（实例请求不走前端缓存，每次进页重新拉：CPU 型号是后端收到第一次请求后才触发检测的，缓存里的旧空值会让卡片一直显示“未检测”；后端 30 秒服务端缓存已足够保护。force=true 时再带 refresh=1 绕过服务端缓存）。后端并行取公网 IP，首次约 3-5 秒。
  // The instance and boot-volume requests go out in parallel (boot volumes reuse the volumes page's "vol:list:" cache key) and volumes are joined onto cards by instanceId; a volume fetch failure degrades (cards show "—") without affecting the instance list. Results arriving while switching account/region are discarded, never overwriting new data. 实例与启动盘两个请求并行发（启动盘复用 volumes 页的 "vol:list:" 缓存键），启动盘按 instanceId 拼到卡片上；启动盘拉取失败时降级（卡片显示"—"），不影响实例列表。切账号/区域期间旧请求的结果直接丢弃，不覆盖新数据。
  async function loadInstances(force) {
    if (cpuRefreshTimer) {
      clearTimeout(cpuRefreshTimer);
      cpuRefreshTimer = null;
    }
    var profileAtStart = currentProfile;
    var regionAtStart = currentRegion;
    if (force) {
      loading = true;
      render();
    }
    try {
      var url = MBot.withProfile("/api/instances", currentProfile, currentRegion);
      url += (url.indexOf("?") === -1 ? "?" : "&") + "state=all";
      if (force) url += "&refresh=1";
      var volUrl = MBot.withProfile("/api/volumes", currentProfile, currentRegion);
      if (force) volUrl += (volUrl.indexOf("?") === -1 ? "?" : "&") + "refresh=1";
      var volPromise = MBot.cachedApi("vol:list:" + (currentProfile || "DEFAULT") + ":" + (currentRegion || ""), volUrl, !!force).catch(function () { return null; });
      var data = await MBot.api(url);
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      instances = data.instances || [];
      var volData = await volPromise;
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      volumesByInstance = {};
      if (volData) {
        (volData.volumes || []).forEach(function (v) {
          var iid = v.instanceId || v["instance-id"];
          if (iid && !volumesByInstance[String(iid)]) {
            volumesByInstance[String(iid)] = v;
          }
        });
      }
      var alive = {};
      instances.forEach(function (raw) { alive[String(raw.id)] = true; });
      Object.keys(selected).forEach(function (id) {
        if (!alive[id]) delete selected[id];
      });
      loading = false;
      render();
      scheduleCpuRefresh();
    } catch (err) {
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      loading = false;
      document.getElementById("instance-grid").innerHTML =
        '<p class="instance-error">加载失败：' +
        MBot.escapeHtml(err.message) + "</p>";
      document.getElementById("instance-empty").hidden = true;
    }
  }

  // Delayed CPU-model refetch: the backend only starts CPU detection (SSH /proc/cpuinfo, 1-3s) when it receives the first list request, so cpuModel is still empty then; refetch after 5s from the lightweight cache-only endpoint GET /api/instances/cpu-models (no OCI calls, <100ms) and update only the CPU text on cards, without a full re-render. Previously this re-fetched the whole instance list with refresh=1 (another full OCI round trip, 3-5s) on every page load. At most one refetch per load round (undetectable machines are not re-polled), and the old timer is cleared in loadInstances when switching account/region. CPU 型号延迟补拉：后端收到第一次列表请求时才触发 CPU 检测（SSH 查 /proc/cpuinfo，要 1-3 秒），当时返回的 cpuModel 还是空；5 秒后调轻量接口 GET /api/instances/cpu-models（只读缓存，不调 OCI，百毫秒内）补拉，只更新卡片上的 CPU 型号文字，不整页重绘。以前是带 refresh=1 重拉整个实例列表（又一轮 OCI 查询，3–5 秒），每进一次页面就多一次。每轮列表加载只补拉一次（检测不到的机器不再反复刷），切账号/区域后旧定时器在 loadInstances 里清掉。
  function scheduleCpuRefresh() {
    if (cpuRefreshTimer) return;
    var missing = instances.some(function (inst) { return !inst.cpuModel; });
    if (!missing) return;
    cpuRefreshTimer = setTimeout(function () {
      cpuRefreshTimer = null;
      refreshCpuModels();
    }, 5000);
  }

  async function refreshCpuModels() {
    var profileAtStart = currentProfile;
    var regionAtStart = currentRegion;
    try {
      var data = await MBot.api(MBot.withProfile("/api/instances/cpu-models", currentProfile, currentRegion));
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      var models = data.cpu_models || {};
      Object.keys(models).forEach(function (id) {
        var model = models[id];
        if (!model) return;
        instances.forEach(function (inst) {
          if (String(inst.id) === String(id)) inst.cpuModel = model;
        });
        var card = document.querySelector('[data-instance-id="' + id + '"]');
        if (!card) return;
        var cell = card.querySelector("[data-cpu-model]");
        if (cell) {
          var fresh = instances.find(function (x) { return String(x.id) === String(id); });
          cell.textContent = cpuModelLabel(model, fresh ? fresh.shape : "");
        }
      });
    } catch (err) {
    }
  }

  async function instanceAction(id, action, label) {
    try {
      await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(id) + "/" + action, currentProfile, currentRegion), { method: "POST" });
      MBot.toast(label + "指令已发送", "success");
      MBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      MBot.toast(label + "失败：" + err.message, "error");
    }
  }

  async function showDetail(inst) {
    var instId = inst.id;
    var seq = ++detailLoadSeq;  // Guard against out-of-order responses when the user opens another instance's detail while this one is still loading. 详情加载序号：用户快速连点两个实例时，旧请求的回包不再覆盖新弹窗。
    // Immediate feedback: open the modal at once with loading placeholders, then fill it when the detail API returns — previously the modal only opened after the (slow) fetch, so the first click looked dead. 点开立即给反馈：先打开弹窗填“加载中…”，等详情接口回来再填数——以前要等接口回来才开弹窗，第一次点看起来像没反应。
    var loadingText = tr("instances.loading", "加载中…");
    detailTarget = {
      id: instId,
      name: norm(inst).name,
      state: "",
    };
    document.getElementById("detail-name").textContent = detailTarget.name;
    document.getElementById("detail-shape").textContent = loadingText;
    document.getElementById("detail-cpu-model").textContent = loadingText;
    document.getElementById("detail-spec").textContent = loadingText;
    document.getElementById("detail-state").textContent = loadingText;
    document.getElementById("detail-public-ip").textContent = loadingText;
    document.getElementById("detail-private-ip").textContent = loadingText;
    document.getElementById("detail-ipv6").textContent = loadingText;
    document.getElementById("detail-ad").textContent = loadingText;
    document.getElementById("detail-boot-volume").textContent = loadingText;
    document.getElementById("detail-created").textContent = loadingText;
    document.getElementById("detail-watchdog").textContent = loadingText;
    document.getElementById("detail-modal").dataset.instanceId = instId;
    document.getElementById("detail-bind-domain-btn").hidden = true;
    document.getElementById("detail-bootbackup-btn").hidden = true;
    document.getElementById("detail-bootswap-btn").hidden = true;
    document.getElementById("detail-bootbackup-del-btn").hidden = true;
    document.getElementById("detail-watchdog-btn").hidden = true;
    document.getElementById("detail-cpu-btn").hidden = true;
    MBot.openModal("detail-modal");
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(instId), currentProfile, currentRegion));
      if (seq !== detailLoadSeq) return;  // A newer detail load started; discard this stale result. 已有更新的详情请求，丢弃这份旧回包。
      var inst2 = res.instance || {};
      var cfg = inst2["shape-config"] || {};
      document.getElementById("detail-name").textContent = inst2["display-name"] || "—";
      document.getElementById("detail-shape").textContent = fmtShape(inst2.shape) || "—";
      document.getElementById("detail-cpu-model").textContent = cpuModelLabel(res.cpu_model, inst2.shape);
      document.getElementById("detail-spec").textContent =
        (cfg.ocpus != null ? cfg.ocpus + " OCPU / " + cfg["memory-in-gbs"] + " GB" : "—");
      document.getElementById("detail-state").innerHTML = stateBadge(inst2["lifecycle-state"]);
      document.getElementById("detail-public-ip").textContent = res.public_ip || "—";
      document.getElementById("detail-private-ip").textContent = res.private_ip || "—";
      var ipv6 = res.ipv6_addresses;
      document.getElementById("detail-ipv6").textContent =
        (ipv6 && ipv6.length ? ipv6.join(", ") : "无");
      document.getElementById("detail-ad").textContent = inst2["availability-domain"] || "—";
      var bootText = "—";
      if (res.boot_volume_size_gbs != null) {
        bootText = res.boot_volume_size_gbs + " GB";
        if (res.boot_volume_vpus_per_gb != null) {
          bootText += " · VPU " + res.boot_volume_vpus_per_gb;
        }
      }
      document.getElementById("detail-boot-volume").textContent = bootText;
      document.getElementById("detail-created").textContent =
        inst2["time-created"] ? MBot.formatTime(inst2["time-created"]) : "—";
      detailTarget = {
        id: instId,
        name: inst2["display-name"] || "—",
        state: inst2["lifecycle-state"] || "",
      };
      document.getElementById("detail-bind-domain-btn").hidden =
        !res.public_ip;
      // Boot-volume backup / restore / delete-old-backups are only available for RUNNING / STOPPED instances. 引导卷备份 / 恢复 / 删除历史备份只对运行中 / 已停止的实例可用。
      var bootButtonsOn = detailTarget.state === "RUNNING" || detailTarget.state === "STOPPED";
      document.getElementById("detail-bootbackup-btn").hidden = !bootButtonsOn;
      document.getElementById("detail-bootswap-btn").hidden = !bootButtonsOn;
      document.getElementById("detail-bootbackup-del-btn").hidden = !bootButtonsOn;
      // The watchdog is only available for RUNNING instances (it must SSH in to install its scripts). 看门狗只对运行中的实例可用（要 SSH 上去装脚本）。
      var watchdogOn = detailTarget.state === "RUNNING";
      document.getElementById("detail-watchdog").textContent = watchdogOn ? "查询中…" : "—";
      document.getElementById("detail-watchdog-btn").hidden = true;
      // CPU re-detection is only available for RUNNING instances. CPU 重新检测也只对运行中的实例可用。
      document.getElementById("detail-cpu-btn").hidden = !watchdogOn;
      if (watchdogOn) refreshWatchdog();
    } catch (err) {
      if (seq !== detailLoadSeq) return;
      MBot.closeModal("detail-modal");
      MBot.toast("加载实例详情失败：" + err.message, "error");
    }
  }

  // Watchdog status is fetched separately after the detail modal opens (the SSH probe is slow and must not block the detail itself). 看门狗状态：详情弹窗打开后单独查（SSH 探针慢，不阻塞详情本身）。
  var watchdogState = null;
  async function refreshWatchdog() {
    var labelEl = document.getElementById("detail-watchdog");
    var btn = document.getElementById("detail-watchdog-btn");
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(detailTarget.id) + "/watchdog",
        currentProfile, currentRegion));
      watchdogState = res.watchdog || null;
      labelEl.textContent = res.label || "—";
      if (watchdogState) {
        btn.textContent = watchdogState.enabled ? "🛡️ 关闭看门狗" : "🛡️ 开启看门狗";
        btn.hidden = false;
      } else {
        btn.hidden = true;
      }
    } catch (err) {
      labelEl.textContent = "查询失败：" + err.message;
      btn.hidden = true;
    }
  }

  async function toggleWatchdog() {
    if (!detailTarget || !watchdogState) return;
    var enable = !watchdogState.enabled;
    var btn = document.getElementById("detail-watchdog-btn");
    var labelEl = document.getElementById("detail-watchdog");
    btn.disabled = true;
    labelEl.textContent = enable ? "正在安装看门狗…" : "正在关闭看门狗…";
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(detailTarget.id) + "/watchdog",
        currentProfile, currentRegion), {
        method: "POST",
        body: JSON.stringify({ enabled: enable }),
      });
      MBot.toast(res.message || "操作完成", "success");
      await refreshWatchdog();
    } catch (err) {
      MBot.toast("看门狗操作失败：" + err.message, "error");
      await refreshWatchdog();
    } finally {
      btn.disabled = false;
    }
  }

  async function refreshCpuModel() {
    if (!detailTarget) return;
    var btn = document.getElementById("detail-cpu-btn");
    var cell = document.getElementById("detail-cpu-model");
    btn.disabled = true;
    cell.textContent = "检测中…";
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(detailTarget.id) + "/cpu-model",
        currentProfile, currentRegion), { method: "POST" });
      cell.textContent = cpuModelLabel(res.cpu_model, detailTarget.shape);
      MBot.toast(res.message || "操作完成",
        res.cpu_model ? "success" : "error");
      if (res.cpu_model) loadInstances(false);
    } catch (err) {
      cell.textContent = cpuModelLabel(null, detailTarget.shape);
      MBot.toast("CPU 检测失败：" + err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function showBootswap() {
    if (!detailTarget) return;
    bootswapTarget = detailTarget;
    bootswapSelected = null;
    document.getElementById("bootswap-name").textContent = bootswapTarget.name;
    document.getElementById("bootswap-confirm-btn").disabled = true;
    var listEl = document.getElementById("bootswap-list");
    listEl.textContent = "加载中…";
    MBot.closeModal("detail-modal");
    MBot.openModal("bootswap-modal");
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(bootswapTarget.id) + "/boot-backups",
        currentProfile, currentRegion));
      var backups = res.backups || [];
      if (!backups.length) {
        var msg = "这台实例的启动盘还没有可用备份（AVAILABLE）。请先在启动盘页面创建备份，几分钟后可用。";
        if (res.other_available_count) {
          msg += "注：账号里还有 " + res.other_available_count + " 个其他启动盘的可用备份（比如换盘前旧盘的备份），删盘不会删备份；原地恢复只列当前启动盘的备份，旧备份可从备份页恢复成新实例。";
        }
        listEl.textContent = msg;
        return;
      }
      listEl.innerHTML = "";
      backups.forEach(function (b) {
        var label = document.createElement("label");
        label.className = "radio-row";
        var radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "bootswap-backup";
        radio.value = b.id;
        radio.addEventListener("change", function () {
          bootswapSelected = b.id;
          document.getElementById("bootswap-confirm-btn").disabled = false;
        });
        var text = document.createElement("span");
        var created = b["time-created"] ? MBot.formatTime(b["time-created"]) : "—";
        text.textContent = (b["display-name"] || "—") + "（" +
          (b["size-in-gbs"] != null ? b["size-in-gbs"] + "GB" : "—") +
          "，" + created + "）";
        label.appendChild(radio);
        label.appendChild(text);
        listEl.appendChild(label);
      });
    } catch (err) {
      listEl.textContent = "加载备份失败：" + err.message;
    }
  }

  // Boot-volume backup: ask for a name (default "<instance>-backup-YYYYMMDD-HHMM", empty = server-generated) and a type (FULL / INCREMENTAL) first; when the free quota is full (409 + full), open the delete-old-backups modal instead so the user deletes first, then creates. 引导卷备份：先弹确认框让用户填名字（默认“<实例名>-backup-年月日-时分”，留空后端自动生成）与类型（全量/增量）；免费额度满了（409 + full）时改弹删除历史备份弹窗，让用户先删再建。
  async function createBootBackup() {
    if (!detailTarget) return;
    var picked = await MBot.createBackupDialog(
      MBot.defaultBackupName(detailTarget.name));
    if (!picked) return;
    var btn = document.getElementById("detail-bootbackup-btn");
    btn.disabled = true;
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(detailTarget.id) + "/boot-backups",
        currentProfile, currentRegion), {
        method: "POST",
        body: {
          display_name: picked.name || undefined,
          type: picked.type || "FULL",
        },
      });
      var name = (res.backup && res.backup["display-name"]) || "备份";
      var quota = (res.count != null && res.limit != null)
        ? "（免费额度 " + res.count + "/" + res.limit + "）" : "";
      MBot.toast("备份「" + name + "」创建中，几分钟后可用" + quota, "success");
    } catch (err) {
      if (err.status === 409 && err.data && err.data.full) {
        MBot.toast(err.message, "error");
        showBootBackupDelete();
      } else {
        MBot.toast("创建备份失败：" + err.message, "error");
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function showBootBackupDelete() {
    if (!detailTarget) return;
    bootBackupDelTarget = detailTarget;
    document.getElementById("bootbackup-del-name").textContent = bootBackupDelTarget.name;
    document.getElementById("bootbackup-del-confirm-btn").disabled = true;
    var listEl = document.getElementById("bootbackup-del-list");
    listEl.textContent = "加载中…";
    MBot.closeModal("detail-modal");
    MBot.openModal("bootbackup-del-modal");
    try {
      var res = await MBot.api(MBot.withProfile(
        "/api/instances/" + encodeURIComponent(bootBackupDelTarget.id) + "/boot-backups?include_all=true",
        currentProfile, currentRegion));
      var backups = res.backups || [];
      if (!backups.length) {
        listEl.textContent = "这台实例的启动盘还没有备份。";
        return;
      }
      listEl.innerHTML = "";
      backups.forEach(function (b) {
        var label = document.createElement("label");
        label.className = "check-row";
        var box = document.createElement("input");
        box.type = "checkbox";
        box.value = b.id;
        box.addEventListener("change", function () {
          var checked = listEl.querySelectorAll("input[type=checkbox]:checked").length;
          document.getElementById("bootbackup-del-confirm-btn").disabled = checked === 0;
        });
        var text = document.createElement("span");
        var created = b["time-created"] ? MBot.formatTime(b["time-created"]) : "—";
        text.textContent = (b["display-name"] || "—") + "（" +
          (b["size-in-gbs"] != null ? b["size-in-gbs"] + "GB" : "—") +
          "，" + (b["lifecycle-state"] || "—") + "，" + created + "）";
        label.appendChild(box);
        label.appendChild(text);
        listEl.appendChild(label);
      });
    } catch (err) {
      listEl.textContent = "加载备份失败：" + err.message;
    }
  }

  async function showIpManage(inst) {
    ipTarget = inst;
    document.getElementById("ip-name").textContent = norm(inst).name;
    document.getElementById("ip-public").textContent = norm(inst).public_ip || "—";
    document.getElementById("ip-private").textContent = "加载中…";
    document.getElementById("ip-ipv6").textContent = "加载中…";
    MBot.openModal("ip-modal");
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id), currentProfile, currentRegion));
      if (ipTarget !== inst) return;  // The instance changed while the modal was open; discard the stale result. 弹窗期间切了实例，丢弃旧结果。
      document.getElementById("ip-public").textContent = res.public_ip || "—";
      document.getElementById("ip-private").textContent = res.private_ip || "—";
      var ipv6 = res.ipv6_addresses;
      document.getElementById("ip-ipv6").textContent =
        (ipv6 && ipv6.length ? ipv6.join(", ") : "无");
    } catch (err) {
      if (ipTarget !== inst) return;
      document.getElementById("ip-private").textContent = "—";
      document.getElementById("ip-ipv6").textContent = "加载失败：" + err.message;
    }
  }

  // Boot-volume modal: prefer the detail API's boot-volume fields (freshly fetched, newest); fall back to the volume data already joined onto the card when unavailable; error and do not open when neither exists. 启动盘弹窗：详情优先用详情接口的启动盘字段（现查，最新），查不到时退回卡片拼好的启动盘数据；都没有时报错不打开。
  // Resize reuses POST /api/volumes/{id}/resize. 扩容复用 POST /api/volumes/{id}/resize。
  async function showBootVolume(inst) {
    var n = norm(inst);
    var vol = null;
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id), currentProfile, currentRegion));
      if (res.boot_volume_id) {
        vol = {
          volumeId: res.boot_volume_id,
          sizeInGBs: res.boot_volume_size_gbs,
          vpusPerGB: res.boot_volume_vpus_per_gb,
          lifecycleState: res.boot_volume_state,
          attachmentState: res.boot_volume_attachment_state,
          attached: res.boot_volume_attachment_state === "ATTACHED"
            || res.boot_volume_attachment_state === "ATTACHING",
        };
      }
    } catch (err) {
    }
    if (!vol) {
      var joined = volumesByInstance[String(inst.id)];
      if (joined && (joined.volumeId || joined.id)) {
        vol = {
          volumeId: joined.volumeId || joined.id,
          sizeInGBs: joined.sizeInGBs != null ? joined.sizeInGBs : joined["size-in-gbs"],
          vpusPerGB: joined.vpusPerGB != null ? joined.vpusPerGB : joined["vpus-per-gb"],
          lifecycleState: joined.lifecycleState || joined["lifecycle-state"],
          attachmentState: joined.attachmentState,
          attached: !!joined.attached,
        };
      }
    }
    if (!vol || !vol.volumeId) {
      MBot.toast("未找到该实例的启动盘", "error");
      return;
    }
    bootVolumeTarget = { instance: inst, volumeId: vol.volumeId, size: vol.sizeInGBs };
    document.getElementById("boot-volume-instance").textContent = n.name;
    document.getElementById("boot-volume-size").textContent =
      vol.sizeInGBs != null ? vol.sizeInGBs + " GB" : "—";
    document.getElementById("boot-volume-vpus").textContent =
      vol.vpusPerGB != null ? vol.vpusPerGB : "—";
    document.getElementById("boot-volume-state").textContent =
      vol.lifecycleState || "—";
    document.getElementById("boot-volume-attached").textContent =
      vol.attached ? "已附加" : (vol.attachmentState || "—");
    document.getElementById("boot-volume-ocid").textContent =
      "…" + String(vol.volumeId).slice(-12);
    document.getElementById("boot-volume-ocid").title = vol.volumeId;
    document.getElementById("boot-volume-new-size").value =
      vol.sizeInGBs != null ? vol.sizeInGBs + 10 : "";
    MBot.openModal("boot-volume-modal");
  }

  async function addIpv6(inst) {
    var name = norm(inst).name;
    var ok = await MBot.confirmDialog(
      "附加 IPv6", "确认给实例 " + MBot.escapeHtml(name) + " 分配一个 IPv6 地址吗？",
      "确认附加", false);
    if (!ok) return;
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile, currentRegion), { method: "POST" });
      MBot.toast("已分配 IPv6：" + (res.ipv6 || "—"), "success");
      MBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      MBot.toast("附加 IPv6 失败：" + err.message, "error");
    }
  }

  async function deleteIpv6(inst) {
    var name = norm(inst).name;
    var ok = await MBot.confirmDialog(
      "删除 IPv6",
      "确认删除实例 " + MBot.escapeHtml(name) + " 的全部 IPv6 地址吗？<br>删除后这些地址立即不可达。",
      "确认删除", true);
    if (!ok) return;
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile, currentRegion), { method: "DELETE" });
      var deleted = res.deleted || [];
      if (deleted.length === 0) {
        MBot.toast(res.message || "该实例没有 IPv6", "info");
      } else {
        MBot.toast("已删除 " + deleted.length + " 个 IPv6 地址", "success");
      }
      MBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      MBot.toast("删除 IPv6 失败：" + err.message, "error");
    }
  }

  async function doRepair(inst) {
    var name = norm(inst).name;
    var ok = await MBot.confirmDialog(
      "尝试修复",
      "确认尝试修复实例 " + MBot.escapeHtml(name) + " 吗？<br>只检查实例状态、主 VNIC 与公网 IP，公网 IP 缺失时重新附加一个临时公网 IP，不做其他改动。",
      "确认修复", false);
    if (!ok) return;
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/repair", currentProfile, currentRegion), { method: "POST" });
      var report = res.report || {};
      document.getElementById("repair-name").textContent = name;
      document.getElementById("repair-report").innerHTML =
        (report.checks || []).map(function (c) {
          var mark = c.status === "fixed" ? "🔧" : "✅";
          return "<li>" + mark + " " + MBot.escapeHtml(c.name) + "："
            + MBot.escapeHtml(c.detail || "") + "</li>";
        }).join("");
      document.getElementById("repair-summary").textContent =
        report.repaired
          ? "已修复：新公网 IP " + (report.new_ip || "—")
          : "检查通过，无需修复。当前公网 IP：" + (report.public_ip || "—");
      MBot.openModal("repair-modal");
      MBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      MBot.toast("修复失败：" + err.message, "error");
    }
  }

  // Terminate confirm modal: delete and delete (keep boot volume) share it, switching title / warning / button text by mode. 终止确认弹窗：删除与删除（保留启动盘）共用，按模式切换标题/警告/按钮文案。
  function openTerminateModal(inst, preserve) {
    terminateTarget = inst;
    terminatePreserve = !!preserve;
    document.getElementById("terminate-name").textContent = norm(inst).name;
    if (preserve) {
      document.getElementById("terminate-title").textContent = "终止实例（保留启动盘）";
      document.getElementById("terminate-warning").textContent =
        "实例将终止，引导卷会保留下来（占用 200GB 免费额度），之后可在启动盘页面查看或删除；实例上的数据仍保存在该引导卷里。";
      document.getElementById("btn-confirm-terminate").textContent = "确认终止（保留启动盘）";
    } else {
      document.getElementById("terminate-title").textContent = "终止实例";
      document.getElementById("terminate-warning").textContent =
        "此操作不可撤销，将永久删除引导卷，实例上的数据会全部丢失。";
      document.getElementById("btn-confirm-terminate").textContent = "确认终止";
    }
    MBot.openModal("terminate-modal");
  }

  async function batchAction(action, label, confirm) {
    var ids = selectedIds();
    if (ids.length === 0) return;
    try {
      var res = await MBot.api(MBot.withProfile("/api/instances/batch", currentProfile, currentRegion), {
        method: "POST",
        body: { action: action, instance_ids: ids, confirm: !!confirm },
      });
      var ok = (res.succeeded || []).length;
      var bad = (res.failed || []).length;
      if (bad === 0) {
        MBot.toast(label + "完成：成功 " + ok + " 台", "success");
      } else {
        var detail = (res.failed || []).map(function (f) {
          return f.id.slice(-4) + "：" + f.error;
        }).join("；");
        MBot.toast(label + "：成功 " + ok + " 台，失败 " + bad + " 台（" + detail + "）", "error");
      }
      selected = {};
      MBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      MBot.toast(label + "失败：" + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Fill the profile dropdown first (restoring the last choice), then the region dropdown (only shown for multi-region accounts), then load the list for the current account + region; when the profiles API fails, still load with the default account so the page is never blank. 先填 profile 下拉（恢复上次选择），再填区域下拉（多区账号才显示），最后按当前账号 + 区域加载列表；profiles 接口失败时仍按默认账号加载，不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    var regionSelect = document.getElementById("region-select");
    MBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadInstances();
    }).catch(function () {
      loadInstances();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      MBot.setProfile(currentProfile);
      // Switching accounts resets the region to the new account's home region (same as the bot). 切账号时区域重置为新账号的主区（与机器人端一致）。
      MBot.setRegion(null);
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      selected = {};
      // When switching accounts, clear the list and show loading placeholders first, then load the new account's data, so old-account cards never flash for a frame. 切账号时先清空列表并显示加载占位，再加载新账号数据，避免旧账号卡片闪一帧。
      instances = [];
      volumesByInstance = {};
      loading = true;
      render();
      loadInstances();
    });
    regionSelect.addEventListener("change", function (e) {
      currentRegion = e.target.value;
      MBot.setRegion(currentRegion);
      selected = {};
      instances = [];
      volumesByInstance = {};
      loading = true;
      render();
      loadInstances();
    });
    document.getElementById("btn-refresh-instances").addEventListener("click", function () { loadInstances(true); });

    // Re-render dynamic text after a language switch (state badges, selected counts, etc. go through the i18n dictionary). 语言切换后重渲染动态文案（状态徽标、已选台数等走 i18n 字典）。
    window.addEventListener("mbot-lang-change", function () { render(); });

    document.getElementById("select-all").addEventListener("change", function (e) {
      if (e.target.checked) {
        instances.forEach(function (raw) { selected[String(raw.id)] = true; });
      } else {
        selected = {};
      }
      render();
    });

    document.querySelectorAll("[data-batch]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        var action = btn.getAttribute("data-batch");
        var label = batchLabel(action);
        var targets = selectedInstances();
        if (targets.length === 0) return;
        if (action === "terminate") {
          document.getElementById("batch-terminate-count").textContent = targets.length;
          document.getElementById("batch-terminate-names").innerHTML =
            targets.map(function (raw) {
              return "<li>" + MBot.escapeHtml(norm(raw).name) + "</li>";
            }).join("");
          MBot.openModal("batch-terminate-modal");
          return;
        }
        var names = targets.map(function (raw) { return norm(raw).name; }).join("、");
        var ok = await MBot.confirmDialog(
          label, "确认" + label + "以下实例吗？<br>" + MBot.escapeHtml(names),
          "确认" + label, false);
        if (ok) await batchAction(action, label, false);
      });
    });

    document.getElementById("instance-grid").addEventListener("click", function (e) {
      var checkbox = e.target.closest("[data-select]");
      if (checkbox) {
        var id = checkbox.getAttribute("data-select");
        if (checkbox.checked) selected[id] = true;
        else delete selected[id];
        updateBatchBar();
        return;
      }
      var copyBtn = e.target.closest("[data-copy]");
      if (copyBtn) {
        MBot.copyText(copyBtn.getAttribute("data-copy"), "IP");
        return;
      }
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var card = btn.closest("[data-instance-id]");
      var instId = card.getAttribute("data-instance-id");
      var inst = instances.find(function (x) { return String(x.id) === instId; });
      if (!inst) return;
      var act = btn.getAttribute("data-act");
      if (act === "start") instanceAction(instId, "start", tr("instances.start", "启动"));
      else if (act === "stop") instanceAction(instId, "stop", tr("instances.stop", "停止"));
      else if (act === "restart") instanceAction(instId, "restart", tr("instances.restart", "重启"));
      else if (act === "detail") showDetail(inst);
      else if (act === "ssh") {
        // Instance card SSH one-click login: the terminal page auto-connects as root with the fixed password (see /api/ssh/connect-instance); on failure it falls back to the manual form with the host prefilled. 实例卡片 SSH 一键直连：终端页用固定密码自动登录 root（见 /api/ssh/connect-instance），失败回落手动表单（预填主机）。
        var n = norm(inst);
        var q = "instance=" + encodeURIComponent(instId) +
          "&profile=" + encodeURIComponent(currentProfile || "") +
          "&region=" + encodeURIComponent(currentRegion || "");
        if (n.public_ip) q += "&host=" + encodeURIComponent(n.public_ip);
        window.location.href = "/terminal?" + q;
      } else if (act === "ip-manage") showIpManage(inst);
      else if (act === "boot-volume") showBootVolume(inst);
      else if (act === "resize") {
        resizeTarget = inst;
        var n = norm(inst);
        document.getElementById("resize-name").textContent = n.name;
        document.getElementById("resize-current").textContent =
          (n.ocpus != null ? n.ocpus + " 核 / " + n.memory_gb + " GB" : "未知");
        document.getElementById("resize-ocpus").value = n.ocpus != null ? n.ocpus : 2;
        document.getElementById("resize-memory").value = n.memory_gb != null ? n.memory_gb : 12;
        MBot.openModal("resize-modal");
      } else if (act === "repair") doRepair(inst);
      else if (act === "terminate") openTerminateModal(inst, false);
      else if (act === "terminate-keep") openTerminateModal(inst, true);
      else if (act === "rename") {
        renameTarget = inst;
        document.getElementById("rename-current").textContent = norm(inst).name;
        document.getElementById("rename-name").value = norm(inst).name;
        MBot.openModal("rename-modal");
      } else if (act === "reinstall") {
        reinstallTarget = inst;
        document.getElementById("reinstall-name").textContent = norm(inst).name;
        document.getElementById("reinstall-confirm-name").value = "";
        MBot.openModal("reinstall-modal");
      } else if (act === "console") {
        (async function () {
          try {
            var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(instId) + "/console", currentProfile, currentRegion));
            var conn = res.console || {};
            consoleCommand = conn["connection-string"] || "";
            document.getElementById("console-name").textContent = norm(inst).name;
            document.getElementById("console-state").textContent =
              conn["lifecycle-state"] ? "（" + conn["lifecycle-state"] + "）" : "";
            document.getElementById("console-command").textContent =
              consoleCommand || "未返回连接命令";
            document.getElementById("console-fingerprint").textContent =
              conn.fingerprint ? "指纹：" + conn.fingerprint : "";
            MBot.openModal("console-modal");
          } catch (err) {
            MBot.toast("获取串行控制台失败：" + err.message, "error");
          }
        })();
      }
    });

    document.getElementById("btn-confirm-terminate").addEventListener("click", async function () {
      if (!terminateTarget) return;
      var inst = terminateTarget;
      var preserve = terminatePreserve;
      try {
        await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/terminate", currentProfile, currentRegion), {
          method: "POST",
          body: { confirm: true, preserve_boot_volume: preserve },
        });
        MBot.toast(preserve
          ? "实例已终止，引导卷已保留（可在启动盘页面删除）"
          : "实例已终止，引导卷已永久删除", "success");
        MBot.closeModal("terminate-modal");
        MBot.cacheClear("inst:");
        if (preserve) MBot.cacheClear("vol:");
        await loadInstances();
      } catch (err) {
        MBot.toast("终止失败：" + err.message, "error");
      }
    });

    document.getElementById("detail-bootswap-btn").addEventListener("click", function () {
      showBootswap();
    });

    document.getElementById("detail-bootbackup-btn").addEventListener("click", function () {
      createBootBackup();
    });

    document.getElementById("detail-bootbackup-del-btn").addEventListener("click", function () {
      showBootBackupDelete();
    });
    document.getElementById("detail-watchdog-btn").addEventListener("click", function () {
      toggleWatchdog();
    });
    document.getElementById("detail-cpu-btn").addEventListener("click", function () {
      refreshCpuModel();
    });

    document.getElementById("bootbackup-del-confirm-btn").addEventListener("click", async function () {
      if (!bootBackupDelTarget) return;
      var ids = Array.prototype.slice.call(
        document.querySelectorAll("#bootbackup-del-list input[type=checkbox]:checked")
      ).map(function (box) { return box.value; });
      if (!ids.length) return;
      var btn = document.getElementById("bootbackup-del-confirm-btn");
      btn.disabled = true;
      try {
        var res = await MBot.api(MBot.withProfile("/api/boot-backups/delete-batch", currentProfile, currentRegion), {
          method: "POST",
          body: { backup_ids: ids },
        });
        var deleted = (res.deleted || []).length;
        var failed = (res.failed || []).length;
        MBot.toast("已删除 " + deleted + " 个备份" + (failed ? "，" + failed + " 个失败" : ""), failed ? "error" : "success");
        MBot.closeModal("bootbackup-del-modal");
        MBot.cacheClear("vol:");
      } catch (err) {
        MBot.toast("删除备份失败：" + err.message, "error");
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById("bootswap-confirm-btn").addEventListener("click", async function () {
      if (!bootswapTarget || !bootswapSelected) return;
      var btn = document.getElementById("bootswap-confirm-btn");
      btn.disabled = true;
      try {
        await MBot.api(MBot.withProfile(
          "/api/instances/" + encodeURIComponent(bootswapTarget.id) + "/restore-boot-volume",
          currentProfile, currentRegion), {
          method: "POST",
          body: { backup_id: bootswapSelected, confirm: true },
        });
        MBot.toast("引导卷恢复已开始，进度请在任务页查看", "success");
        MBot.closeModal("bootswap-modal");
        window.location.href = "/tasks";
      } catch (err) {
        MBot.toast("恢复失败：" + err.message, "error");
        btn.disabled = false;
      }
    });

    document.getElementById("btn-ip-change").addEventListener("click", function () {
      if (!ipTarget) return;
      changeIpTarget = ipTarget;
      document.getElementById("change-ip-name").textContent = norm(ipTarget).name;
      MBot.closeModal("ip-modal");
      MBot.openModal("change-ip-modal");
    });
    document.getElementById("btn-ip-add-ipv6").addEventListener("click", function () {
      if (!ipTarget) return;
      var inst = ipTarget;
      MBot.closeModal("ip-modal");
      addIpv6(inst);
    });
    document.getElementById("btn-ip-delete-ipv6").addEventListener("click", function () {
      if (!ipTarget) return;
      var inst = ipTarget;
      MBot.closeModal("ip-modal");
      deleteIpv6(inst);
    });

    document.getElementById("btn-boot-volume-backup").addEventListener("click", async function () {
      if (!bootVolumeTarget || !bootVolumeTarget.volumeId) return;
      var created = await MBot.backupBootVolume(bootVolumeTarget.volumeId, currentProfile);
      if (created) MBot.closeModal("boot-volume-modal");
    });

    document.getElementById("btn-boot-volume-restore").addEventListener("click", function () {
      if (!bootVolumeTarget || !bootVolumeTarget.instance) return;
      detailTarget = bootVolumeTarget.instance;
      MBot.closeModal("boot-volume-modal");
      showBootswap();
    });

    // Boot-volume resize: the frontend validates the new size is larger than the current one first; the backend validates again. 启动盘扩容：前端先校验新容量大于当前容量，后端再校验一次。
    document.getElementById("boot-volume-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!bootVolumeTarget) return;
      var size = parseInt(document.getElementById("boot-volume-new-size").value, 10);
      if (!size || size <= 0) {
        MBot.toast("新容量必须是正整数（GB）", "error");
        return;
      }
      if (bootVolumeTarget.size != null && size <= bootVolumeTarget.size) {
        MBot.toast("新容量必须大于当前容量 " + bootVolumeTarget.size + "GB", "error");
        return;
      }
      var ok = await MBot.confirmDialog(
        "确认扩容",
        "确认把实例 " + MBot.escapeHtml(norm(bootVolumeTarget.instance).name)
        + " 的启动盘扩到 " + size + "GB 吗？<br>扩容后无法缩回，扩出的部分会计入 200GB 免费额度。",
        "确认扩容", false);
      if (!ok) return;
      try {
        await MBot.api(MBot.withProfile("/api/volumes/" + encodeURIComponent(bootVolumeTarget.volumeId) + "/resize", currentProfile, currentRegion), {
          method: "POST",
          body: { size_in_gbs: size },
        });
        MBot.toast("扩容已提交，启动盘正在扩大到 " + size + "GB", "success");
        MBot.closeModal("boot-volume-modal");
        MBot.cacheClear("vol:");
        MBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        MBot.toast("扩容失败：" + err.message, "error");
      }
    });

    document.getElementById("btn-confirm-change-ip").addEventListener("click", async function () {
      if (!changeIpTarget) return;
      var inst = changeIpTarget;
      try {
        var res = await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/change-ip", currentProfile, currentRegion), {
          method: "POST",
          body: { confirm: true },
        });
        MBot.toast("换 IP 成功：" + (res.old_ip || "—") + " → " + (res.new_ip || "—"), "success");
        MBot.closeModal("change-ip-modal");
        MBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        MBot.toast("换 IP 失败：" + err.message, "error");
      }
    });

    // Confirm resize: the frontend validates against the free-tier cap first; the backend validates again. 确认升/降配：前端先按免费额度上限校验，后端再校验一次。
    document.getElementById("resize-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!resizeTarget) return;
      var ocpus = parseInt(document.getElementById("resize-ocpus").value, 10);
      var memory = parseInt(document.getElementById("resize-memory").value, 10);
      if (!ocpus || ocpus < 1 || ocpus > 4) {
        MBot.toast("OCPU 必须在 1–4 之间（免费额度上限 4 核）", "error");
        return;
      }
      if (!memory || memory < 1 || memory > 24) {
        MBot.toast("内存必须在 1–24GB 之间（免费额度上限 24GB）", "error");
        return;
      }
      try {
        await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(resizeTarget.id) + "/resize", currentProfile, currentRegion), {
          method: "POST",
          body: { ocpus: ocpus, memory_gb: memory, confirm: true },
        });
        MBot.toast("改规格已提交（" + ocpus + " 核 / " + memory + "GB），实例可能会重启", "success");
        MBot.closeModal("resize-modal");
        MBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        MBot.toast("升/降配失败：" + err.message, "error");
      }
    });

    document.getElementById("btn-confirm-batch-terminate").addEventListener("click", async function () {
      MBot.closeModal("batch-terminate-modal");
      await batchAction("terminate", batchLabel("terminate"), true);
    });

    document.getElementById("rename-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!renameTarget) return;
      var newName = document.getElementById("rename-name").value.trim();
      if (!newName) {
        MBot.toast("新实例名不能为空", "error");
        return;
      }
      try {
        await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(renameTarget.id) + "/rename", currentProfile, currentRegion), {
          method: "POST",
          body: { name: newName },
        });
        MBot.toast("实例已改名为 " + newName, "success");
        MBot.closeModal("rename-modal");
        MBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        MBot.toast("改名失败：" + err.message, "error");
      }
    });

    // Confirm reinstall: the typed name must match the instance name exactly. 确认重装：输入名必须与实例名完全一致。
    document.getElementById("reinstall-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!reinstallTarget) return;
      var confirmName = document.getElementById("reinstall-confirm-name").value;
      if (confirmName !== norm(reinstallTarget).name) {
        MBot.toast("输入的实例名与实际不符，请完整输入实例名", "error");
        return;
      }
      try {
        await MBot.api(MBot.withProfile("/api/instances/" + encodeURIComponent(reinstallTarget.id) + "/reinstall", currentProfile, currentRegion), {
          method: "POST",
          body: { confirm: true, confirm_name: confirmName },
        });
        MBot.toast("重装已提交，系统盘正在重建", "success");
        MBot.closeModal("reinstall-modal");
        MBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        MBot.toast("重装失败：" + err.message, "error");
      }
    });

    document.getElementById("btn-copy-console").addEventListener("click", function () {
      if (consoleCommand) MBot.copyText(consoleCommand, "连接命令");
    });
  });
})();
