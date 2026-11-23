/* 启动盘逻辑：卷表格、扩容、删除、孤儿盘列表、启动卷备份 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var volumes = [];
  var orphans = [];
  var backups = [];
  var currentProfile = null; // 当前账号（profile），由 fillProfileSelect 恢复
  var loadGen = 0; // 加载代次：每次 loadAll 递增，旧代次的异步结果不再
                   // 写回共享列表，避免快速切账号时新旧数据串台。
  var resizeTarget = null;
  var deleteTarget = null;
  var vpusTarget = null;
  var restoreTarget = null; // 恢复目标备份（归一化后）
  var deleteBackupTarget = null;

  // 字段归一化：后端返回 R-Bot 形状（camelCase）
  // {volumeId, attachmentId, displayName, sizeInGBs, vpusPerGB,
  //  lifecycleState, instanceId, instanceName, attached, attachmentState,
  //  availabilityDomain, timeCreated}，过渡期同时双写旧字段，
  // 这里优先认新字段，旧字段作兜底。
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
    if (s === "AVAILABLE") return '<span class="badge badge-green">AVAILABLE</span>';
    if (s === "IN-USE") return '<span class="badge badge-blue">IN-USE</span>';
    if (s === "PROVISIONING" || s === "RESTORING") return '<span class="badge badge-amber">' + RBot.escapeHtml(state) + "</span>";
    if (s === "TERMINATED" || s === "FAULTY") return '<span class="badge badge-red">' + RBot.escapeHtml(state) + "</span>";
    return '<span class="badge badge-gray">' + RBot.escapeHtml(state || "—") + "</span>";
  }

  function volumeRow(raw, showInstance) {
    var esc = RBot.escapeHtml;
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
    return (
      "<tr data-volume-id=\"" + esc(v.id) + "\">" +
        "<td><b>" + esc(v.name) + "</b></td>" +
        "<td>" + esc(v.size) + " GB</td>" +
        vpusCell +
        "<td>" + stateBadge(v.state) + "</td>" +
        instanceCell +
        '<td><div class="row-actions">' +
          '<button type="button" class="btn btn-sm" data-act="resize">扩容</button>' +
          '<button type="button" class="btn btn-sm" data-act="vpus">改 VPU</button>' +
          '<button type="button" class="btn btn-sm" data-act="backup">备份</button>' +
          '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
        "</div></td>" +
      "</tr>"
    );
  }

  // 备份字段归一化：后端返回 R-Bot 形状（camelCase）
  // {backupId, displayName, bootVolumeId, sizeInGBs, lifecycleState,
  //  sourceType, type, timeCreated, imageId}，过渡期同时双写旧字段。
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
    if (s === "AVAILABLE") return '<span class="badge badge-green">AVAILABLE</span>';
    if (s === "CREATING" || s === "REQUEST_RECEIVED") return '<span class="badge badge-amber">' + RBot.escapeHtml(state) + "</span>";
    if (s === "TERMINATED" || s === "FAULTY") return '<span class="badge badge-red">' + RBot.escapeHtml(state) + "</span>";
    return '<span class="badge badge-gray">' + RBot.escapeHtml(state || "—") + "</span>";
  }

  // 来源卷名：备份记录只有 bootVolumeId，在已加载的卷列表里找名字，
  // 找不到（来源盘已删）就显示截断的 OCID。
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
    var esc = RBot.escapeHtml;
    var b = normBackup(raw);
    var canRestore = (b.state || "").toUpperCase() === "AVAILABLE";
    return (
      "<tr data-backup-id=\"" + esc(b.id) + "\">" +
        "<td><b>" + esc(b.name) + "</b>" +
          (b.type ? '<br><span style="color: var(--text-faint); font-size: 12px;">' + esc(b.type) + "</span>" : "") + "</td>" +
        "<td><span class=\"mono\" title=\"" + esc(b.boot_volume_id || "") + "\">" + esc(sourceVolumeName(b.boot_volume_id)) + "</span></td>" +
        "<td>" + esc(b.size) + " GB</td>" +
        "<td>" + backupStateBadge(b.state) + "</td>" +
        "<td>" + esc(RBot.formatTime(b.time_created)) + "</td>" +
        '<td><div class="row-actions">' +
          '<button type="button" class="btn btn-sm btn-primary" data-act="restore"' +
            (canRestore ? "" : " disabled title=\"只有 AVAILABLE 状态的备份才能恢复\"") + ">恢复</button>" +
          '<button type="button" class="btn btn-sm btn-danger" data-act="delete-backup">删除</button>' +
        "</div></td>" +
      "</tr>"
    );
  }

  function renderBackups() {
    var tbody = document.getElementById("backup-tbody");
    if (backups.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">暂无备份</td></tr>';
    } else {
      tbody.innerHTML = backups.map(backupRow).join("");
    }
  }

  function render() {
    var tbody = document.getElementById("volume-tbody");
    var empty = document.getElementById("volume-empty");
    empty.hidden = volumes.length !== 0;
    tbody.innerHTML = volumes.map(function (v) { return volumeRow(v, true); }).join("");

    var orphanTbody = document.getElementById("orphan-tbody");
    if (orphans.length === 0) {
      orphanTbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">没有孤儿盘</td></tr>';
    } else {
      orphanTbody.innerHTML = orphans.map(function (v) { return volumeRow(v, false); }).join("");
    }
    renderBackups();
  }

  // 切账号 / 初次加载时先显示“加载中…”，而不是空列表：
  // 空列表会被误读为“没有数据”，且配额标题若不重置会一直闪
  // 旧账号的数字（浏览器实测 2026-11-24）。
  function showLoading() {
    document.getElementById("volume-tbody").innerHTML =
      '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    document.getElementById("volume-empty").hidden = true;
    document.getElementById("orphan-tbody").innerHTML =
      '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    document.getElementById("backup-tbody").innerHTML =
      '<tr><td colspan="6" style="text-align: center; color: var(--text-muted);">加载中…</td></tr>';
    var q = document.getElementById("backup-quota");
    q.textContent = "免费额度：加载中…";
    q.style.color = "";
  }

  // 加载备份列表 + 配额（走前端缓存；force=true 时跳过缓存重新拉取）
  async function loadBackups(force) {
    var gen = loadGen; // 捕获当前代次，切账号后旧代次不再写回
    var prof = currentProfile || "DEFAULT";
    try {
      var data = await RBot.cachedApi("bb:list:" + prof,
        RBot.withProfile("/api/boot-backups", currentProfile), !!force);
      if (gen !== loadGen) return;
      backups = data.backups || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("backup-tbody").innerHTML =
        '<tr><td colspan="6" style="text-align: center; color: var(--danger);">加载失败：' +
        RBot.escapeHtml(err.message) + "</td></tr>";
      backups = [];
    }
    try {
      var quota = await RBot.cachedApi("bb:quota:" + prof,
        RBot.withProfile("/api/boot-backups/quota", currentProfile), !!force);
      if (gen !== loadGen) return;
      var el = document.getElementById("backup-quota");
      el.textContent = "免费额度：" + quota.count + " / " + quota.limit +
        (quota.full ? "（已满）" : "");
      el.style.color = quota.full ? "var(--danger)" : "";
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("backup-quota").textContent = "";
    }
    renderBackups();
  }

  // 加载卷列表 + 孤儿盘（走前端缓存；force=true 时跳过缓存重新拉取）
  async function loadAll(force) {
    var gen = ++loadGen; // 新一代次加载开始，旧代次的结果作废
    var prof = currentProfile || "DEFAULT";
    try {
      var data = await RBot.cachedApi("vol:list:" + prof,
        RBot.withProfile("/api/volumes", currentProfile), !!force);
      if (gen !== loadGen) return;
      volumes = data.volumes || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("volume-tbody").innerHTML =
        '<tr><td colspan="6" style="text-align: center; color: var(--danger);">加载失败：' +
        RBot.escapeHtml(err.message) + "</td></tr>";
    }
    try {
      var o = await RBot.cachedApi("vol:orphans:" + prof,
        RBot.withProfile("/api/volumes/orphans", currentProfile), !!force);
      if (gen !== loadGen) return;
      // 兼容两种返回键名：orphans 或 volumes
      orphans = o.orphans || o.volumes || [];
    } catch (err) {
      if (gen !== loadGen) return;
      document.getElementById("orphan-tbody").innerHTML =
        '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">孤儿盘加载失败</td></tr>';
    }
    if (gen !== loadGen) return;
    render();
    // 备份列表依赖卷列表做来源卷名映射，所以在卷加载完之后再拉。
    await loadBackups(force);
  }

  function findVolume(id) {
    return volumes.concat(orphans).find(function (v) { return String(v.id) === id; });
  }

  function findBackup(id) {
    return backups.find(function (b) { return String(b.backupId || b.id) === id; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 先填 profile 下拉（恢复上次选择），再按当前账号加载列表；
    // profiles 接口失败时仍按默认账号加载，不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    showLoading();
    RBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      loadAll();
    }).catch(function () {
      loadAll();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      RBot.setProfile(currentProfile);
      // 切账号时先清空列表、显示加载中（含配额标题），再加载新
      // 账号数据，避免旧账号数据一直显示到新数据回来（同
      // instances.js v1.6.0 的做法；浏览器实测 2026-11-24：不清空
      // 时切换后仍显示旧账号数据，配额标题还会闪旧数字）。
      volumes = [];
      orphans = [];
      backups = [];
      showLoading();
      loadAll(true);
    });
    document.getElementById("btn-refresh-volumes").addEventListener("click", function () { loadAll(true); });

    // 两张表共用一套操作按钮处理
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
          RBot.openModal("resize-modal");
        } else if (act === "vpus") {
          vpusTarget = norm(vol);
          document.getElementById("vpus-name").textContent = vpusTarget.name;
          document.getElementById("vpus-current").textContent =
            vpusTarget.vpus != null ? vpusTarget.vpus : "未知";
          document.getElementById("vpus-value").value =
            vpusTarget.vpus != null ? String(vpusTarget.vpus) : "10";
          RBot.openModal("vpus-modal");
        } else if (act === "backup") {
          // 备份启动卷：配额检查 + 满了提示删最早，全在共享流程里。
          var bv = norm(vol);
          RBot.backupBootVolume(bv.id, currentProfile).then(function (created) {
            if (created) loadBackups(true);
          });
        } else if (act === "delete") {
          deleteTarget = norm(vol);
          document.getElementById("delete-volume-name").textContent = deleteTarget.name;
          RBot.openModal("delete-volume-modal");
        }
      });
    });

    // 备份表的操作按钮：恢复 / 删除
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
        RBot.openModal("restore-modal");
      } else if (act === "delete-backup") {
        deleteBackupTarget = normBackup(raw);
        document.getElementById("delete-backup-name").textContent = deleteBackupTarget.name;
        RBot.openModal("delete-backup-modal");
      }
    });

    // 恢复表单：形状选 A1 时显示 CPU/内存选择，其他形状隐藏
    document.getElementById("restore-shape").addEventListener("change", function (e) {
      document.getElementById("restore-flex-group").hidden =
        e.target.value !== "VM.Standard.A1.Flex";
    });

    // 确认从备份恢复（原机不动，新开一台实例；启动盘重建需几分钟）
    document.getElementById("restore-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!restoreTarget) return;
      var name = document.getElementById("restore-name").value.trim();
      if (!name) {
        RBot.toast("新实例名不能为空", "error");
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
      RBot.toast("正在从备份重建启动盘并开机，请耐心等待…", "info");
      try {
        var res = await RBot.api(RBot.withProfile("/api/boot-backups/" + encodeURIComponent(restoreTarget.id) + "/restore", currentProfile), {
          method: "POST",
          body: body,
        });
        RBot.toast("恢复成功，新实例 ID：" + (res.instance_id || "").slice(-12), "success");
        RBot.closeModal("restore-modal");
        RBot.cacheClear("inst:");
      } catch (err) {
        RBot.toast("恢复失败：" + err.message, "error");
      } finally {
        submitBtn.disabled = false;
      }
    });

    // 确认删除备份
    document.getElementById("btn-confirm-delete-backup").addEventListener("click", async function () {
      if (!deleteBackupTarget) return;
      try {
        await RBot.api(RBot.withProfile("/api/boot-backups/" + encodeURIComponent(deleteBackupTarget.id) + "?confirm=true", currentProfile), { method: "DELETE" });
        RBot.toast("备份已删除", "success");
        RBot.closeModal("delete-backup-modal");
        RBot.cacheClear("bb:");
        await loadBackups(true);
      } catch (err) {
        RBot.toast("删除备份失败：" + err.message, "error");
      }
    });

    // 确认扩容
    document.getElementById("resize-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!resizeTarget) return;
      var newSize = parseInt(document.getElementById("resize-size").value, 10);
      if (!newSize || newSize <= resizeTarget.size) {
        RBot.toast("新容量必须大于当前大小", "error");
        return;
      }
      try {
        await RBot.api(RBot.withProfile("/api/volumes/" + encodeURIComponent(resizeTarget.id) + "/resize", currentProfile), {
          method: "POST",
          body: { size_in_gbs: newSize },
        });
        RBot.toast("扩容已提交", "success");
        RBot.closeModal("resize-modal");
        RBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        RBot.toast("扩容失败：" + err.message, "error");
      }
    });

    // 确认改 VPU
    document.getElementById("vpus-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!vpusTarget) return;
      var newVpus = parseInt(document.getElementById("vpus-value").value, 10);
      if ([10, 20, 30].indexOf(newVpus) === -1) {
        RBot.toast("VPU 只能是 10、20 或 30", "error");
        return;
      }
      try {
        await RBot.api(RBot.withProfile("/api/volumes/" + encodeURIComponent(vpusTarget.id) + "/vpus", currentProfile), {
          method: "POST",
          body: { vpus_per_gb: newVpus },
        });
        RBot.toast("VPU 已改为 " + newVpus, "success");
        RBot.closeModal("vpus-modal");
        RBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        RBot.toast("改 VPU 失败：" + err.message, "error");
      }
    });

    // 确认删除
    document.getElementById("btn-confirm-delete-volume").addEventListener("click", async function () {
      if (!deleteTarget) return;
      try {
        await RBot.api(RBot.withProfile("/api/volumes/" + encodeURIComponent(deleteTarget.id), currentProfile), { method: "DELETE" });
        RBot.toast("启动盘已删除", "success");
        RBot.closeModal("delete-volume-modal");
        RBot.cacheClear("vol:");
        await loadAll();
      } catch (err) {
        RBot.toast("删除失败：" + err.message, "error");
      }
    });
  });
})();
