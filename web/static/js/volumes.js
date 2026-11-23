/* 启动盘逻辑：卷表格、扩容、删除、孤儿盘列表 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var volumes = [];
  var orphans = [];
  var currentProfile = null; // 当前账号（profile），由 fillProfileSelect 恢复
  var resizeTarget = null;
  var deleteTarget = null;
  var vpusTarget = null;

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
          '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
        "</div></td>" +
      "</tr>"
    );
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
  }

  // 加载卷列表 + 孤儿盘（走前端缓存；force=true 时跳过缓存重新拉取）
  async function loadAll(force) {
    var prof = currentProfile || "DEFAULT";
    try {
      var data = await RBot.cachedApi("vol:list:" + prof,
        RBot.withProfile("/api/volumes", currentProfile), !!force);
      volumes = data.volumes || [];
    } catch (err) {
      document.getElementById("volume-tbody").innerHTML =
        '<tr><td colspan="6" style="text-align: center; color: var(--danger);">加载失败：' +
        RBot.escapeHtml(err.message) + "</td></tr>";
    }
    try {
      var o = await RBot.cachedApi("vol:orphans:" + prof,
        RBot.withProfile("/api/volumes/orphans", currentProfile), !!force);
      // 兼容两种返回键名：orphans 或 volumes
      orphans = o.orphans || o.volumes || [];
    } catch (err) {
      document.getElementById("orphan-tbody").innerHTML =
        '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">孤儿盘加载失败</td></tr>';
    }
    render();
  }

  function findVolume(id) {
    return volumes.concat(orphans).find(function (v) { return String(v.id) === id; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 先填 profile 下拉（恢复上次选择），再按当前账号加载列表；
    // profiles 接口失败时仍按默认账号加载，不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    RBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      loadAll();
    }).catch(function () {
      loadAll();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      RBot.setProfile(currentProfile);
      loadAll();
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
        } else if (act === "delete") {
          deleteTarget = norm(vol);
          document.getElementById("delete-volume-name").textContent = deleteTarget.name;
          RBot.openModal("delete-volume-modal");
        }
      });
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
