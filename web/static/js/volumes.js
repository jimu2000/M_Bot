/* 启动盘逻辑：卷表格、扩容、删除、孤儿盘列表、启动卷备份 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var volumes = [];
  var orphans = [];
  var backups = [];
  var currentProfile = null;  // Current account (profile), restored by fillProfileSelect. 当前账号（profile），由 fillProfileSelect 恢复。
  var currentRegion = null;  // Current working region, restored by fillRegionSelect (only multi-region accounts have the second dropdown). 当前工作区域，由 fillRegionSelect 恢复（多区账号才有二级下拉）。
  var loadGen = 0;  // Load generation: incremented on every loadAll; async results from an old generation no longer write back to the shared lists, so fast account switching cannot mix old and new data. 加载代次：每次 loadAll 递增，旧代次的异步结果不再写回共享列表，避免快速切账号时新旧数据串台。
  var resizeTarget = null;
  var deleteTarget = null;
  var vpusTarget = null;
  var restoreTarget = null;
  var deleteBackupTarget = null;
  var selectedBackups = {};  // Multi-select delete: selected backup id -> true. 多选删除：已选备份 id -> true。
  var backupPaid = false;  // Whether the current account is paid (backups are not capped at 5). 当前账号是否付费（备份不限 5 个）。

  // Field normalization: the backend returns panel shapes (camelCase) {volumeId, attachmentId, displayName, sizeInGBs, vpusPerGB, lifecycleState, instanceId, instanceName, attached, attachmentState, availabilityDomain, timeCreated}; during the transition both old and new fields are written, and new fields win here with old ones as fallback. 字段归一化：后端返回面板形状（camelCase）{volumeId, attachmentId, displayName, sizeInGBs, vpusPerGB, lifecycleState, instanceId, instanceName, attached, attachmentState, availabilityDomain, timeCreated}，过渡期同时双写旧字段，这里优先认新字段，旧字段作兜底。
  function norm(v) {
    return {
      id: v.volumeId || v.id,
      name: v.displayName || v["display-name"] || v.display_name || "—",
      size: v.sizeInGBs != null ? v.sizeInGBs
        : (v["size-in-gbs"] != null ? v["size-in-gbs"] : v.size_in_gbs),
      vpus: v.vpusPerGB != null ? v.vpusPerGB
        : (v["vpus-per-gb"] != null ? v["vpus-per-gb"] : v.vpus_per_gb),
      state: v.lifecycleState || v["lifecycle-state"] || v.state || "",
      instance_id: v.instanceId || v["instance-id"] || v.instance_id || null,
      instance_name: v.instanceName || v.instance_name || null,
      attached: v.attached,
      attachment_state: v.attachmentState || null,
      ad: v.availabilityDomain || v["availability-domain"] || v.availability_domain || null,
    };
  }

  function stateBadge(state) {
    var s = (state || "").toUpperCase();
    var label = window.MBotI18n ? window.MBotI18n.volumeStateLabel(state) : (state || "—");
    if (s === "AVAILABLE") return '<span class="badge badge-green">' + MBot.escapeHtml(label) + "</span>";
    if (s === "IN-USE") return '<span class="badge badge-blue">' + MBot.escapeHtml(label) + "</span>";
    if (s === "PROVISIONING" || s === "RESTORING") return '<span class="badge badge-amber">' + MBot.escapeHtml(label) + "</span>";
    if (s === "TERMINATED" || s === "FAULTY") return '<span class="badge badge-red">' + MBot.escapeHtml(label) + "</span>";
    return '<span class="badge badge-gray">' + MBot.escapeHtml(label) + "</span>";
  }

  function volumeRow(raw, showInstance) {
    var esc = MBot.escapeHtml;
    var v = norm(raw);
    var instanceCell = showInstance
      ? "<td>" + (v.instance_id
          ? '<span class="mono" title="' + esc(v.instance_id) + '">' + esc(v.instance_name || ("…" + v.instance_id.slice(-12))) + "</span>" +
            (v.attachment_state ? '<br><span style="color: var(--text-faint); font-size: 12px;">' + esc(v.attachment_state) + "</span>" : "")
          : '<span style="color: var(--text-faint);">未挂载</span>') + "</td>"
      : "";
    var vpusCell = showInstance
      ? "<td>" + esc(v.vpus != null ? v.vpus : "—") + "</td>"
      : "";
    // A deleted volume is a tombstone record: resize / VPU change / backup / delete are all invalid, so no action buttons are given. 已删除的盘是墓碑记录，扩容/改 VPU/备份/删除都无效，不给操作按钮。
    var actionsCell = (v.state || "").toUpperCase() === "TERMINATED"
      ? '<td><span style="color: var(--text-faint);">已删除</span></td>'
      : '<td><div class="row-actions">' +
          '<button type="button" class="btn btn-sm" data-act="resize">扩容</button>' +
          '<button type="button" class="btn btn-sm" data-act="vpus">改 VPU</button>' +
          '<button type="button" class="btn btn-sm" data-act="backup">备份</button>' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
        "</div></td>";
    return (
      "<tr data-volume-id=\"" + esc(v.id) + "\">" +
        "<td><b>" + esc(v.name) + "</b></td>" +
        "<td>" + esc(v.size) + " GB</td>" +
        vpusCell +
        "<td>" + stateBadge(v.state) + "</td>" +
        instanceCell +
        actionsCell +
      "</tr>"
    );
  }

  // Backup field normalization: the backend returns panel shapes (camelCase) {backupId, displayName, bootVolumeId, sizeInGBs, lifecycleState, sourceType, type, timeCreated, imageId}, with old fields written alongside during the transition. 备份字段归一化：后端返回面板形状（camelCase）{backupId, displayName, bootVolumeId, sizeInGBs, lifecycleState, sourceType, type, timeCreated, imageId}，过渡期同时双写旧字段。
  function normBackup(b) {
    return {
      id: b.backupId || b.id,
      name: b.displayName || b["display-name"] || b.display_name || "—",
      boot_volume_id: b.bootVolumeId || b["boot-volume-id"] || b.boot_volume_id || null,
      size: b.sizeInGBs != null ? b.sizeInGBs
        : (b["size-in-gbs"] != null ? b["size-in-gbs"] : b.size_in_gbs),
      state: b.lifecycleState || b["lifecycle-state"] || b.state || "",
      source_type: b.sourceType || b["source-type"] || b.source_type || "",
      type: b.type || "",
      time_created: b.timeCreated || b["time-created"] || b.time_created || null,
    };
  }

  function backupStateBadge(state) {
    var s = (state || "").toUpperCase();
    var label = window.MBotI18n ? window.MBotI18n.volumeStateLabel(state) : (state || "—");
    if (s === "AVAILABLE") return '<span class="badge badge-green">' + MBot.escapeHtml(label) + "</span>";
    if (s === "CREATING" || s === "REQUEST_RECEIVED") return '<span class="badge badge-amber">' + MBot.escapeHtml(label) + "</span>";
    if (s === "TERMINATED" || s === "FAULTY") return '<span class="badge badge-red">' + MBot.escapeHtml(label) + "</span>";
    return '<span class="badge badge-gray">' + MBot.escapeHtml(label) + "</span>";
  }

  // Source volume name: a backup record only carries bootVolumeId, so look the name up in the loaded volume list; when not found (the source volume was deleted), show the truncated OCID. 来源卷名：备份记录只有 bootVolumeId，在已加载的卷列表里找名字，找不到（来源盘已删）就显示截断的 OCID。
  function sourceVolumeName(bootVolumeId) {
    if (!bootVolumeId) return "—";
    var all = volumes.concat(orphans);
    for (var i = 0; i < all.length; i++) {
      var v = norm(all[i]);
      if (v.id === bootVolumeId) return v.name;
    }
    return "…" + bootVolumeId.slice(-12);
  }

  function backupRow(raw) {
    var esc = MBot.escapeHtml;
    var b = normBackup(raw);
    var canRestore = (b.state || "").toUpperCase() === "AVAILABLE";
    var checked = selectedBackups[b.id] ? " checked" : "";
    return (
      "<tr data-backup-id=\"" + esc(b.id) + "\">" +
        "<td><input type=\"checkbox\" class=\"backup-check\" data-backup-id=\"" +
          esc(b.id) + "\"" + checked + " aria-label=\"选择备份 " + esc(b.name) + "\"></td>" +
        "<td><b>" + esc(b.name) + "</b>" +
          (b.type ? '<br><span style="color: var(--text-faint); font-size: 12px;">' + esc(b.type) + "</span>" : "") + "</td>" +
        "<td><span class=\"mono\" title=\"" + esc(b.boot_volume_id || "") + "\">" + esc(sourceVolumeName(b.boot_volume_id)) + "</span></td>" +
        "<td>" + esc(b.size) + " GB</td>" +
        "<td>" + backupStateBadge(b.state) + "</td>" +
        "<td>" + esc(MBot.formatTime(b.time_created)) + "</td>" +
        '<td><div class="row-actions">' +
          '<button type="button" class="btn btn-sm btn-primary" data-act="restore"' +
            (canRestore ? "" : " disabled title=\"只有 AVAILABLE 状态的备份才能恢复\"") + ">恢复</button>" +
          '<button type="button" class="btn btn-sm btn-danger" data-act="delete-backup">删除</button>' +
        "</div></td>" +
      "</tr>"
    );
  }

  function selectedBackupCount() {
    var n = 0;
    for (var id in selectedBackups) if (selectedBackups[id]) n++;
    return n;
  }

  function updateBackupSelectionUI() {
    var count = selectedBackupCount();
    var btn = document.getElementById("btn-delete-selected-backups");
    btn.disabled = count === 0;
    btn.textContent = "删除所选" + (count ? "（" + count + "）" : "");
    var all = document.getElementById("backup-select-all");
    var boxes = document.querySelectorAll(".backup-check");
    all.checked = boxes.length > 0 &&
      Array.prototype.every.call(boxes, function (box) { return box.checked; });
  }

  function renderBackups() {
    var tbody = document.getElementById("backup-tbody");
    if (backups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">暂无备份</td></tr>';
    } else {
      tbody.innerHTML = backups.map(backupRow).join("");
    }
    updateBackupSelectionUI();
  }

  function render() {
    // Deleted / deleting volumes are never shown; the main table only holds non-deleted volumes, AVAILABLE first. 已删除 / 删除中的盘一律不显示；主表只放未删除的盘，AVAILABLE 在前。
    var live = [];
    volumes.forEach(function (v) {
      var s = (norm(v).state || "").toUpperCase();
      if (s !== "TERMINATED" && s !== "TERMINATING") live.push(v);
    });
    live.sort(function (a, b) {
      var sa = (norm(a).state || "").toUpperCase() === "AVAILABLE" ? 0 : 1;
      var sb = (norm(b).state || "").toUpperCase() === "AVAILABLE" ? 0 : 1;
      return sa - sb;
    });
    var tbody = document.getElementById("volume-tbody");
    var empty = document.getElementById("volume-empty");
    empty.hidden = live.length !== 0;
    tbody.innerHTML = live.map(function (v) { return volumeRow(v, true); }).join("");

    var orphanTbody = document.getElementById("orphan-tbody");
    if (orphans.length === 0) {
      orphanTbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">没有孤儿盘</td></tr>';
    } else {
      orphanTbody.innerHTML = orphans.map(function (v) { return volumeRow(v, false); }).join("");
    }
    renderBackups();
  }

  // When switching accounts / on first load, show "loading..." instead of an empty list: an empty list is misread as "no data", and a quota title that is not reset keeps flashing the old account's numbers. 切账号 / 初次加载时先显示“加载中…”，而不是空列表：空列表会被误读为“没有数据”，且配额标题若不重置会一直闪旧账号的数字。
  function showLoading() {
    document.getElementById("volume-tbody").innerHTML =
      '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    document.getElementById("volume-empty").hidden = true;
    document.getElementById("orphan-tbody").innerHTML =
      '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    document.getElementById("backup-tbody").innerHTML =
      '<tr><td colspan="7" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    var q = document.getElementById("backup-quota");
    q.textContent = "免费额度：加载中…";
    q.style.color = "";
  }

  // Load the backup list + quota (through the frontend cache; force=true skips the cache and re-fetches). 加载备份列表 + 配额（走前端缓存；force=true 时跳过缓存重新拉取）。
  // The two requests go out in parallel (quota and list are independent); each degrades on its own failure. 两个请求并行发（配额与列表无关），各自失败时各自降级。
  async function loadBackups(force) {
    var gen = loadGen;  // Capture the current generation; an old generation no longer writes back after an account switch. 捕获当前代次，切账号后旧代次不再写回。
    var prof = (currentProfile || "DEFAULT") + ":" + (currentRegion || "");
    var backupsPromise = MBot.cachedApi("bb:list:" + prof,
      MBot.withProfile("/api/boot-backups", currentProfile, currentRegion), !!force);
    var quotaPromise = MBot.cachedApi("bb:quota:" + prof,
      MBot.withProfile("/api/boot-backups/quota", currentProfile, currentRegion), !!force);
    // Attach an empty catch first, so an early return from switching accounts mid-flight does not leave an unhandled rejection warning in the console (the error still throws normally when the original promise is awaited later). 先挂一个空 catch，避免中途切账号提前 return 后未处理的 rejection 在控制台刷警告（后面 await 原 promise 时错误仍会正常抛出）。
    backupsPromise.catch(function () {});
    quotaPromise.catch(function () {});
    try {
      var data = await backupsPromise;
      if (gen !== loadGen) return;
      backups = data.backups || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("backup-tbody").innerHTML =
        '<tr><td colspan="7" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
      backups = [];
    }
    try {
      var quota = await quotaPromise;
      if (gen !== loadGen) return;
      backupPaid = !!quota.paid;
      var el = document.getElementById("backup-quota");
      if (quota.paid) {
        el.textContent = "付费账号（备份不限 5 个，共 " + quota.count + " 个）";
        el.style.color = "";
      } else {
        el.textContent = "免费额度：" + quota.count + " / " + quota.limit +
          (quota.full ? "（已满，创建时自动删最早备份）" : "");
        el.style.color = quota.full ? "var(--danger)" : "";
      }
      document.getElementById("btn-toggle-paid").textContent =
        backupPaid ? "设为免费账号" : "设为付费账号";
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("backup-quota").textContent = "";
    }
    renderBackups();
  }

  // Load the volume list + orphan volumes (through the frontend cache; force=true skips the cache and re-fetches). 加载卷列表 + 孤儿盘（走前端缓存；force=true 时跳过缓存重新拉取）。
  // The two requests go out in parallel and degrade independently; force sends refresh=1 to bypass the 30s server cache. 两个请求并行发，各自失败时各自降级；force 时带 refresh=1 绕过服务端 30 秒缓存。
  async function loadAll(force) {
    var gen = ++loadGen;  // A new generation starts loading; the old generation's results are void. 新一代次加载开始，旧代次的结果作废。
    var prof = (currentProfile || "DEFAULT") + ":" + (currentRegion || "");
    var volUrl = MBot.withProfile("/api/volumes", currentProfile, currentRegion);
    var orphUrl = MBot.withProfile("/api/volumes/orphans", currentProfile, currentRegion);
    if (force) {
      volUrl += (volUrl.indexOf("?") === -1 ? "?" : "&") + "refresh=1";
      orphUrl += (orphUrl.indexOf("?") === -1 ? "?" : "&") + "refresh=1";
    }
    var volPromise = MBot.cachedApi("vol:list:" + prof, volUrl, !!force);
    var orphPromise = MBot.cachedApi("vol:orphans:" + prof, orphUrl, !!force);
    volPromise.catch(function () {});
    orphPromise.catch(function () {});
    try {
      var data = await volPromise;
      if (gen !== loadGen) return;
      volumes = data.volumes || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("volume-tbody").innerHTML =
        '<tr><td colspan="6" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
    }
    try {
      var o = await orphPromise;
      if (gen !== loadGen) return;
      // Compatible with both return key names: orphans or volumes. 兼容两种返回键名：orphans 或 volumes。
      orphans = o.orphans || o.volumes || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("orphan-tbody").innerHTML =
        '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">孤儿盘加载失败</td></tr>';
    }
    if (gen !== loadGen) return;
    render();
    // The backup list needs the volume list for the source-volume name mapping, so fetch it only after volumes have loaded. 备份列表依赖卷列表做来源卷名映射，所以在卷加载完之后再拉。
    await loadBackups(force);
  }

  function findVolume(id) {
    return volumes.concat(orphans).find(function (v) { return String(v.id) === id; });
  }

  function findBackup(id) {
    return backups.find(function (b) { return String(b.backupId || b.id) === id; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // Fill the profile dropdown first (restoring the last choice), then the region dropdown (only for multi-region accounts), then load the list for the current account + region; when the profiles API fails, still load with the default account so the page is never blank. 先填 profile 下拉（恢复上次选择），再填区域下拉（多区账号才显示），最后按当前账号 + 区域加载列表；profiles 接口失败时仍按默认账号加载，不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    var regionSelect = document.getElementById("region-select");
    showLoading();
    MBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadAll();
    }).catch(function () {
      loadAll();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      MBot.setProfile(currentProfile);
      // Switching accounts resets the region to the new account's home region (same as the bot). 切账号时区域重置为新账号的主区（与机器人端一致）。
      MBot.setRegion(null);
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      // When switching accounts, clear the list and show loading (including the quota title) first, then load the new account's data, so old-account data is not shown until the new data arrives (same approach as instances.js). 切账号时先清空列表、显示加载中（含配额标题），再加载新账号数据，避免旧账号数据一直显示到新数据回来（同 instances.js 的做法；浏览器实测：不清空时切换后仍显示旧账号数据，配额标题还会闪旧数字）。
      volumes = [];
      orphans = [];
      backups = [];
      selectedBackups = {};
      showLoading();
      loadAll(true);
    });
    regionSelect.addEventListener("change", function (e) {
      currentRegion = e.target.value;
      MBot.setRegion(currentRegion);
      volumes = [];
      orphans = [];
      backups = [];
      selectedBackups = {};
      showLoading();
      loadAll(true);
    });
    document.getElementById("btn-refresh-volumes").addEventListener("click", function () { loadAll(true); });

    ["volume-tbody", "orphan-tbody"].forEach(function (tbodyId) {
      document.getElementById(tbodyId).addEventListener("click", function (e) {
        var btn = e.target.closest("[data-act]");
        if (!btn) return;
        var row = btn.closest("[data-volume-id]");
        var vol = findVolume(row.getAttribute("data-volume-id"));
        if (!vol) return;
        var act = btn.getAttribute("data-act");
        if (act === "resize") {
          resizeTarget = norm(vol);
          document.getElementById("resize-name").textContent = resizeTarget.name;
          document.getElementById("resize-current").textContent = resizeTarget.size + " GB";
          var input = document.getElementById("resize-size");
          input.min = resizeTarget.size + 1;
          input.value = resizeTarget.size + 10;
          MBot.openModal("resize-modal");
        } else if (act === "vpus") {
          vpusTarget = norm(vol);
          document.getElementById("vpus-name").textContent = vpusTarget.name;
          document.getElementById("vpus-current").textContent =
            vpusTarget.vpus != null ? vpusTarget.vpus : "未知";
          document.getElementById("vpus-value").value =
            vpusTarget.vpus != null ? String(vpusTarget.vpus) : "10";
          MBot.openModal("vpus-modal");
        } else if (act === "backup") {
          var bv = norm(vol);
          MBot.backupBootVolume(bv.id, currentProfile).then(function (created) {
            if (created) loadBackups(true);
          });
        } else if (act === "delete") {
          deleteTarget = norm(vol);
          document.getElementById("delete-volume-name").textContent = deleteTarget.name;
          MBot.openModal("delete-volume-modal");
        }
      });
    });

    document.getElementById("backup-tbody").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn || btn.disabled) return;
      var row = btn.closest("[data-backup-id]");
      var raw = findBackup(row.getAttribute("data-backup-id"));
      if (!raw) return;
      var act = btn.getAttribute("data-act");
      if (act === "restore") {
        restoreTarget = normBackup(raw);
        document.getElementById("restore-backup-name").textContent = restoreTarget.name;
        document.getElementById("restore-name").value = restoreTarget.name + "-restored";
        document.getElementById("restore-shape").value = "auto";
        document.getElementById("restore-flex-group").hidden = true;
        MBot.openModal("restore-modal");
      } else if (act === "delete-backup") {
        deleteBackupTarget = normBackup(raw);
        document.getElementById("delete-backup-name").textContent = deleteBackupTarget.name;
        MBot.openModal("delete-backup-modal");
      }
    });

    document.getElementById("backup-tbody").addEventListener("change", function (e) {
      var box = e.target.closest(".backup-check");
      if (!box) return;
      selectedBackups[box.getAttribute("data-backup-id")] = box.checked;
      updateBackupSelectionUI();
    });
    document.getElementById("backup-select-all").addEventListener("change", function (e) {
      var checked = e.target.checked;
      document.querySelectorAll(".backup-check").forEach(function (box) {
        box.checked = checked;
        selectedBackups[box.getAttribute("data-backup-id")] = checked;
      });
      updateBackupSelectionUI();
    });

    document.getElementById("btn-delete-selected-backups").addEventListener("click", async function () {
      var ids = Object.keys(selectedBackups).filter(function (id) { return selectedBackups[id]; });
      if (ids.length === 0) return;
      var names = ids.map(function (id) {
        var raw = findBackup(id);
        return raw ? normBackup(raw).name : id;
      });
      var ok = await MBot.confirmDialog(
        "删除所选备份",
        "<p>即将删除 " + ids.length + " 个备份：</p><p>" +
          names.map(function (n) { return "• " + MBot.escapeHtml(n); }).join("<br>") +
          "</p><p>删除后无法再用它们恢复实例，不可恢复。</p>",
        "确认删除 " + ids.length + " 个备份", true);
      if (!ok) return;
      try {
        var res = await MBot.api(MBot.withProfile("/api/boot-backups/delete-batch", currentProfile, currentRegion), {
          method: "POST",
          body: { backup_ids: ids },
        });
        var deleted = (res.deleted || []).length;
        var failed = res.failed || [];
        MBot.toast(failed.length
          ? "已删除 " + deleted + " 个备份，" + failed.length + " 个失败"
          : "已删除 " + deleted + " 个备份", failed.length ? "error" : "success");
        selectedBackups = {};
        MBot.cacheClear("bb:");
        await loadBackups(true);
      } catch (err) {
        MBot.toast("批量删除失败：" + err.message, "error");
      }
    });

    // Paid-account switch: shares the same config as the bot. 付费账号开关：与机器人端共用同一份配置。
    document.getElementById("btn-toggle-paid").addEventListener("click", async function () {
      try {
        var res = await MBot.api(MBot.withProfile("/api/boot-backups/paid", currentProfile, currentRegion), {
          method: "POST",
          body: { paid: !backupPaid },
        });
        backupPaid = !!res.paid;
        MBot.toast(backupPaid ? "已设为付费账号，备份不限 5 个" : "已设为免费账号，备份上限 5 个", "success");
        MBot.cacheClear("bb:");
        await loadBackups(true);
      } catch (err) {
        MBot.toast("切换失败：" + err.message, "error");
      }
    });

    // Restore form: CPU/memory choices show only when the shape is A1; other shapes hide them. 恢复表单：形状选 A1 时显示 CPU/内存选择，其他形状隐藏。
    document.getElementById("restore-shape").addEventListener("change", function (e) {
      document.getElementById("restore-flex-group").hidden =
        e.target.value !== "VM.Standard.A1.Flex";
    });

    // Confirm restore from backup (the original machine is untouched; a new instance is built, and rebuilding the boot volume takes a few minutes). 确认从备份恢复（原机不动，新开一台实例；启动盘重建需几分钟）。
    document.getElementById("restore-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!restoreTarget) return;
      var name = document.getElementById("restore-name").value.trim();
      if (!name) {
        MBot.toast("新实例名不能为空", "error");
        return;
      }
      var shape = document.getElementById("restore-shape").value;
      var body = { display_name: name, shape: shape, confirm: true };
      if (shape === "VM.Standard.A1.Flex") {
        body.ocpus = parseInt(document.getElementById("restore-ocpus").value, 10);
        body.memory_gb = parseInt(document.getElementById("restore-memory").value, 10);
      }
      var submitBtn = e.target.querySelector('button[type="submit"]');
      submitBtn.disabled = true;
      MBot.toast("正在从备份重建启动盘并开机，请耐心等待…", "info");
      try {
        var res = await MBot.api(MBot.withProfile("/api/boot-backups/" + encodeURIComponent(restoreTarget.id) + "/restore", currentProfile, currentRegion), {
          method: "POST",
          body: body,
        });
        MBot.toast("恢复成功，新实例 ID：" + (res.instance_id || "").slice(-12), "success");
        MBot.closeModal("restore-modal");
        MBot.cacheClear("inst:");
      } catch (err) {
        MBot.toast("恢复失败：" + err.message, "error");
      } finally {
        submitBtn.disabled = false;
      }
    });

    document.getElementById("btn-confirm-delete-backup").addEventListener("click", async function () {
      if (!deleteBackupTarget) return;
      try {
        await MBot.api(MBot.withProfile("/api/boot-backups/" + encodeURIComponent(deleteBackupTarget.id) + "?confirm=true", currentProfile, currentRegion), { method: "DELETE" });
        MBot.toast("备份已删除", "success");
        MBot.closeModal("delete-backup-modal");
        MBot.cacheClear("bb:");
        await loadBackups(true);
      } catch (err) {
        MBot.toast("删除备份失败：" + err.message, "error");
      }
    });

    document.getElementById("resize-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!resizeTarget) return;
      var newSize = parseInt(document.getElementById("resize-size").value, 10);
      if (!newSize || newSize <= resizeTarget.size) {
        MBot.toast("新容量必须大于当前大小", "error");
        return;
      }
      try {
        await MBot.api(MBot.withProfile("/api/volumes/" + encodeURIComponent(resizeTarget.id) + "/resize", currentProfile, currentRegion), {
          method: "POST",
          body: { size_in_gbs: newSize },
        });
        MBot.toast("扩容已提交", "success");
        MBot.closeModal("resize-modal");
        MBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        MBot.toast("扩容失败：" + err.message, "error");
      }
    });

    document.getElementById("vpus-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!vpusTarget) return;
      var newVpus = parseInt(document.getElementById("vpus-value").value, 10);
      if ([10, 20, 30].indexOf(newVpus) === -1) {
        MBot.toast("VPU 只能是 10、20 或 30", "error");
        return;
      }
      try {
        await MBot.api(MBot.withProfile("/api/volumes/" + encodeURIComponent(vpusTarget.id) + "/vpus", currentProfile, currentRegion), {
          method: "POST",
          body: { vpus_per_gb: newVpus },
        });
        MBot.toast("VPU 已改为 " + newVpus, "success");
        MBot.closeModal("vpus-modal");
        MBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        MBot.toast("改 VPU 失败：" + err.message, "error");
      }
    });

    document.getElementById("btn-confirm-delete-volume").addEventListener("click", async function () {
      if (!deleteTarget) return;
      try {
        await MBot.api(MBot.withProfile("/api/volumes/" + encodeURIComponent(deleteTarget.id), currentProfile, currentRegion), { method: "DELETE" });
        MBot.toast("启动盘已删除", "success");
        MBot.closeModal("delete-volume-modal");
        MBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        MBot.toast("删除失败：" + err.message, "error");
      }
    });
  });
})();
