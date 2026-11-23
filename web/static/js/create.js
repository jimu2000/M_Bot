/* 创建实例逻辑：四卡表单、快捷开机预设、配额预检、提交创建
 *
 * 注意（与后端 API 的对应关系，以后端实际实现为准）：
 * - 后端 POST /api/create 要求 subnet_id / availability_domain /
 *   ssh_keys_file 必填，表单在卡 3 收集这三项；
 * - ssh_keys_file 必须是面板服务器上的公钥文件路径（后端直传 OCI CLI
 *   --ssh-authorized-keys-file），不支持粘贴公钥内容；
 * - A1 的 ocpus/memory_gb 以 --shape-config 传给 launch（E2 是固定规格，
 *   不传 shape-config），boot_volume_size_gbs / assign_public_ip /
 *   assign_ipv6 / min_delay / max_delay / user_data 全部已接入后端，
 *   由 Grabber 传给 OCI CLI（见 oci/grabber.py）。 */
(function () {
  "use strict";
  var RBot = window.RBot;

  var SHAPE_E2 = "VM.Standard.E2.1.Micro";
  var SHAPE_A1 = "VM.Standard.A1.Flex";

  function $(id) { return document.getElementById(id); }

  function showError(msg) {
    var el = $("create-error");
    el.textContent = msg;
    el.hidden = false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function hideError() { $("create-error").hidden = true; }

  // E2 是固定规格（1 OCPU / 1 GB），选中时锁定 OCPU/内存输入
  function onShapeChange() {
    var isE2 = $("shape").value === SHAPE_E2;
    $("ocpus").disabled = isE2;
    $("memory-gb").disabled = isE2;
    $("e2-hint").hidden = !isE2;
    if (isE2) {
      $("ocpus").value = 1;
      $("memory-gb").value = 1;
      selectImageByArch("x86_64");
    } else {
      selectImageByArch("aarch64");
    }
  }

  function selectImageByArch(arch) {
    var sel = $("image-id");
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].getAttribute("data-arch") === arch) {
        sel.selectedIndex = i;
        return;
      }
    }
  }

  // 快捷开机预设：点一张卡片填满表单
  function applyPreset(card) {
    var shape = card.getAttribute("data-shape");
    $("shape").value = shape;
    onShapeChange();
    if (shape !== SHAPE_E2) {
      $("ocpus").value = card.getAttribute("data-ocpus");
      $("memory-gb").value = card.getAttribute("data-memory");
    }
    selectImageByArch(card.getAttribute("data-arch"));
    var disk = card.getAttribute("data-disk");
    if (disk) $("disk-gb").value = disk;
    document.querySelectorAll(".preset-card").forEach(function (c) {
      c.style.borderColor = "";
      c.style.boxShadow = "";
    });
    card.style.borderColor = "var(--accent)";
    card.style.boxShadow = "var(--shadow)";
    RBot.toast("已应用预设：" + card.querySelector("b").textContent, "success");
  }

  function collectPayload() {
    var shape = $("shape").value;
    var isE2 = shape === SHAPE_E2;
    var name = $("display-name").value.trim();
    if (!name) {
      // 实例名留空时自动生成（时间戳后缀，避免重名）
      var d = new Date();
      name = "rbot-" + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0") +
             "-" + String(d.getHours()).padStart(2, "0") + String(d.getMinutes()).padStart(2, "0");
    }
    return {
      shape: shape,
      ocpus: isE2 ? 1 : parseInt($("ocpus").value, 10),
      memory_gb: isE2 ? 1 : parseInt($("memory-gb").value, 10),
      image_id: $("image-id").value,
      display_name: name,
      // 后端必填三项（卡 3 收集）
      subnet_id: $("subnet-id").value.trim(),
      availability_domain: $("availability-domain").value.trim(),
      ssh_keys_file: $("ssh-keys-file").value.trim(),
      // 已接入后端的扩展字段（Grabber 传给 OCI CLI）
      boot_volume_size_gbs: parseInt($("disk-gb").value, 10),
      assign_public_ip: $("public-ip").checked,
      assign_ipv6: $("ipv6").checked,
      min_delay: parseInt($("min-delay").value, 10) || 0,
      max_delay: parseInt($("max-delay").value, 10) || 0,
      target_cpu: $("target-cpu").value.trim(),
      user_data: $("startup-script").value,
    };
  }

  async function submitCreate(e) {
    e.preventDefault();
    hideError();
    var payload = collectPayload();
    if (!payload.ocpus || payload.ocpus < 1 || !payload.memory_gb || payload.memory_gb < 1) {
      showError("请填写有效的 OCPU 与内存。");
      return;
    }
    if (!payload.availability_domain || !payload.subnet_id || !payload.ssh_keys_file) {
      showError("请填写可用域、子网 OCID 与 SSH 公钥文件路径（卡 3 网络与安全）。");
      return;
    }
    var btn = $("btn-create");
    btn.disabled = true;
    btn.textContent = "配额预检中…";
    try {
      // 先调配额预检
      var check = await RBot.api("/api/quota/can-launch", {
        method: "POST",
        body: { shape: payload.shape, ocpus: payload.ocpus, memory_gb: payload.memory_gb },
      });
      if (!check.can_launch) {
        showError("配额不足，无法创建：" + (check.reason || "免费额度已用完"));
        return;
      }
      // 预检通过，提交创建
      btn.textContent = "创建中…";
      var result = await RBot.api("/api/create", { method: "POST", body: payload });
      RBot.toast("创建任务已提交", "success");
      window.location.href = "/tasks";
    } catch (err) {
      showError("创建失败：" + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "开始创建";
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("shape").addEventListener("change", onShapeChange);
    onShapeChange();
    document.getElementById("preset-grid").addEventListener("click", function (e) {
      var card = e.target.closest(".preset-card");
      if (card) applyPreset(card);
    });
    document.getElementById("preset-grid").addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("preset-card")) {
        e.preventDefault();
        applyPreset(e.target);
      }
    });
    $("create-form").addEventListener("submit", submitCreate);
  });
})();
