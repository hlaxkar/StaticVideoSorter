/* ═══════════ StaticVideoSorter Frontend ═══════════ */

(function () {
    "use strict";

    const IS_TOUCH = ("ontouchstart" in window) || navigator.maxTouchPoints > 0;
    if (IS_TOUCH) document.body.classList.add("is-touch");

    // ─── Utilities ───

    function $(sel) { return document.querySelector(sel); }
    function $$(sel) { return document.querySelectorAll(sel); }

    function show(el) { el.classList.remove("hidden"); }
    function hide(el) { el.classList.add("hidden"); }

    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    function toast(msg, isError) {
        const container = $("#toast-container");
        const t = document.createElement("div");
        t.className = "toast" + (isError ? " error" : "");
        t.textContent = msg;
        container.appendChild(t);
        setTimeout(() => t.remove(), 4000);
    }

    async function api(url, opts) {
        try {
            const resp = await fetch(url, opts);
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(data.detail || `HTTP ${resp.status}`);
            }
            return resp.json();
        } catch (e) {
            if (e.message === "Failed to fetch") toast("Server not responding", true);
            throw e;
        }
    }

    // ─── Tabs ───

    const tabs = $$(".tab");
    const tabContents = $$(".tab-content");

    function switchTab(name) {
        tabs.forEach(t => t.classList.toggle("active", t.dataset.tab === name));
        tabContents.forEach(tc => tc.classList.toggle("active", tc.id === "tab-" + name));
    }

    tabs.forEach(t => t.addEventListener("click", () => switchTab(t.dataset.tab)));

    // ═══════════ FOLDER PICKER MODAL ═══════════

    const fpModal       = $("#folder-picker-modal");
    const fpList        = $("#fp-list");
    const fpBreadcrumb  = $("#fp-breadcrumb");
    const fpCurrentPath = $("#fp-current-path");
    const fpSelectBtn   = $("#fp-select-btn");
    const fpCancelBtn   = $("#fp-cancel-btn");

    let fpTargetId   = null;
    let fpCurrentDir = "";
    let fpOnSelect   = null;

    // Map hidden-input id → display span id
    const DISPLAY_MAP = {
        "detect-folder":  "detect-folder-display",
        "review-folder":  "review-folder-display",
        "extract-folder": "extract-folder-display",
        "extract-output": "extract-output-display",
    };

    // Per-target callbacks called after selection (register below in each tab section)
    const PICKER_CALLBACKS = {};

    function setFolderValue(inputId, path) {
        const input   = document.getElementById(inputId);
        const display = document.getElementById(DISPLAY_MAP[inputId]);
        if (!input) return;
        input.value = path;
        if (display) {
            if (path) {
                display.textContent = path;
                display.classList.remove("placeholder");
            } else {
                display.textContent = display.dataset.placeholder || "No folder selected";
                display.classList.add("placeholder");
            }
        }
    }

    function getFolderValue(inputId) {
        const input = document.getElementById(inputId);
        return input ? input.value.trim() : "";
    }

    async function fpNavigate(path) {
        fpCurrentDir = path;
        fpCurrentPath.textContent = path;
        fpList.innerHTML = '<div class="fp-loading">Loading...</div>';
        renderBreadcrumb(path);

        try {
            const data = await api("/api/browse?path=" + encodeURIComponent(path));
            fpCurrentDir = data.current || path;
            fpCurrentPath.textContent = fpCurrentDir;
            renderBreadcrumb(fpCurrentDir);
            renderDirList(data.dirs || []);
        } catch (e) {
            fpList.innerHTML = '<div class="fp-empty">Could not read directory.</div>';
        }
    }

    function renderBreadcrumb(path) {
        fpBreadcrumb.innerHTML = "";
        const parts    = path.split("/").filter((p, i) => i === 0 || p !== "");
        const segments = [];
        let accumulated = "";

        for (let i = 0; i < parts.length; i++) {
            if (i === 0 && parts[i] === "") {
                accumulated = "/";
                segments.push({ label: "/", path: "/" });
            } else {
                accumulated = (accumulated === "/" ? "/" : accumulated + "/") + parts[i];
                segments.push({ label: parts[i], path: accumulated });
            }
        }

        segments.forEach((seg, i) => {
            const isLast = i === segments.length - 1;
            const crumb  = document.createElement("span");
            crumb.className = "fp-crumb" + (isLast ? " current" : "");
            crumb.textContent = seg.label;
            if (!isLast) crumb.addEventListener("click", () => fpNavigate(seg.path));
            fpBreadcrumb.appendChild(crumb);

            if (!isLast) {
                const sep = document.createElement("span");
                sep.className   = "fp-crumb-sep";
                sep.textContent = "/";
                fpBreadcrumb.appendChild(sep);
            }
        });

        fpBreadcrumb.scrollLeft = fpBreadcrumb.scrollWidth;
    }

    function renderDirList(dirs) {
        fpList.innerHTML = "";

        if (dirs.length === 0) {
            fpList.innerHTML = '<div class="fp-empty">No subfolders here.</div>';
            return;
        }

        dirs.forEach(name => {
            const item = document.createElement("div");
            item.className = "fp-item";
            item.innerHTML = `<span class="fp-item-icon">📁</span><span>${escapeHtml(name)}</span>`;
            item.addEventListener("click", () => {
                const child = (fpCurrentDir === "/" ? "/" : fpCurrentDir + "/") + name;
                fpNavigate(child);
            });
            fpList.appendChild(item);
        });
    }

    function openFolderPicker(targetId, onSelect) {
        fpTargetId = targetId;
        fpOnSelect = onSelect || null;
        const startPath = getFolderValue(targetId) || "";
        fpModal.classList.remove("hidden");
        document.body.style.overflow = "hidden";
        fpNavigate(startPath);
    }

    function closeFolderPicker() {
        fpModal.classList.add("hidden");
        document.body.style.overflow = "";
        fpTargetId = null;
        fpOnSelect = null;
    }

    fpSelectBtn.addEventListener("click", () => {
        if (!fpTargetId || !fpCurrentDir) return;
        setFolderValue(fpTargetId, fpCurrentDir);
        const cb = fpOnSelect;
        closeFolderPicker();
        if (cb) cb(fpCurrentDir);
    });

    fpCancelBtn.addEventListener("click", closeFolderPicker);
    $(".fp-close").addEventListener("click", closeFolderPicker);
    $(".fp-backdrop").addEventListener("click", closeFolderPicker);

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !fpModal.classList.contains("hidden")) closeFolderPicker();
    });

    // Wire up all Browse buttons — callbacks come from PICKER_CALLBACKS
    $$("[data-picker-target]").forEach(btn => {
        btn.addEventListener("click", () => {
            const targetId = btn.dataset.pickerTarget;
            openFolderPicker(targetId, PICKER_CALLBACKS[targetId] || null);
        });
    });

    // Mark unset display spans as placeholder
    Object.keys(DISPLAY_MAP).forEach(inputId => {
        const display = document.getElementById(DISPLAY_MAP[inputId]);
        if (display && !getFolderValue(inputId)) display.classList.add("placeholder");
    });

    // ═══════════ DETECT TAB ═══════════

    let detectJobId = null;
    let detectEventSource = null;

    async function checkCheckpoint(folder) {
        if (!folder) return;
        try {
            const data = await api("/api/checkpoint?folder=" + encodeURIComponent(folder));
            if (data.exists && data.count > 0) {
                $("#detect-checkpoint-text").textContent =
                    `Checkpoint found — ${data.count} videos already processed.`;
                show($("#detect-checkpoint-banner"));
            } else {
                hide($("#detect-checkpoint-banner"));
            }
        } catch (e) {
            hide($("#detect-checkpoint-banner"));
        }
    }

    PICKER_CALLBACKS["detect-folder"] = (path) => checkCheckpoint(path);

    function startDetection(fresh) {
        const folder = getFolderValue("detect-folder");
        if (!folder) { toast("Select a folder first", true); return; }

        const sensitivity = document.querySelector('input[name="sensitivity"]:checked').value;
        const workers     = parseInt($("#detect-workers").value) || 4;

        $("#detect-log").innerHTML = "";
        show($("#detect-log"));
        show($("#detect-progress-section"));
        hide($("#detect-summary"));
        hide($("#detect-post-actions"));
        hide($("#goto-review-btn"));
        hide($("#goto-extract-btn"));
        hide($("#detect-run-btn"));
        show($("#detect-cancel-btn"));
        hide($("#detect-checkpoint-banner"));
        $("#detect-progress-bar").style.width = "0%";
        $("#detect-progress-count").textContent = "";
        $("#detect-progress-label").textContent = "Detecting...";

        api("/api/detect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder, sensitivity, workers, fresh }),
        }).then(data => {
            detectJobId = data.job_id;
            detectEventSource = new EventSource("/api/detect/" + detectJobId + "/stream");
            detectEventSource.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.type === "progress") onDetectProgress(msg);
                if (msg.type === "summary")  onDetectSummary(msg);
                if (msg.type === "done")     onDetectDone();
                if (msg.type === "error")    onDetectError(msg.message);
            };
            detectEventSource.onerror = () => {};
        }).catch(e => {
            toast("Failed to start detection: " + e.message, true);
            resetDetectUI();
        });
    }

    function onDetectProgress(msg) {
        const pct = msg.total > 0 ? (msg.done / msg.total * 100) : 0;
        $("#detect-progress-bar").style.width = pct + "%";
        $("#detect-progress-count").textContent = `${msg.done} / ${msg.total}`;

        const log   = $("#detect-log");
        const entry = document.createElement("div");
        entry.className = "log-entry";

        const decision   = msg.decision || "unknown";
        const resultClass = decision.startsWith("error") ? "error" : decision;
        entry.innerHTML =
            `<span class="filename">${escapeHtml(msg.filename)}</span>` +
            `<span class="result ${resultClass}">${escapeHtml(decision)}</span>` +
            `<span class="confidence">${msg.confidence || ""}</span>`;
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    function onDetectSummary(msg) {
        show($("#detect-summary"));
        $("#sum-static").textContent  = msg.static  || 0;
        $("#sum-dynamic").textContent = msg.dynamic || 0;
        $("#sum-review").textContent  = msg.review  || 0;
        $("#sum-errors").textContent  = msg.errors  || 0;
        $("#sum-space").textContent   = (msg.space_saved_mb || 0) + " MB";
        show($("#detect-post-actions"));
        if ((msg.review || 0) > 0) show($("#goto-review-btn"));
        if ((msg.static || 0) > 0) show($("#goto-extract-btn"));
    }

    function onDetectDone() {
        if (detectEventSource) { detectEventSource.close(); detectEventSource = null; }
        resetDetectUI();
        $("#detect-progress-label").textContent = "Complete";
    }

    function onDetectError(message) {
        if (detectEventSource) { detectEventSource.close(); detectEventSource = null; }
        toast("Detection error: " + message, true);
        resetDetectUI();
    }

    function resetDetectUI() {
        show($("#detect-run-btn"));
        hide($("#detect-cancel-btn"));
    }

    $("#detect-run-btn").addEventListener("click",    () => startDetection(false));
    $("#detect-resume-btn").addEventListener("click", () => startDetection(false));
    $("#detect-fresh-btn").addEventListener("click",  () => startDetection(true));
    $("#detect-cancel-btn").addEventListener("click", () => {
        if (detectJobId) api("/api/detect/" + detectJobId + "/cancel", { method: "POST" }).catch(() => {});
    });

    $("#goto-review-btn").addEventListener("click", () => {
        const folder = getFolderValue("detect-folder");
        setFolderValue("review-folder", folder);
        switchTab("review");
        loadReview();
    });

    $("#goto-extract-btn").addEventListener("click", () => {
        const folder = getFolderValue("detect-folder");
        setFolderValue("extract-folder", folder ? folder + "/static" : "");
        switchTab("extract");
    });

    // ═══════════ REVIEW TAB ═══════════

    let reviewVideos     = [];
    let reviewIndex      = 0;
    let reviewBaseFolder = "";

    async function loadReview() {
        const folder = getFolderValue("review-folder");
        if (!folder) { toast("Select a folder first", true); return; }
        reviewBaseFolder = folder;

        try {
            const data  = await api("/api/review?folder=" + encodeURIComponent(folder));
            reviewVideos = data.videos || [];

            if (reviewVideos.length === 0) {
                hide($("#review-player-area"));
                show($("#review-done-msg"));
                $("#review-done-msg").textContent = "No videos in the review subfolder.";
                hide($("#review-count"));
                return;
            }

            reviewIndex = 0;
            show($("#review-count"));
            $("#review-count").textContent = reviewVideos.length + " video(s) to review";
            hide($("#review-done-msg"));
            show($("#review-player-area"));
            showReviewVideo();
        } catch (e) {
            toast("Failed to load review: " + e.message, true);
        }
    }

    function showReviewVideo() {
        if (reviewIndex < 0 || reviewIndex >= reviewVideos.length) return;
        const v     = reviewVideos[reviewIndex];
        const video = $("#review-video");

        video.src = "/api/video?path=" + encodeURIComponent(v.path);
        video.load();

        if (IS_TOUCH) {
            show($("#review-play-overlay"));
        } else {
            hide($("#review-play-overlay"));
            video.play().catch(() => show($("#review-play-overlay")));
        }

        $("#review-filename").textContent   = v.filename;
        $("#review-confidence").textContent = v.confidence || "—";
        $("#review-motion").textContent     = v.global_motion_score || "—";
        $("#review-duration").textContent   = v.duration_s ? v.duration_s + "s" : "—";
        $("#review-size").textContent       = (v.width && v.height) ? v.width + "x" + v.height : "—";
        $("#review-position").textContent   = (reviewIndex + 1) + " / " + reviewVideos.length;

        $("#review-prev-btn").disabled = reviewIndex === 0;
        $("#review-next-btn").disabled = reviewIndex === reviewVideos.length - 1;
    }

    $("#review-play-overlay").addEventListener("click", () => {
        $("#review-video").play().then(() => hide($("#review-play-overlay"))).catch(() => {});
    });

    async function reviewDecide(decision) {
        if (reviewIndex >= reviewVideos.length) return;
        const v = reviewVideos[reviewIndex];

        try {
            await api("/api/review/decide", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: v.path, decision, base_folder: reviewBaseFolder }),
            });
            v._decided = decision;

            if (reviewIndex < reviewVideos.length - 1) {
                reviewIndex++;
                showReviewVideo();
            } else {
                hide($("#review-player-area"));
                show($("#review-done-msg"));
                const n = reviewVideos.filter(x => x._decided).length;
                $("#review-done-msg").textContent = `All done! ${n} video(s) reviewed.`;
            }
        } catch (e) {
            toast("Review error: " + e.message, true);
        }
    }

    $("#review-load-btn").addEventListener("click",    loadReview);
    $("#review-static-btn").addEventListener("click",  () => reviewDecide("static"));
    $("#review-dynamic-btn").addEventListener("click", () => reviewDecide("dynamic"));
    $("#review-skip-btn").addEventListener("click",    () => reviewDecide("skip"));
    $("#review-prev-btn").addEventListener("click", () => { if (reviewIndex > 0) { reviewIndex--; showReviewVideo(); } });
    $("#review-next-btn").addEventListener("click", () => { if (reviewIndex < reviewVideos.length - 1) { reviewIndex++; showReviewVideo(); } });

    document.addEventListener("keydown", (e) => {
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        if (!fpModal.classList.contains("hidden")) return;
        if (!$("#tab-review").classList.contains("active")) return;
        if (!reviewVideos.length) return;
        if (e.key === "s" || e.key === "S") { e.preventDefault(); reviewDecide("static"); }
        if (e.key === "d" || e.key === "D") { e.preventDefault(); reviewDecide("dynamic"); }
        if (e.key === " ")                  { e.preventDefault(); reviewDecide("skip"); }
    });

    // ═══════════ EXTRACT TAB ═══════════

    let extractJobId = null;
    let extractEventSource = null;

    $("#extract-quality").addEventListener("input", (e) => {
        $("#extract-quality-value").textContent = e.target.value;
    });

    $$('input[name="extract-format"]').forEach(r => {
        r.addEventListener("change", () => {
            const isPng = document.querySelector('input[name="extract-format"]:checked').value === "png";
            if (isPng) hide($("#extract-quality-field"));
            else        show($("#extract-quality-field"));
        });
    });

    $("#extract-output-clear").addEventListener("click", () => {
        setFolderValue("extract-output", "");
        const display = $("#extract-output-display");
        if (display) {
            display.textContent = "Default: <folder>/extracted_frames/";
            display.classList.add("placeholder");
        }
    });

    function startExtraction() {
        const folder = getFolderValue("extract-folder");
        if (!folder) { toast("Select a source folder first", true); return; }

        const fmt          = document.querySelector('input[name="extract-format"]:checked').value;
        const quality      = parseInt($("#extract-quality").value) || 95;
        const workers      = parseInt($("#extract-workers").value) || 4;
        const output       = getFolderValue("extract-output") || null;
        const skipExisting = $("#extract-skip-existing").checked;

        $("#extract-log").innerHTML = "";
        show($("#extract-log"));
        show($("#extract-progress-section"));
        hide($("#extract-summary"));
        hide($("#extract-gallery"));
        hide($("#extract-run-btn"));
        show($("#extract-cancel-btn"));
        $("#extract-progress-bar").style.width = "0%";
        $("#extract-progress-count").textContent = "";
        $("#extract-progress-label").textContent = "Extracting...";

        api("/api/extract", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder, output_dir: output, format: fmt, quality, workers, skip_existing: skipExisting }),
        }).then(data => {
            extractJobId = data.job_id;
            extractEventSource = new EventSource("/api/extract/" + extractJobId + "/stream");
            extractEventSource.onmessage = (event) => {
                const msg = JSON.parse(event.data);
                if (msg.type === "progress") onExtractProgress(msg);
                if (msg.type === "done")     onExtractDone(msg);
                if (msg.type === "error")    onExtractError(msg.message);
            };
            extractEventSource.onerror = () => {};
        }).catch(e => {
            toast("Failed to start extraction: " + e.message, true);
            resetExtractUI();
        });
    }

    function onExtractProgress(msg) {
        const pct = msg.total > 0 ? (msg.done / msg.total * 100) : 0;
        $("#extract-progress-bar").style.width = pct + "%";
        $("#extract-progress-count").textContent = `${msg.done} / ${msg.total}`;

        const log   = $("#extract-log");
        const entry = document.createElement("div");
        entry.className = "log-entry";
        const status = msg.status || "unknown";
        entry.innerHTML =
            `<span class="filename">${escapeHtml(msg.filename)}</span>` +
            `<span class="result ${status === "ok" ? "ok" : "error"}">${escapeHtml(status)}</span>`;
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    function onExtractDone(msg) {
        if (extractEventSource) { extractEventSource.close(); extractEventSource = null; }
        resetExtractUI();
        $("#extract-progress-label").textContent = "Complete";

        if (msg.extracted !== undefined) {
            show($("#extract-summary"));
            $("#ext-sum-extracted").textContent = msg.extracted || 0;
            $("#ext-sum-skipped").textContent   = msg.skipped   || 0;
            $("#ext-sum-errors").textContent    = msg.errors    || 0;
        }
        loadGallery();
    }

    function onExtractError(message) {
        if (extractEventSource) { extractEventSource.close(); extractEventSource = null; }
        toast("Extraction error: " + message, true);
        resetExtractUI();
    }

    function resetExtractUI() {
        show($("#extract-run-btn"));
        hide($("#extract-cancel-btn"));
    }

    async function loadGallery() {
        const folder = getFolderValue("extract-folder");
        if (!folder) return;

        try {
            const data   = await api("/api/frames?folder=" + encodeURIComponent(folder));
            const frames = data.frames || [];
            if (frames.length === 0) return;

            const gallery = $("#extract-gallery");
            gallery.innerHTML = "";
            show(gallery);
            frames.forEach(f => {
                const thumb = document.createElement("div");
                thumb.className = "gallery-thumb";
                thumb.innerHTML =
                    `<img src="/api/image?path=${encodeURIComponent(f.path)}" ` +
                    `loading="lazy" alt="${escapeHtml(f.filename)}">`;
                thumb.addEventListener("click", () => openLightbox(f.path));
                gallery.appendChild(thumb);
            });
        } catch (e) { /* non-critical */ }
    }

    function openLightbox(path) {
        $("#lightbox-img").src = "/api/image?path=" + encodeURIComponent(path);
        show($("#lightbox"));
    }

    function closeLightbox() {
        hide($("#lightbox"));
        $("#lightbox-img").src = "";
    }

    $(".lightbox-backdrop").addEventListener("click", closeLightbox);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !$("#lightbox").classList.contains("hidden")) closeLightbox();
    });

    $("#extract-run-btn").addEventListener("click",    startExtraction);
    $("#extract-cancel-btn").addEventListener("click", () => {
        if (extractJobId) api("/api/extract/" + extractJobId + "/cancel", { method: "POST" }).catch(() => {});
    });

    // ═══════════ SETTINGS TAB ═══════════

    $("#cfg-quality").addEventListener("input", (e) => {
        $("#cfg-quality-value").textContent = e.target.value;
    });

    async function loadSettings() {
        try {
            const cfg = await api("/api/config");

            const sensEl = document.querySelector(`input[name="cfg-sensitivity"][value="${cfg.sensitivity || "medium"}"]`);
            if (sensEl) sensEl.checked = true;
            $("#cfg-workers").value = cfg.workers || 4;
            const fmtEl = document.querySelector(`input[name="cfg-format"][value="${cfg.output_format || "jpg"}"]`);
            if (fmtEl) fmtEl.checked = true;
            $("#cfg-quality").value = cfg.quality || 95;
            $("#cfg-quality-value").textContent = cfg.quality || 95;

            applyConfigDefaults(cfg);
        } catch (e) { /* use defaults */ }
    }

    function applyConfigDefaults(cfg) {
        const sensEl = document.querySelector(`input[name="sensitivity"][value="${cfg.sensitivity || "medium"}"]`);
        if (sensEl) sensEl.checked = true;
        $("#detect-workers").value = cfg.workers || 4;

        const fmtEl = document.querySelector(`input[name="extract-format"][value="${cfg.output_format || "jpg"}"]`);
        if (fmtEl) fmtEl.checked = true;
        $("#extract-quality").value = cfg.quality || 95;
        $("#extract-quality-value").textContent = cfg.quality || 95;
        $("#extract-workers").value = cfg.workers || 4;

        if ((cfg.output_format || "jpg") === "png") hide($("#extract-quality-field"));
        else show($("#extract-quality-field"));
    }

    $("#settings-save-btn").addEventListener("click", async () => {
        const cfg = {
            sensitivity:   document.querySelector('input[name="cfg-sensitivity"]:checked').value,
            workers:       parseInt($("#cfg-workers").value) || 4,
            output_format: document.querySelector('input[name="cfg-format"]:checked').value,
            quality:       parseInt($("#cfg-quality").value) || 95,
        };
        try {
            await api("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(cfg),
            });
            show($("#settings-saved-msg"));
            setTimeout(() => hide($("#settings-saved-msg")), 2000);
            applyConfigDefaults(cfg);
        } catch (e) {
            toast("Failed to save: " + e.message, true);
        }
    });

    // ─── Init ───
    loadSettings();
})();
