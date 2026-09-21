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
  let task = "create"; // create | edit
  let editImageId = null;
  let strength = Number(localStorage.getItem('imagine_strength') || 0.65);
  let galleryFilter = localStorage.getItem('imagine_gallery_filter') || 'all';
  let selectMode = false;
  let selectedIds = new Set();
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
      const msg = typeof body === "object" ? JSON.stringify(body) : body;
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
  }


  function renderTaskChips() {
    const el = $("taskChips");
    if (!el) return;
    el.innerHTML = "";
    [
      { id: "create", label: "Create" },
      { id: "edit", label: "Edit" },
    ].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (task === m.id ? " active" : "");
      b.textContent = m.label;
      b.onclick = () => {
        task = m.id;
        const panel = $("editPanel");
        if (panel) panel.classList.toggle("hidden", task !== "edit");
        if (task === "edit") {
          $("prompt").placeholder = "Describe the edit — e.g. change background to sunset beach…";
        } else {
          $("prompt").placeholder = "A neon koi swimming through misty bamboo at dusk…";
        }
        refreshChips();
      };
      el.appendChild(b);
    });
    const panel = $("editPanel");
    if (panel) panel.classList.toggle("hidden", task !== "edit");
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

  async function pollStatus() {
    try {
      const s = await fetch("/api/status", { headers: pinHeaders(), credentials: "same-origin" }).then((r) => r.json());
      const pill = $("statusPill");
      const ready = s.worker && s.worker.ready;
      const loading = s.worker && s.worker.loading;
      const h3 = s.h3_running;
      const mem = s.memory && s.memory.mem_available_gb != null ? `${s.memory.mem_available_gb}G free` : "";
      const cpu = s.cpu_percent != null ? `CPU ${Math.round(s.cpu_percent)}%` : "";
      const gpu = s.gpu_percent != null ? `GPU ${Math.round(s.gpu_percent)}%` : "";
      const gpuT = s.gpu_temp_c != null ? `${Math.round(s.gpu_temp_c)}°C` : "";
      const cpuT = s.cpu_temp_c != null ? `CPU ${Math.round(s.cpu_temp_c)}°C` : "";
      // Prefer GPU temp (most meaningful on Spark); fall back to CPU package temp
      const temp = gpuT || cpuT;
      const load = [cpu, gpu, temp, mem].filter(Boolean).join(" · ");
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
      if (s.pin_ok) $("gate").classList.add("hidden");
    } catch {
      $("statusPill").textContent = "offline";
      $("statusPill").className = "err";
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

  function paintGallery() {
    const g = $("gallery");
    if (!g) return;
    g.innerHTML = "";
    g.classList.toggle("select-mode", selectMode);
    const items = filteredGalleryItems();
    const empty = $("galleryEmpty");
    if (empty) empty.classList.toggle("hidden", items.length > 0);
    items.forEach((it) => {
      const d = document.createElement("div");
      d.className = "gitem" + (it.nsfw || it.spicy ? " nsfw" : "") + (it.rgba ? " rgba-thumb" : "") + (selectedIds.has(it.id) ? " selected" : "");
      d.dataset.id = it.id;
      const img = document.createElement("img");
      img.src = it.image;
      img.alt = it.prompt || "";
      img.loading = "lazy";
      if (it.rgba) img.classList.add("checker");
      let pressTimer = null;
      let longPressed = false;
      const clearPress = () => {
        if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
        d.classList.remove("pressing");
      };
      img.addEventListener("pointerdown", (e) => {
        if (selectMode) return;
        if (e.button != null && e.button !== 0) return;
        longPressed = false;
        d.classList.add("pressing");
        pressTimer = setTimeout(() => {
          longPressed = true;
          clearPress();
          usePromptFromJob(it);
          if (navigator.vibrate) try { navigator.vibrate(12); } catch (_) {}
        }, 480);
      });
      img.addEventListener("pointerup", clearPress);
      img.addEventListener("pointerleave", clearPress);
      img.addEventListener("pointercancel", clearPress);
      img.onclick = (e) => {
        if (selectMode) {
          e.preventDefault();
          e.stopPropagation();
          if (selectedIds.has(it.id)) selectedIds.delete(it.id);
          else selectedIds.add(it.id);
          paintGallery();
          syncSelectUi();
          return;
        }
        if (longPressed) { e.preventDefault(); e.stopPropagation(); longPressed = false; return; }
        openSheet(it);
      };
      const star = document.createElement("button");
      star.type = "button";
      star.className = "star" + (it.starred ? " on" : "");
      star.textContent = it.starred ? "★" : "☆";
      star.title = "Star";
      star.onclick = async (e) => {
        e.stopPropagation();
        try {
          const res = await api(`/api/gallery/${it.id}/star`, { method: "POST", body: "{}" });
          it.starred = !!res.starred;
          paintGallery();
        } catch (err) {
          alert("Star failed: " + (err.message || err));
        }
      };
      const del = document.createElement("button");
      del.className = "del";
      del.type = "button";
      del.textContent = "×";
      del.onclick = async (e) => {
        e.stopPropagation();
        clearPress();
        await api(`/api/gallery/${it.id}`, { method: "DELETE" });
        loadGallery();
      };
      if (selectMode) {
        const chk = document.createElement("div");
        chk.className = "sel-check";
        chk.textContent = selectedIds.has(it.id) ? "✓" : "";
        d.appendChild(chk);
      }
      d.appendChild(img);
      d.appendChild(star);
      if (!selectMode) d.appendChild(del);
      g.appendChild(d);
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
      galleryItems = data.items || [];
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
    $("sheetImg").src = url + "?t=" + Date.now();
    $("sheetDownload").href = url;
    $("sheetDownload").download = `imagine-${job.id}.png`;
    const bits = [];
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
      ri.removeAttribute("src");
      ri.src = "";
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
    const url = job.image || `/api/images/${job.id}.png`;
    $("resultImg").src = url + "?t=" + Date.now();
    $("downloadBtn").href = url;
    $("downloadBtn").download = `imagine-${job.id}.png`;
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


  async function generate() {
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
      steps: Number($("steps").value) || 28,
      seed: seedRaw === "" ? null : Number(seedRaw),
      style: style.id || null,
      spicy: !!spicy,
      rgba: !!rgba,
      framing: !!framing,
      image_id: task === "edit" ? editImageId : null,
      strength: task === "edit" ? Number(strength) || 0.65 : undefined,
    };
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
      clearInterval(timer);
      $("generateBtn").disabled = false;
      activeJobId = null;
      cancelRequested = false;
      const cancelBtn = $("cancelBtn");
      if (cancelBtn) cancelBtn.classList.add("hidden");
      setTimeout(() => $("progressWrap").classList.add("hidden"), 2500);
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
  if ($("sheetDelete")) $("sheetDelete").onclick = async () => {
    if (!sheetJob) return;
    if (!confirm("Delete this image?")) return;
    const id = sheetJob.id;
    closeSheet();
    await api(`/api/gallery/${id}`, { method: "DELETE" });
    loadGallery();
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
    $("deleteSelectedBtn").onclick = async () => {
      const ids = Array.from(selectedIds);
      if (!ids.length) return;
      if (!confirm(`Delete ${ids.length} image(s)?`)) return;
      try {
        await api("/api/gallery/delete", { method: "POST", body: JSON.stringify({ ids }) });
        selectedIds.clear();
        selectMode = false;
        await loadGallery();
      } catch (e) {
        alert("Delete failed: " + (e.message || e));
      }
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
        if (gIt) gIt.starred = sheetJob.starred;
        paintGallery();
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
