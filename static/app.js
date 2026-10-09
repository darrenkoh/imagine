(() => {
  const ASPECTS = [
    { id: "1:1", label: "1:1", w: 1024, h: 1024 },
    { id: "16:9", label: "16:9", w: 1344, h: 768 },
    { id: "9:16", label: "9:16", w: 768, h: 1344 },
    { id: "3:4", label: "3:4", w: 896, h: 1152 },
    { id: "4:3", label: "4:3", w: 1152, h: 896 },
    { id: "2:3", label: "2:3", w: 832, h: 1216 },
  ];
  const STYLES = [
    { id: "", label: "None" },
    { id: "cinematic film still, dramatic lighting, anamorphic bokeh, highly detailed, color graded", label: "Cinematic" },
    { id: "anime key visual, crisp cel shading, clean lineart, vibrant colors, studio lighting", label: "Anime" },
    { id: "photorealistic photograph, 85mm lens, natural light, sharp focus, real skin texture", label: "Photo" },
    { id: "watercolor painting, soft wet-on-wet washes, paper texture, delicate pigments", label: "Watercolor" },
    { id: "pencil sketch, graphite on paper, cross-hatching, hand-drawn linework, monochrome", label: "Sketch" },
    { id: "photorealistic 3D CGI render, Unreal Engine 5, Blender Cycles, Octane render, ray tracing, subsurface scattering, volumetric lighting, detailed materials and shaders, studio HDRI, cinematic 3D look, not a drawing, not 2D illustration", label: "3D" },
  ];

  let aspect = ASPECTS.find((a) => a.id === "3:4") || ASPECTS[0];
  let style = STYLES[0];
  let spicy = localStorage.getItem("imagine_spicy") === "1";
  let nsfwBlur = localStorage.getItem("imagine_nsfw_blur") !== "0";
  let rgba = localStorage.getItem("imagine_rgba") === "1";
  // default ON (missing key => framed)
  let framing = localStorage.getItem("imagine_framing") !== "0";
  let task = "create"; // create | edit | animate
  let editImageId = null;
  let animateImageId = null;
  let h3Video = false;
  let h3Known = false;
  let h3Detail = "";
  const DURATIONS = [5, 6, 10];
  const RESOLUTIONS = [
    { id: "fast", label: "Fast" },
    { id: "768p", label: "768p" },
  ];
  let duration = Number(localStorage.getItem("imagine_h3_duration") || 5);
  if (!DURATIONS.includes(duration)) duration = 5;
  let resolution = localStorage.getItem("imagine_h3_resolution") || "fast";
  if (!RESOLUTIONS.some((mode) => mode.id === resolution)) resolution = "fast";
  let strength = Number(localStorage.getItem('imagine_strength') || 0.65);
  let galleryFilter = localStorage.getItem('imagine_gallery_filter') || 'all';
  let selectMode = false;
  let selectedIds = new Set();
  let deletingIds = new Set();
  // Ids removed locally. A gallery refresh that started before the delete
  // returns must not put those cards back.
  let removedIds = new Set();
  let activeJobId = null;
  let cancelRequested = false;
  let galleryItems = [];
  let lastJob = null;
  let sheetJob = null;
  let timer = null;

  const $ = (id) => document.getElementById(id);
  const pinHeaders = () => {
    const pin = localStorage.getItem("imagine_pin") || "";
    return pin ? { "X-Imagine-Pin": pin } : {};
  };

  async function api(path, opts = {}) {
    const headers = Object.assign(
      { "Content-Type": "application/json" },
      pinHeaders(),
      opts.headers || {}
    );
    const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
    if (res.status === 401) {
      localStorage.removeItem("imagine_pin");
      $("gate").classList.remove("hidden");
      throw new Error("unauthorized");
    }
    const ct = res.headers.get("content-type") || "";
    const body = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      let msg = res.statusText;
      if (body && typeof body === "object") {
        const detail = body.detail;
        msg = typeof detail === "string" ? detail : JSON.stringify(detail || body);
      } else if (typeof body === "string" && body) {
        msg = body;
      }
      throw new Error(msg || res.statusText);
    }
    return body;
  }

  function renderChips(el, items, current, onPick, key = "id") {
    el.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (it[key] === current[key] ? " active" : "");
      b.textContent = it.label;
      b.onclick = () => onPick(it);
      el.appendChild(b);
    });
  }

  function refreshChips() {
    renderChips($("aspectChips"), ASPECTS, aspect, (it) => {
      aspect = it;
      refreshChips();
    });
    renderChips($("styleChips"), STYLES, style, (it) => {
      style = it;
      refreshChips();
    });
    renderModeChips();
    renderTaskChips();
    renderFramingChips();
    renderRgbaChips();
    renderAnimateChips();
  }


  function renderTaskChips() {
    const el = $("taskChips");
    if (!el) return;
    el.innerHTML = "";
    [
      { id: "create", label: "Create" },
      { id: "edit", label: "Edit" },
      { id: "animate", label: "Animate" },
    ].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (task === m.id ? " active" : "");
      b.textContent = m.label;
      b.onclick = () => {
        task = m.id;
        refreshChips();
      };
      el.appendChild(b);
    });
    syncTaskPanels();
  }

  function renderAnimateChips() {
    const dur = $("durationChips");
    if (dur) {
      dur.innerHTML = "";
      DURATIONS.forEach((sec) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip" + (duration === sec ? " active" : "");
        b.textContent = sec + "s";
        b.onclick = () => {
          duration = sec;
          localStorage.setItem("imagine_h3_duration", String(sec));
          refreshChips();
        };
        dur.appendChild(b);
      });
    }
    const res = $("resolutionChips");
    if (res) {
      res.innerHTML = "";
      RESOLUTIONS.forEach((mode) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "chip" + (resolution === mode.id ? " active" : "");
        b.textContent = mode.label;
        b.onclick = () => {
          resolution = mode.id;
          localStorage.setItem("imagine_h3_resolution", mode.id);
          refreshChips();
        };
        res.appendChild(b);
      });
    }
  }

  function syncTaskPanels() {
    const edit = $("editPanel");
    const anim = $("animatePanel");
    const still = $("stillControls");
    if (edit) edit.classList.toggle("hidden", task !== "edit");
    if (anim) anim.classList.toggle("hidden", task !== "animate");
    if (still) still.classList.toggle("hidden", task === "animate");
    const label = $("promptLabel");
    if (label) label.textContent = task === "animate" ? "Motion" : "Prompt";
    const gen = $("generateBtn");
    if (gen && !gen.disabled) gen.textContent = task === "animate" ? "Animate" : "Generate";
    const prompt = $("prompt");
    if (prompt) {
      if (task === "animate") {
        prompt.placeholder = "Slow push-in, leaves stir, light shifts across the scene…";
      } else if (task === "edit") {
        prompt.placeholder = "Describe the edit — e.g. change background to sunset beach…";
      } else {
        prompt.placeholder = "A neon koi swimming through misty bamboo at dusk…";
      }
    }
    const hint = $("h3Hint");
    if (hint) {
      hint.textContent = h3Video
        ? "Local H3 uses this image as the first frame. Leave motion blank for a gentle default."
        : (h3Detail || "Local H3 is not ready.");
    }
  }

  async function useAnimateFile(file) {
    if (!file) return;
    task = "animate";
    refreshChips();
    try {
      const up = await uploadEditFile(file);
      animateImageId = up.id;
      $("animatePreview").src = up.url + "?t=" + Date.now();
      $("animatePreviewWrap").classList.remove("hidden");
      $("prompt").focus();
    } catch (err) {
      alert("Upload failed: " + (err.message || err));
      animateImageId = null;
    }
  }

  async function useJobAsAnimateSource(job) {
    if (!job) return;
    const url = job.image || (job.id ? `/api/images/${job.id}.png` : "");
    if (!url) {
      alert("This item has no still to animate");
      return;
    }
    const blob = await fetch(url, { headers: pinHeaders(), credentials: "same-origin" }).then((r) => {
      if (!r.ok) throw new Error("could not read the image");
      return r.blob();
    });
    const file = new File([blob], `${job.id || "still"}.png`, { type: blob.type || "image/png" });
    const text = (job.user_prompt || job.prompt || "").trim();
    await useAnimateFile(file);
    if (text && !$("prompt").value.trim()) {
      $("prompt").value = text;
      autoGrowPrompt();
    }
    if (typeof closeSheet === "function") closeSheet();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function renderRgbaChips() {
    const el = $("rgbaChips");
    if (!el) return;
    el.innerHTML = "";
    [
      { id: false, label: "Opaque" },
      { id: true, label: "Transparent (RGBA)" },
    ].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (rgba === m.id ? " active" : "") + (m.id ? " rgba" : "");
      b.textContent = m.label;
      b.onclick = () => {
        rgba = m.id;
        localStorage.setItem("imagine_rgba", rgba ? "1" : "0");
        const hint = $("rgbaHint");
        if (hint) hint.classList.toggle("hidden", !rgba);
        refreshChips();
      };
      el.appendChild(b);
    });
    const hint = $("rgbaHint");
    if (hint) hint.classList.toggle("hidden", !rgba);
  }

  async function uploadEditFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    const headers = Object.assign({}, pinHeaders());
    // Do NOT set Content-Type — browser sets multipart boundary
    const res = await fetch("/api/upload", {
      method: "POST",
      headers,
      body: fd,
      credentials: "same-origin",
    });
    if (res.status === 401) {
      localStorage.removeItem("imagine_pin");
      $("gate").classList.remove("hidden");
      throw new Error("unauthorized");
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    return data;
  }


  function renderFramingChips() {
    const el = $("framingChips");
    if (!el) return;
    el.innerHTML = "";
    [
      { id: true, label: "On" },
      { id: false, label: "Off" },
    ].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (framing === m.id ? " active" : "");
      b.textContent = m.label;
      b.onclick = () => {
        framing = m.id;
        localStorage.setItem("imagine_framing", framing ? "1" : "0");
        refreshChips();
      };
      el.appendChild(b);
    });
  }

  function renderModeChips() {
    const el = $("modeChips");
    if (!el) return;
    el.innerHTML = "";
    const modes = [
      { id: false, label: "Normal" },
      { id: true, label: "Spicy +18" },
    ];
    modes.forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (spicy === m.id ? " active" : "") + (m.id ? " spicy" : "");
      b.textContent = m.label;
      b.onclick = () => {
        spicy = m.id;
        localStorage.setItem("imagine_spicy", spicy ? "1" : "0");
        refreshChips();
      };
      el.appendChild(b);
    });
  }

  
  function autoGrowPrompt() {
    const el = $("prompt");
    if (!el) return;
    el.style.height = "auto";
    const max = Math.floor(window.innerHeight * 0.6);
    const next = Math.min(Math.max(el.scrollHeight, 180), max);
    el.style.height = next + "px";
  }

  async function unlock() {
    const pin = $("pinInput").value.trim();
    $("pinErr").classList.add("hidden");
    try {
      await api("/api/auth", { method: "POST", body: JSON.stringify({ pin }) });
      localStorage.setItem("imagine_pin", pin);
      $("gate").classList.add("hidden");
      pollStatus();
      loadGallery();
    } catch {
      $("pinErr").classList.remove("hidden");
    }
  }

  function formatBitrate(bytesPerSec) {
    if (bytesPerSec == null || !Number.isFinite(bytesPerSec) || bytesPerSec < 0) return "";
    const bits = bytesPerSec * 8;
    const units = [
      [1e9, "Gb/s"],
      [1e6, "Mb/s"],
      [1e3, "kb/s"],
    ];
    for (const [scale, unit] of units) {
      if (bits >= scale) {
        const v = bits / scale;
        const digits = v >= 100 ? 0 : v >= 10 ? 1 : 2;
        const text = v.toFixed(digits).replace(/\.0+$/, "").replace(/(\.\d)0$/, "$1");
        return `${text} ${unit}`;
      }
    }
    return `${Math.round(bits)} b/s`;
  }

  function formatNetTelemetry(net) {
    if (!net || net.rx_bps == null || net.tx_bps == null) return "";
    const down = formatBitrate(net.rx_bps);
    const up = formatBitrate(net.tx_bps);
    if (!down || !up) return "";
    const names = Array.isArray(net.ifaces) ? net.ifaces.filter(Boolean) : [];
    const label = names.length === 1 ? `${names[0]} ` : "";
    return `${label}↓ ${down} ↑ ${up}`;
  }

  async function pollStatus() {
    try {
      const s = await fetch("/api/status", { headers: pinHeaders(), credentials: "same-origin" }).then((r) => r.json());
      const pill = $("statusPill");
      const ready = s.worker && s.worker.ready;
      const loading = s.worker && s.worker.loading;
      const h3 = s.h3_running;
      h3Video = !!s.h3_video;
      h3Detail = s.h3_detail || "";
      h3Known = true;
      syncTaskPanels();
      const mem = s.memory && s.memory.mem_available_gb != null ? `${s.memory.mem_available_gb}G free` : "";
      const cpu = s.cpu_percent != null ? `CPU ${Math.round(s.cpu_percent)}%` : "";
      const gpu = s.gpu_percent != null ? `GPU ${Math.round(s.gpu_percent)}%` : "";
      const gpuT = s.gpu_temp_c != null ? `${Math.round(s.gpu_temp_c)}°C` : "";
      const cpuT = s.cpu_temp_c != null ? `CPU ${Math.round(s.cpu_temp_c)}°C` : "";
      // Prefer GPU temp (most meaningful on Spark); fall back to CPU package temp
      const temp = gpuT || cpuT;
      const net = formatNetTelemetry(s.net);
      const load = [cpu, gpu, temp, mem, net].filter(Boolean).join(" · ");
      if (ready) {
        pill.textContent = load ? `ready · ${load}` : "ready";
        pill.className = "ok";
      } else if (loading) {
        pill.textContent = load ? `loading model · ${load}` : "loading model";
        pill.className = "warn";
      } else if (h3) {
        pill.textContent = load ? `H3 up (stop first) · ${load}` : "H3 up (stop first)";
        pill.className = "warn";
      } else {
        pill.textContent = load ? `worker down · ${load}` : "worker down";
        pill.className = "err";
      }
      const ifaces = s.net && Array.isArray(s.net.ifaces) ? s.net.ifaces.filter(Boolean) : [];
      pill.title = ifaces.length > 1 ? `${pill.textContent} (${ifaces.join(", ")})` : pill.textContent;
      if (s.pin_ok) $("gate").classList.add("hidden");
    } catch {
      $("statusPill").textContent = "offline";
      $("statusPill").className = "err";
      $("statusPill").removeAttribute("title");
    }
  }

  
  function formatWhen(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }


  async function clearAll() {
    const ok = confirm(
      "Clear ALL generated images from the gallery? This cannot be undone."
    );
    if (!ok) return;
    const btn = $("clearAll");
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/clear-all", { method: "DELETE" });
      // Reset result pane
      const rw = $("resultWrap");
      if (rw) rw.classList.add("hidden");
      const ri = $("resultImg");
      if (ri) ri.removeAttribute("src");
      closeLightbox();
      await loadGallery();
      const c = (res && res.cleared) || {};
      alert(
        `Cleared ${c.images || 0} images.`
      );
    } catch (e) {
      alert("Clear failed: " + (e.message || e));
    } finally {
      if (btn) btn.disabled = false;
    }
  }



  function applyNsfwBlur() {
    const g = $("gallery");
    const btn = $("nsfwBlurToggle");
    if (g) g.classList.toggle("nsfw-blur-off", !nsfwBlur);
    if (btn) {
      btn.classList.toggle("active", nsfwBlur);
      btn.textContent = nsfwBlur ? "Blur NSFW" : "Show NSFW";
      btn.title = nsfwBlur
        ? "NSFW thumbs blurred — tap to show"
        : "NSFW thumbs visible — tap to blur";
    }
  }

  function renderGalleryFilters() {
    const el = $("galleryFilters");
    if (!el) return;
    el.innerHTML = "";
    [
      { id: "all", label: "All" },
      { id: "starred", label: "Starred" },
      { id: "spicy", label: "Spicy" },
      { id: "normal", label: "Normal" },
    ].forEach((f) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (galleryFilter === f.id ? " active" : "");
      b.textContent = f.label;
      b.onclick = () => {
        galleryFilter = f.id;
        localStorage.setItem("imagine_gallery_filter", galleryFilter);
        renderGalleryFilters();
        paintGallery();
      };
      el.appendChild(b);
    });
  }

  function filteredGalleryItems() {
    return (galleryItems || []).filter((it) => {
      if (galleryFilter === "starred") return !!it.starred;
      if (galleryFilter === "spicy") return !!(it.spicy || it.nsfw);
      if (galleryFilter === "normal") return !(it.spicy || it.nsfw);
      return true;
    });
  }

  function itemById(id) {
    return (galleryItems || []).find((x) => x.id === id);
  }

  function galleryCardEl(id) {
    const g = $("gallery");
    if (!g) return null;
    for (const child of g.children) {
      if (child.dataset.id === id) return child;
    }
    return null;
  }

  function dropCard(card) {
    const img = card.querySelector("img");
    if (img && thumbObserver) thumbObserver.unobserve(img);
    card.remove();
  }

  function thumbSrcFor(it) {
    if (it.thumb) return it.thumb;
    if (!it.image || !it.id) return "";
    return `/api/thumbs/${it.id}.${it.rgba ? "png" : "jpg"}`;
  }

  let thumbObserver = null;
  function observeThumb(img) {
    if (!("IntersectionObserver" in window)) {
      if (img.dataset.src) img.src = img.dataset.src;
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const node = entry.target;
          if (node.dataset.src && node.getAttribute("src") !== node.dataset.src) {
            node.src = node.dataset.src;
            if (node.complete && node.naturalWidth) node.classList.add("ready");
          }
          thumbObserver.unobserve(node);
        }
      }, { rootMargin: "600px 0px" });
    }
    thumbObserver.observe(img);
  }

  function galleryCardClass(it) {
    return "gitem"
      + ((it.nsfw || it.spicy) ? " nsfw" : "")
      + (it.rgba ? " rgba-thumb" : "")
      + (it.kind === "video" ? " video" : "")
      + (selectedIds.has(it.id) ? " selected" : "")
      + (deletingIds.has(it.id) ? " deleting" : "");
  }

  function syncGalleryCard(card, it) {
    card.className = galleryCardClass(it);
    card.setAttribute("aria-busy", deletingIds.has(it.id) ? "true" : "false");
    const star = card.querySelector("button.star");
    if (star) {
      star.classList.toggle("on", !!it.starred);
      star.textContent = it.starred ? "★" : "☆";
    }
    let chk = card.querySelector(".sel-check");
    if (selectMode) {
      if (!chk) {
        chk = document.createElement("div");
        chk.className = "sel-check";
        card.appendChild(chk);
      }
      chk.textContent = selectedIds.has(it.id) ? "✓" : "";
    } else if (chk) {
      chk.remove();
    }
    const del = card.querySelector("button.del");
    if (del) del.classList.toggle("hidden", !!selectMode);
    let overlay = card.querySelector(".del-state");
    if (deletingIds.has(it.id)) {
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "del-state";
        overlay.innerHTML = '<span class="del-spin" aria-hidden="true"></span><span>Deleting…</span>';
        card.appendChild(overlay);
      }
    } else if (overlay) {
      overlay.remove();
    }
  }

  function createGalleryCard(it, eager) {
    const d = document.createElement("div");
    d.dataset.id = it.id;
    const img = document.createElement("img");
    img.alt = it.prompt || "";
    img.decoding = "async";
    if (it.rgba) img.classList.add("checker");
    const full = it.image || "";
    if (full) img.dataset.full = full;
    const thumb = thumbSrcFor(it);
    if (thumb) img.dataset.src = thumb;
    img.addEventListener("load", () => img.classList.add("ready"));
    img.addEventListener("error", () => {
      const fallback = img.dataset.full;
      if (fallback && img.dataset.fellback !== "1" && img.getAttribute("src") !== fallback) {
        img.dataset.fellback = "1";
        img.src = fallback;
        return;
      }
      img.classList.add("ready");
    });
    let pressTimer = null;
    let longPressed = false;
    const clearPress = () => {
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
      d.classList.remove("pressing");
    };
    img.addEventListener("pointerdown", (e) => {
      if (selectMode || deletingIds.has(d.dataset.id)) return;
      if (e.button != null && e.button !== 0) return;
      longPressed = false;
      d.classList.add("pressing");
      pressTimer = setTimeout(() => {
        longPressed = true;
        clearPress();
        const current = itemById(d.dataset.id);
        if (current) usePromptFromJob(current);
        if (navigator.vibrate) try { navigator.vibrate(12); } catch (_) {}
      }, 480);
    });
    img.addEventListener("pointerup", clearPress);
    img.addEventListener("pointerleave", clearPress);
    img.addEventListener("pointercancel", clearPress);
    img.onclick = (e) => {
      const current = itemById(d.dataset.id);
      if (!current || deletingIds.has(current.id)) return;
      if (selectMode) {
        e.preventDefault();
        e.stopPropagation();
        if (selectedIds.has(current.id)) selectedIds.delete(current.id);
        else selectedIds.add(current.id);
        syncGalleryCard(d, current);
        syncSelectUi();
        return;
      }
      if (longPressed) { e.preventDefault(); e.stopPropagation(); longPressed = false; return; }
      openSheet(current);
    };
    const star = document.createElement("button");
    star.type = "button";
    star.className = "star";
    star.title = "Star";
    star.onclick = async (e) => {
      e.stopPropagation();
      const current = itemById(d.dataset.id);
      if (!current || deletingIds.has(current.id)) return;
      try {
        const res = await api(`/api/gallery/${current.id}/star`, { method: "POST", body: "{}" });
        current.starred = !!res.starred;
        syncGalleryCard(d, current);
        if (sheetJob && sheetJob.id === current.id && $("sheetStar")) {
          sheetJob.starred = current.starred;
          $("sheetStar").textContent = current.starred ? "★ Starred" : "★ Star";
        }
      } catch (err) {
        alert("Star failed: " + (err.message || err));
      }
    };
    const del = document.createElement("button");
    del.className = "del";
    del.type = "button";
    del.textContent = "×";
    del.title = "Delete";
    del.setAttribute("aria-label", "Delete");
    del.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      clearPress();
      deleteGalleryIds([d.dataset.id]);
    };
    d.appendChild(img);
    d.appendChild(star);
    if (it.kind === "video") {
      const badge = document.createElement("div");
      badge.className = "play-badge";
      badge.textContent = "▶";
      d.appendChild(badge);
    }
    d.appendChild(del);
    syncGalleryCard(d, it);
    if (thumb) {
      if (eager) {
        img.src = thumb;
        if (img.complete && img.naturalWidth) img.classList.add("ready");
      } else {
        observeThumb(img);
      }
    }
    return d;
  }

  async function deleteGalleryIds(ids) {
    const unique = [];
    for (const id of ids) {
      if (id && !deletingIds.has(id) && !unique.includes(id)) unique.push(id);
    }
    if (!unique.length) return;
    const batch = unique.length > 1;
    const delBtn = $("deleteSelectedBtn");
    if (batch && delBtn) {
      delBtn.disabled = true;
      delBtn.textContent = "Deleting…";
    }
    unique.forEach((id) => {
      deletingIds.add(id);
      const card = galleryCardEl(id);
      const it = itemById(id);
      if (card && it) syncGalleryCard(card, it);
    });
    try {
      if (unique.length === 1) {
        await api(`/api/gallery/${encodeURIComponent(unique[0])}`, { method: "DELETE" });
      } else {
        await api("/api/gallery/delete", { method: "POST", body: JSON.stringify({ ids: unique }) });
      }
      const gone = new Set(unique);
      unique.forEach((id) => removedIds.add(id));
      galleryItems = galleryItems.filter((it) => !gone.has(it.id));
      unique.forEach((id) => {
        deletingIds.delete(id);
        selectedIds.delete(id);
        const card = galleryCardEl(id);
        if (card) card.classList.add("deleting-out");
      });
      if (sheetJob && gone.has(sheetJob.id)) closeSheet();
      window.setTimeout(() => {
        unique.forEach((id) => {
          const card = galleryCardEl(id);
          if (card) dropCard(card);
        });
        const empty = $("galleryEmpty");
        if (empty) empty.classList.toggle("hidden", filteredGalleryItems().length > 0);
        if (batch) selectMode = false;
        if (delBtn) delBtn.disabled = false;
        syncSelectUi();
      }, 180);
    } catch (err) {
      unique.forEach((id) => {
        deletingIds.delete(id);
        const card = galleryCardEl(id);
        const it = itemById(id);
        if (card && it) syncGalleryCard(card, it);
      });
      if (delBtn) delBtn.disabled = false;
      syncSelectUi();
      alert("Delete failed: " + (err.message || err));
    }
  }

  function paintGallery() {
    const g = $("gallery");
    if (!g) return;
    g.classList.toggle("select-mode", selectMode);
    const items = filteredGalleryItems();
    const empty = $("galleryEmpty");
    if (empty) empty.classList.toggle("hidden", items.length > 0);
    const want = new Set(items.map((it) => it.id));
    for (const child of Array.from(g.children)) {
      if (!want.has(child.dataset.id)) dropCard(child);
    }
    const byId = new Map();
    for (const child of g.children) byId.set(child.dataset.id, child);
    items.forEach((it, index) => {
      let card = byId.get(it.id);
      if (!card) {
        card = createGalleryCard(it, index < 12);
        byId.set(it.id, card);
      } else {
        syncGalleryCard(card, it);
      }
      const before = g.children[index] || null;
      if (before !== card) g.insertBefore(card, before);
    });
    applyNsfwBlur();
  }

  function syncSelectUi() {
    const selBtn = $("selectModeBtn");
    const delBtn = $("deleteSelectedBtn");
    if (selBtn) selBtn.textContent = selectMode ? "Done" : "Select";
    if (delBtn) {
      delBtn.classList.toggle("hidden", !selectMode || selectedIds.size === 0);
      delBtn.textContent = selectedIds.size ? `Delete (${selectedIds.size})` : "Delete";
    }
  }

  async function loadGallery() {
    try {
      const data = await api("/api/gallery");
      const incoming = data.items || [];
      const serverIds = new Set(incoming.map((it) => it.id));
      for (const id of [...removedIds]) {
        if (!serverIds.has(id)) removedIds.delete(id);
      }
      galleryItems = incoming.filter((it) => !removedIds.has(it.id) && !deletingIds.has(it.id));
      renderGalleryFilters();
      paintGallery();
      syncSelectUi();
    } catch (_) {}
  }



  function openSheet(job) {
    if (!job) return;
    sheetJob = job;
    lastJob = job;
    const url = job.image || `/api/images/${job.id}.png`;
    const isVideo = job.kind === "video" && job.video;
    const sheetVid = $("sheetVideo");
    if (isVideo && sheetVid) {
      $("sheetImg").classList.add("hidden");
      sheetVid.classList.remove("hidden");
      sheetVid.poster = job.image || "";
      sheetVid.src = job.video;
      $("sheetDownload").href = job.video;
      $("sheetDownload").download = `imagine-${job.id}.mp4`;
    } else {
      if (sheetVid) {
        sheetVid.pause();
        sheetVid.classList.add("hidden");
        sheetVid.removeAttribute("src");
      }
      $("sheetImg").classList.remove("hidden");
      $("sheetImg").src = url + "?t=" + Date.now();
      $("sheetDownload").href = url;
      $("sheetDownload").download = `imagine-${job.id}.png`;
    }
    const bits = [];
    if (job.model) bits.push(job.model);
    if (job.duration) bits.push(`${job.duration}s`);
    if (job.resolution) bits.push(job.resolution);
    if (job.width && job.height) bits.push(`${job.width}×${job.height}`);
    if (job.steps != null) bits.push(`${job.steps} steps`);
    if (job.seed != null) bits.push(`seed ${job.seed}`);
    if (job.elapsed_s != null) bits.push(`${job.elapsed_s}s`);
    $("sheetMeta").textContent = bits.join(" · ");
    const text = (job.user_prompt || job.prompt || "").trim();
    const sp = $("sheetPrompt");
    const hint = $("sheetPromptHint");
    if (text) {
      sp.textContent = text;
      sp.classList.remove("hidden");
      if (hint) hint.classList.remove("hidden");
    } else {
      sp.textContent = "";
      sp.classList.add("hidden");
      if (hint) hint.classList.add("hidden");
    }
    if ($("sheetStar")) $("sheetStar").textContent = job.starred ? "★ Starred" : "★ Star";
    $("sheetBackdrop").classList.remove("hidden");
    $("sheet").classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }

  function closeSheet() {
    $("sheet").classList.add("hidden");
    $("sheetBackdrop").classList.add("hidden");
    $("sheetImg").src = "";
    const sheetVid = $("sheetVideo");
    if (sheetVid) {
      sheetVid.pause();
      sheetVid.removeAttribute("src");
      sheetVid.load();
    }
    sheetJob = null;
    document.body.style.overflow = "";
  }

  function openLightbox(src) {
    if (!src) return;
    $("lightboxImg").src = src;
    $("lightbox").classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }
  function closeLightbox() {
    $("lightbox").classList.add("hidden");
    $("lightboxImg").src = "";
    document.body.style.overflow = "";
  }


  function clearResult() {
    lastJob = null;
    const rw = $("resultWrap");
    if (rw) rw.classList.add("hidden");
    const ri = $("resultImg");
    if (ri) {
      ri.classList.remove("hidden");
      ri.removeAttribute("src");
      ri.src = "";
    }
    const rv = $("resultVideo");
    if (rv) {
      rv.pause();
      rv.classList.add("hidden");
      rv.removeAttribute("src");
      rv.load();
    }
    const rp = $("resultPrompt");
    if (rp) {
      rp.textContent = "";
      rp.classList.add("hidden");
    }
    const hint = $("resultPromptHint");
    if (hint) hint.classList.add("hidden");
    if (typeof closeSheet === "function") closeSheet();
    if (typeof closeLightbox === "function") closeLightbox();
  }

  function showResult(job) {
    lastJob = job;
    $("resultWrap").classList.remove("hidden");
    const isVideo = job.kind === "video" && job.video;
    const url = job.image || `/api/images/${job.id}.png`;
    const rv = $("resultVideo");
    if (isVideo && rv) {
      $("resultImg").classList.add("hidden");
      rv.classList.remove("hidden");
      rv.poster = job.image || "";
      rv.src = job.video;
      $("downloadBtn").href = job.video;
      $("downloadBtn").download = `imagine-${job.id}.mp4`;
      if (job.image_id) animateImageId = job.image_id;
    } else {
      if (rv) {
        rv.pause();
        rv.classList.add("hidden");
        rv.removeAttribute("src");
      }
      $("resultImg").classList.remove("hidden");
      $("resultImg").src = url + "?t=" + Date.now();
      $("downloadBtn").href = url;
      $("downloadBtn").download = `imagine-${job.id}.png`;
    }
    const animBtn = $("animateResultBtn");
    if (animBtn) animBtn.classList.toggle("hidden", !!isVideo);
    if (job.seed != null) $("seed").value = job.seed;
    const text = (job.user_prompt || job.prompt || "").trim();
    const rp = $("resultPrompt");
    const hint = $("resultPromptHint");
    if (rp) {
      if (text) {
        rp.textContent = text;
        rp.classList.remove("hidden");
        if (hint) hint.classList.remove("hidden");
      } else {
        rp.textContent = "";
        rp.classList.add("hidden");
        if (hint) hint.classList.add("hidden");
      }
    }
  }


  function usePromptFromJob(job) {
    if (!job) return;
    const text = (job.user_prompt || job.prompt || "").trim();
    if (!text) return;
    $("prompt").value = text;
    if (job.negative_prompt != null) $("negative").value = job.negative_prompt || "";
    if (job.seed != null && $("seed")) $("seed").value = job.seed;
    autoGrowPrompt();
    $("prompt").focus();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function useResultPrompt() {
    usePromptFromJob(lastJob || sheetJob);
  }


  function beginProgress() {
    $("generateBtn").disabled = true;
    cancelRequested = false;
    activeJobId = null;
    const cancelBtn = $("cancelBtn");
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    clearResult();
    $("progressWrap").classList.remove("hidden");
    const bar = $("progressBar");
    bar.classList.remove("indeterminate");
    bar.style.width = "0%";
    const t0 = Date.now();
    timer = setInterval(() => {
      $("elapsed").textContent = Math.round((Date.now() - t0) / 1000) + "s";
    }, 250);
    $("progressLabel").textContent = "Starting… 0%";
    return bar;
  }

  function endProgress() {
    clearInterval(timer);
    $("generateBtn").disabled = false;
    activeJobId = null;
    cancelRequested = false;
    syncTaskPanels();
    const cancelBtn = $("cancelBtn");
    if (cancelBtn) cancelBtn.classList.add("hidden");
    setTimeout(() => $("progressWrap").classList.add("hidden"), 2500);
  }

  async function animate() {
    if (h3Known && !h3Video) {
      alert(h3Detail || "Local H3 is not ready.");
      return;
    }
    if (!animateImageId) {
      alert("Drop a screenshot or choose an image first");
      const drop = $("animateDrop");
      if (drop) drop.focus();
      return;
    }
    const prompt = $("prompt").value.trim();
    const bar = beginProgress();
    bar.classList.add("indeterminate");
    $("progressLabel").textContent = "H3 · starting";
    try {
      const started = await api("/api/animate", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          image_id: animateImageId,
          duration,
          resolution,
        }),
      });
      const jobId = started.id;
      activeJobId = jobId;
      let job = started;
      while (job.status === "queued" || job.status === "running") {
        if (cancelRequested) {
          try { await api(`/api/jobs/${jobId}/cancel`, { method: "POST", body: "{}" }); } catch (_) {}
          throw new Error("Cancelled");
        }
        if (job.status === "cancelled") throw new Error("Cancelled");
        await new Promise((r) => setTimeout(r, 1000));
        job = await api("/api/jobs/" + jobId);
        const remote = (job.provider_status || "").trim();
        const ahead = Number(job.queue_ahead);
        let msg = remote && remote !== "queued" ? `H3 · ${remote}` : "H3 · starting";
        if (Number.isFinite(ahead) && ahead > 0) msg += ` · #${ahead + 1} ahead`;
        $("progressLabel").textContent = msg;
      }
      if (job.status === "error") {
        throw new Error(typeof job.error === "string" ? job.error : JSON.stringify(job.error || "failed"));
      }
      if (job.status === "cancelled") throw new Error("Cancelled");
      bar.classList.remove("indeterminate");
      bar.style.width = "100%";
      showResult(job);
      loadGallery();
      $("progressLabel").textContent = `Done · ${job.elapsed_s || "?"}s`;
    } catch (e) {
      bar.classList.add("indeterminate");
      $("progressLabel").textContent = "Failed: " + (e.message || e);
    } finally {
      endProgress();
    }
  }

  async function generate() {
    if (task === "animate") return animate();
    const prompt = $("prompt").value.trim();
    if (!prompt) {
      $("prompt").focus();
      return;
    }
    if (task === "edit" && !editImageId) {
      alert("Pick a source image for Edit mode");
      return;
    }
    const seedRaw = $("seed").value;
    const body = {
      prompt,
      negative_prompt: $("negative").value.trim(),
      width: aspect.w,
      height: aspect.h,
      steps: Number($("steps").value) || 8,
      seed: seedRaw === "" ? null : Number(seedRaw),
      style: style.id || null,
      spicy: !!spicy,
      rgba: !!rgba,
      framing: !!framing,
      image_id: task === "edit" ? editImageId : null,
      strength: task === "edit" ? Number(strength) || 0.65 : undefined,
    };
    const bar = beginProgress();
    try {
      const started = await api("/api/generate", { method: "POST", body: JSON.stringify(body) });
      const jobId = started.id;
      activeJobId = jobId;
      let job = started;
      while (job.status === "queued" || job.status === "running") {
        if (cancelRequested) {
          try { await api(`/api/jobs/${jobId}/cancel`, { method: "POST", body: "{}" }); } catch (_) {}
          throw new Error("Cancelled");
        }
        if (job.status === "cancelled") throw new Error("Cancelled");
        await new Promise((r) => setTimeout(r, 400));
        job = await api("/api/jobs/" + jobId);
        const pct = Math.max(0, Math.min(100, Number(job.pct) || 0));
        const step = job.step != null ? job.step : "?";
        const steps = job.steps != null ? job.steps : body.steps;
        if (job.status === "queued" || (pct === 0 && Number(step) === 0)) {
          bar.style.width = "0%";
          bar.classList.add("indeterminate");
          const ahead = Number(job.queue_ahead);
          let waitMsg = "Waiting · next up";
          if (Number.isFinite(ahead) && ahead > 0) {
            waitMsg = ahead === 1
              ? "Waiting · #2 in queue"
              : `Waiting · #${ahead + 1} in queue`;
          }
          if (job.eta_s != null && Number(job.eta_s) > 0) {
            waitMsg += ` · ~${Math.round(Number(job.eta_s))}s`;
          }
          $("progressLabel").textContent = waitMsg;
        } else {
          bar.classList.remove("indeterminate");
          bar.style.width = pct + "%";
          $("progressLabel").textContent = `${pct}% · step ${step}/${steps}`;
        }
      }
      if (job.status === "error") {
        throw new Error(typeof job.error === "string" ? job.error : JSON.stringify(job.error || "failed"));
      }
      bar.style.width = "100%";
      showResult(job);
      loadGallery();
      $("progressLabel").textContent = `Done · 100% · ${job.elapsed_s || "?"}s`;
    } catch (e) {
      bar.classList.add("indeterminate");
      $("progressLabel").textContent = "Failed: " + (e.message || e);
    } finally {
      endProgress();
    }
  }

  function shuffleSeed() {
    $("seed").value = Math.floor(Math.random() * 2 ** 31);
  }

  $("pinBtn").onclick = unlock;
  $("pinInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") unlock();
  });
  $("resultImg").onclick = () => openLightbox($("resultImg").src);
  if ($("resultPrompt")) $("resultPrompt").onclick = useResultPrompt;
  $("lightbox").onclick = (e) => {
    if (e.target.id === "lightbox" || e.target.id === "lightboxClose") closeLightbox();
  };
  $("lightboxClose").onclick = (e) => { e.stopPropagation(); closeLightbox(); };

  if ($("sheetClose")) $("sheetClose").onclick = closeSheet;
  if ($("sheetBackdrop")) $("sheetBackdrop").onclick = closeSheet;
  if ($("sheetPrompt")) $("sheetPrompt").onclick = () => usePromptFromJob(sheetJob);
  if ($("sheetReuse")) $("sheetReuse").onclick = () => { usePromptFromJob(sheetJob); closeSheet(); };
  if ($("sheetImg")) $("sheetImg").onclick = () => {
    if ($("sheetImg").src) openLightbox($("sheetImg").src);
  };
  if ($("sheetDelete")) $("sheetDelete").onclick = () => {
    if (!sheetJob || deletingIds.has(sheetJob.id)) return;
    if (!confirm("Delete this image?")) return;
    const id = sheetJob.id;
    closeSheet();
    deleteGalleryIds([id]);
  };

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeSheet(); closeLightbox(); } });
  $("generateBtn").onclick = generate;
  if ($("clearAll")) $("clearAll").onclick = clearAll;
  $("shuffleSeed").onclick = shuffleSeed;
  
  applyNsfwBlur();
  if ($("nsfwBlurToggle")) {
    $("nsfwBlurToggle").onclick = () => {
      nsfwBlur = !nsfwBlur;
      localStorage.setItem("imagine_nsfw_blur", nsfwBlur ? "1" : "0");
      applyNsfwBlur();
    };
  }
  const adv = $("advancedPanel");
  if (adv) {
    if (localStorage.getItem("imagine_advanced") === "1") adv.open = true;
    adv.addEventListener("toggle", () => {
      localStorage.setItem("imagine_advanced", adv.open ? "1" : "0");
    });
  }

  $("refreshGallery").onclick = loadGallery;
  $("regenBtn").onclick = () => {
    if ((lastJob && lastJob.kind === "video") || task === "animate") {
      animate();
      return;
    }
    shuffleSeed();
    generate();
  };

  refreshChips();
  if (localStorage.getItem("imagine_pin")) {
    // probe
    fetch("/api/gallery", { headers: pinHeaders(), credentials: "same-origin" }).then((r) => {
      if (r.ok) {
        $("gate").classList.add("hidden");
        loadGallery();
        }
    });
  }
  pollStatus();
  
  const promptEl = $("prompt");
  if (promptEl) {
    promptEl.addEventListener("input", autoGrowPrompt);
    promptEl.addEventListener("focus", autoGrowPrompt);
    window.addEventListener("resize", autoGrowPrompt);
    autoGrowPrompt();
  }

  
  if ($("editFile")) {
    $("editFile").onchange = async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        $("progressLabel") && ($("progressLabel").textContent = "Uploading…");
        const up = await uploadEditFile(file);
        editImageId = up.id;
        $("editPreview").src = up.url + "?t=" + Date.now();
        $("editPreviewWrap").classList.remove("hidden");
        // Snap aspect to upload if close
        if (up.width && up.height) {
          // keep user aspect; worker will resize
        }
      } catch (err) {
        alert("Upload failed: " + (err.message || err));
        editImageId = null;
      }
    };
  }
  if ($("clearEdit")) {
    $("clearEdit").onclick = () => {
      editImageId = null;
      if ($("editFile")) $("editFile").value = "";
      $("editPreviewWrap").classList.add("hidden");
      $("editPreview").src = "";
    };
  }

  if ($("animateBrowse") && $("animateFile")) {
    $("animateBrowse").onclick = () => $("animateFile").click();
    $("animateFile").onchange = async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      await useAnimateFile(file);
      e.target.value = "";
    };
  }
  if ($("clearAnimate")) {
    $("clearAnimate").onclick = () => {
      animateImageId = null;
      if ($("animateFile")) $("animateFile").value = "";
      $("animatePreviewWrap").classList.add("hidden");
      $("animatePreview").src = "";
    };
  }
  const animateDrop = $("animateDrop");
  if (animateDrop) {
    animateDrop.addEventListener("dragover", (e) => {
      e.preventDefault();
      animateDrop.classList.add("dragover");
    });
    animateDrop.addEventListener("dragleave", () => animateDrop.classList.remove("dragover"));
    animateDrop.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      dragDepth = 0;
      animateDrop.classList.remove("dragover");
      const overlay = $("dropOverlay");
      if (overlay) overlay.classList.add("hidden");
      const file = firstImageFile(e.dataTransfer && e.dataTransfer.files);
      if (file) await useAnimateFile(file);
    });
  }
  if ($("animateResultBtn")) {
    $("animateResultBtn").onclick = () => {
      useJobAsAnimateSource(lastJob).catch((e) => alert("Could not use this still: " + (e.message || e)));
    };
  }
  if ($("sheetAnimate")) {
    $("sheetAnimate").onclick = () => {
      useJobAsAnimateSource(sheetJob).catch((e) => alert("Could not use this still: " + (e.message || e)));
    };
  }

  function firstImageFile(fileList) {
    if (!fileList) return null;
    for (const file of fileList) {
      if (file && file.type && file.type.startsWith("image/")) return file;
    }
    return null;
  }

  let dragDepth = 0;
  function dragHasFiles(e) {
    const types = e.dataTransfer && e.dataTransfer.types;
    if (!types) return false;
    return Array.from(types).indexOf("Files") !== -1;
  }
  document.addEventListener("dragenter", (e) => {
    if ($("gate") && !$("gate").classList.contains("hidden")) return;
    if (!dragHasFiles(e)) return;
    dragDepth += 1;
    const overlay = $("dropOverlay");
    if (overlay) overlay.classList.remove("hidden");
  });
  document.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
  });
  document.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) {
      const overlay = $("dropOverlay");
      if (overlay) overlay.classList.add("hidden");
    }
  });
  document.addEventListener("dragend", () => {
    dragDepth = 0;
    const overlay = $("dropOverlay");
    if (overlay) overlay.classList.add("hidden");
  });
  document.addEventListener("drop", async (e) => {
    dragDepth = 0;
    const overlay = $("dropOverlay");
    if (overlay) overlay.classList.add("hidden");
    if ($("gate") && !$("gate").classList.contains("hidden")) return;
    const file = firstImageFile(e.dataTransfer && e.dataTransfer.files);
    if (!file) return;
    e.preventDefault();
    await useAnimateFile(file);
  });
  document.addEventListener("paste", async (e) => {
    if ($("gate") && !$("gate").classList.contains("hidden")) return;
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (!file) return;
        e.preventDefault();
        await useAnimateFile(file);
        return;
      }
    }
  });


  // Strength slider
  if ($("strength")) {
    $("strength").value = String(strength);
    if ($("strengthVal")) $("strengthVal").textContent = Number(strength).toFixed(2);
    $("strength").oninput = () => {
      strength = Number($("strength").value);
      if ($("strengthVal")) $("strengthVal").textContent = strength.toFixed(2);
      localStorage.setItem("imagine_strength", String(strength));
    };
  }
  if ($("cancelBtn")) {
    $("cancelBtn").onclick = () => {
      cancelRequested = true;
      $("progressLabel").textContent = "Cancelling…";
      if (activeJobId) {
        api(`/api/jobs/${activeJobId}/cancel`, { method: "POST", body: "{}" }).catch(() => {});
      }
    };
  }
  if ($("selectModeBtn")) {
    $("selectModeBtn").onclick = () => {
      selectMode = !selectMode;
      if (!selectMode) selectedIds.clear();
      syncSelectUi();
      paintGallery();
    };
  }
  if ($("deleteSelectedBtn")) {
    $("deleteSelectedBtn").onclick = () => {
      const ids = Array.from(selectedIds);
      if (!ids.length) return;
      if (!confirm(`Delete ${ids.length} image${ids.length === 1 ? "" : "s"}?`)) return;
      deleteGalleryIds(ids);
    };
  }
  if ($("sheetStar")) {
    $("sheetStar").onclick = async () => {
      if (!sheetJob) return;
      try {
        const res = await api(`/api/gallery/${sheetJob.id}/star`, { method: "POST", body: "{}" });
        sheetJob.starred = !!res.starred;
        $("sheetStar").textContent = sheetJob.starred ? "★ Starred" : "★ Star";
        const gIt = galleryItems.find((x) => x.id === sheetJob.id);
        if (gIt) {
          gIt.starred = sheetJob.starred;
          const card = galleryCardEl(gIt.id);
          if (card) syncGalleryCard(card, gIt);
        }
      } catch (e) {
        alert("Star failed: " + (e.message || e));
      }
    };
  }
  if ($("sheetUseSource")) {
    $("sheetUseSource").onclick = async () => {
      if (!sheetJob) return;
      try {
        // Re-upload gallery image as edit source via fetch→blob→upload
        const url = sheetJob.image || `/api/images/${sheetJob.id}.png`;
        const blob = await fetch(url, { headers: pinHeaders(), credentials: "same-origin" }).then((r) => r.blob());
        const file = new File([blob], `${sheetJob.id}.png`, { type: "image/png" });
        const up = await uploadEditFile(file);
        editImageId = up.id;
        task = "edit";
        $("editPreview").src = (up.url || url) + "?t=" + Date.now();
        $("editPreviewWrap").classList.remove("hidden");
        refreshChips();
        closeSheet();
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (e) {
        alert("Could not use as source: " + (e.message || e));
      }
    };
  }
  renderGalleryFilters();

  setInterval(pollStatus, 5000);
})();
