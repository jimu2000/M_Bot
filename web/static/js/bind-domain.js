/* 实例详情“绑定域名”弹窗（v2.01）。
 * 详情弹窗里的“🌐 绑定域名”按钮打开本弹窗：先选主域名（Zone，
 * 来自 GET /api/dns/zones，与 DNS 管理页同一接口），再输二级域名，
 * 提交到 POST /api/instances/{id}/bind-domain 建 A 记录。
 * 实例 OCID 与公网 IP 从详情弹窗读（instances.js 的 showDetail
 * 会把 OCID 写进 #detail-modal 的 dataset.instanceId，
 * 公网 IP 显示在 #detail-public-ip）。无公网 IP 时按钮本身隐藏，
 * 这里再兜底校验一次。 */
(function () {
  "use strict";

  var MBot = window.MBot;
  var I18n = window.MBotI18n;

  var SUBDOMAIN_RE = /^[A-Za-z0-9_.\-]+$/;

  function t(key, params) {
    return I18n ? I18n.t(key, params) : key;
  }

  function $(id) { return document.getElementById(id); }

  function validSubdomain(text) {
    var sub = (text || "").trim().toLowerCase().replace(/\.$/, "");
    if (sub === "@") return true;
    if (!sub || sub.length > 253) return false;
    if (!SUBDOMAIN_RE.test(sub)) return false;
    if (sub.charAt(0) === "-" || sub.charAt(0) === "." ||
        sub.charAt(sub.length - 1) === "-" ||
        sub.charAt(sub.length - 1) === ".") return false;
    if (sub.indexOf("..") !== -1) return false;
    var labels = sub.split(".");
    for (var i = 0; i < labels.length; i++) {
      if (!labels[i] || labels[i].length > 63) return false;
    }
    return true;
  }

  function currentInstance() {
    var modal = $("detail-modal");
    var id = modal && modal.dataset ? modal.dataset.instanceId : "";
    var ipEl = $("detail-public-ip");
    var ip = ipEl ? (ipEl.textContent || "").trim() : "";
    if (ip === "—") ip = "";
    var nameEl = $("detail-name");
    return {
      id: id || "",
      ip: ip,
      name: nameEl ? (nameEl.textContent || "").trim() : "",
    };
  }

  async function loadZones(selectEl) {
    selectEl.innerHTML = "";
    var loading = document.createElement("option");
    loading.textContent = t("instances.bind_domain_loading_zones");
    selectEl.appendChild(loading);
    var data = await MBot.api("/api/dns/zones");
    var zones = (data && data.zones) || [];
    selectEl.innerHTML = "";
    zones.forEach(function (z) {
      var opt = document.createElement("option");
      opt.value = z.id;
      opt.textContent = z.name;
      selectEl.appendChild(opt);
    });
    return zones;
  }

  function showResult(text, isError) {
    var el = $("bind-domain-result");
    el.hidden = false;
    el.textContent = text;
    el.style.color = isError ? "var(--danger, #c0392b)" : "";
  }

  function openBindModal() {
    var inst = currentInstance();
    if (!inst.id || !inst.ip) {
      MBot.toast(t("instances.bind_domain_no_ip"), "error");
      return;
    }
    $("bind-domain-instance").textContent = inst.name || inst.id;
    $("bind-domain-ip").textContent = inst.ip;
    $("bind-domain-subdomain").value = "";
    $("bind-domain-exists").hidden = true;
    $("bind-domain-result").hidden = true;
    $("bind-domain-overwrite-btn").hidden = true;
    $("bind-domain-submit").disabled = false;
    MBot.closeModal("detail-modal");
    MBot.openModal("bind-domain-modal");
    loadZones($("bind-domain-zone")).catch(function (err) {
      showResult(t("instances.bind_domain_no_credentials") +
        "（" + (err.message || err) + "）", true);
    });
  }

  async function submitBind(overwrite) {
    var inst = currentInstance();
    var zoneId = $("bind-domain-zone").value;
    var sub = ($("bind-domain-subdomain").value || "").trim();
    if (!zoneId) return;
    if (!validSubdomain(sub)) {
      MBot.toast(t("instances.bind_domain_invalid"), "error");
      return;
    }
    var submitBtn = $("bind-domain-submit");
    submitBtn.disabled = true;
    try {
      var url = MBot.withProfile(
        "/api/instances/" + encodeURIComponent(inst.id) + "/bind-domain",
        MBot.getProfile(), MBot.getRegion());
      var res = await MBot.api(url, {
        method: "POST",
        body: { zone_id: zoneId, subdomain: sub, overwrite: !!overwrite },
      });
      var doneKey = res.action === "updated"
        ? "instances.bind_domain_updated"
        : (res.action === "exists_same"
          ? "instances.bind_domain_exists_same"
          : "instances.bind_domain_done");
      showResult(t(doneKey, { fqdn: res.fqdn, ip: res.ip,
                              old_ip: "" }), false);
      $("bind-domain-exists").hidden = true;
      $("bind-domain-overwrite-btn").hidden = true;
      submitBtn.disabled = false;
    } catch (err) {
      submitBtn.disabled = false;
      var data = err.data || {};
      if (err.status === 409 && data.exists) {
        var existsEl = $("bind-domain-exists");
        existsEl.hidden = false;
        existsEl.textContent = t("instances.bind_domain_exists", {
          fqdn: data.fqdn,
          old_ip: (data.existing && data.existing.content) || "?",
          ip: inst.ip,
        });
        $("bind-domain-overwrite-btn").hidden = false;
        return;
      }
      MBot.toast(err.message || err, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var openBtn = $("detail-bind-domain-btn");
    if (openBtn) openBtn.addEventListener("click", openBindModal);
    var form = $("bind-domain-form");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      submitBind(false);
    });
    var overwriteBtn = $("bind-domain-overwrite-btn");
    if (overwriteBtn) overwriteBtn.addEventListener("click", function () {
      submitBind(true);
    });
  });
})();
