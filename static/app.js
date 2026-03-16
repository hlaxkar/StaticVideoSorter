/* ═══════════ StaticSort Frontend ═══════════ */

(function () {
    "use strict";

    const IS_TOUCH = ("ontouchstart" in window) || navigator.maxTouchPoints > 0;
    if (IS_TOUCH) document.body.classList.add("is-touch");

    // ─── Utilities ───

    function $(sel) { return document.querySelector(sel); }
    function $$(sel) { return document.querySelectorAll(sel); }

    function show(el) { el.classList.remove("hidden"); }
    function hide(el) { el.classList.add("hidden"); }

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
            if (e.message === "Failed to fetch") {
                toast("Server not responding", true);
            }
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

    // ─── Autocomplete ───

    function setupAutocomplete(inputEl, dropdownEl) {
        let debounceTimer = null;
        let items = [];
        let selectedIdx = -1;

        function render() {
            dropdownEl.innerHTML = "";
            if (items.length === 0) {
                dropdownEl.classList.remove("open");
                return;
            }
            items.forEach((name, i) => {
                const div = document.createElement("div");
                div.className = "autocomplete-item" + (i === selectedIdx ? " selected" : "");
                div.textContent = name;
                div.addEventListener("mousedown", (e) => {
                    e.preventDefault();
                    select(name);
                });
                dropdownEl.appendChild(div);
            });
            dropdownEl.classList.add("open");
        }

        function select(name) {
            const current = inputEl.value.replace(/\/+$/, "");
            inputEl.value = current + "/" + name;
            items = [];
            selectedIdx = -1;
            dropdownEl.classList.remove("open");
            // Trigger another browse for the new path
            fetchDirs(inputEl.value);
        }

        async function fetchDirs(path) {
            try {
                const data = await api("/api/browse?path=" + encodeURIComponent(path));
                items = data.dirs || [];
                selectedIdx = -1;
                render();
            } catch (e) {
                items = [];
                render();
            }
        }

        inputEl.addEventListener("input", () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => fetchDirs(inputEl.value), 200);
        });

        inputEl.addEventListener("keydown", (e) => {
            if (!dropdownEl.classList.contains("open")) return;
            if (e.key === "ArrowDown") {
                e.preventDefault();
                selectedIdx = Math.min(selectedIdx + 1, items.length - 1);
                render();
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                selectedIdx = Math.max(selectedIdx - 1, 0);
                render();
            } else if (e.key === "Enter" && selectedIdx >= 0) {
                e.preventDefault();
                select(items[selectedIdx]);
            } else if (e.key === "Escape") {
                dropdownEl.classList.remove("open");
            }
        });

        inputEl.addEventListener("blur", () => {
            setTimeout(() => dropdownEl.classList.remove("open"), 150);
        });

        inputEl.addEventListener("focus", () => {
            if (inputEl.value) fetchDirs(inputEl.value);
        });
    }

    setupAutocomplete($("#detect-folder"), $("#detect-folder-dropdown"));
    setupAutocomplete($("#review-folder"), $("#review-folder-dropdown"));
    setupAutocomplete($("#extract-folder"), $("#extract-folder-dropdown"));

    // ═══════════ DETECT TAB ═══════════

    let detectJobId = null;
    let detectEventSource = null;

    // Checkpoint check on folder blur
    $("#detect-folder").addEventListener("blur", async () => {
        const folder = $("#detect-folder").value.trim();
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
    });

    function startDetection(fresh) {
        const folder = $("#detect-folder").value.trim();
        if (!folder) { toast("Enter a folder path", true); return; }

        const sensitivity = document.querySelector('input[name="sensitivity"]:checked').value;
        const workers = parseInt($("#detect-workers").value) || 4;

        // Reset UI
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

            detectEventSource.onerror = () => {
                // SSE auto-reconnects
            };
        }).catch(e => {
            toast("Failed to start detection: " + e.message, true);
            resetDetectUI();
        });
    }

    function onDetectProgress(msg) {
        const pct = msg.total > 0 ? (msg.done / msg.total * 100) : 0;
        $("#detect-progress-bar").style.width = pct + "%";
        $("#detect-progress-count").textContent = `${msg.done} / ${msg.total}`;

        const log = $("#detect-log");
        const entry = document.createElement("div");
        entry.className = "log-entry";

        const decision = msg.decision || "unknown";
        let resultClass = decision;
        if (decision.startsWith("error")) resultClass = "error";

        entry.innerHTML =
            `<span class="filename">${escapeHtml(msg.filename)}</span>` +
            `<span class="result ${resultClass}">${escapeHtml(decision)}</span>` +
            `<span class="confidence">${msg.confidence || ""}</span>`;
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    function onDetectSummary(msg) {
        show($("#detect-summary"));
        $("#sum-static").textContent = msg.static || 0;
        $("#sum-dynamic").textContent = msg.dynamic || 0;
        $("#sum-review").textContent = msg.review || 0;
        $("#sum-errors").textContent = msg.errors || 0;
        $("#sum-space").textContent = (msg.space_saved_mb || 0) + " MB";

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

    $("#detect-run-btn").addEventListener("click", () => startDetection(false));
    $("#detect-resume-btn").addEventListener("click", () => startDetection(false));
    $("#detect-fresh-btn").addEventListener("click", () => startDetection(true));

    $("#detect-cancel-btn").addEventListener("click", () => {
        if (detectJobId) {
            api("/api/detect/" + detectJobId + "/cancel", { method: "POST" })
                .catch(() => {});
        }
    });

    // Post-action buttons
    $("#goto-review-btn").addEventListener("click", () => {
        const folder = $("#detect-folder").value.trim();
        $("#review-folder").value = folder;
        switchTab("review");
        loadReview();
    });

    $("#goto-extract-btn").addEventListener("click", () => {
        const folder = $("#detect-folder").value.trim();
        $("#extract-folder").value = folder + "/static";
        switchTab("extract");
    });

    // ═══════════ REVIEW TAB ═══════════

    let reviewVideos = [];
    let reviewIndex = 0;
    let reviewBaseFolder = "";

    async function loadReview() {
        const folder = $("#review-folder").value.trim();
        if (!folder) { toast("Enter a folder path", true); return; }
        reviewBaseFolder = folder;

        try {
            const data = await api("/api/review?folder=" + encodeURIComponent(folder));
            reviewVideos = data.videos || [];

            if (reviewVideos.length === 0) {
                hide($("#review-player-area"));
                show($("#review-done-msg"));
                $("#review-done-msg").textContent = "No videos to review.";
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
        const v = reviewVideos[reviewIndex];
        const video = $("#review-video");
        const overlay = $("#review-play-overlay");

        video.src = "/api/video?path=" + encodeURIComponent(v.path);
        video.load();

        // Autoplay handling
        if (IS_TOUCH) {
            show(overlay);
        } else {
            hide(overlay);
            video.play().catch(() => show(overlay));
        }

        $("#review-filename").textContent = v.filename;
        $("#review-confidence").textContent = v.confidence || "—";
        $("#review-motion").textContent = v.global_motion_score || "—";
        $("#review-duration").textContent = v.duration_s ? v.duration_s + "s" : "—";
        $("#review-size").textContent = (v.width && v.height) ? v.width + "x" + v.height : "—";
        $("#review-position").textContent = (reviewIndex + 1) + " / " + reviewVideos.length;

        $("#review-prev-btn").disabled = reviewIndex === 0;
        $("#review-next-btn").disabled = reviewIndex === reviewVideos.length - 1;
    }

    // Play overlay tap
    $("#review-play-overlay").addEventListener("click", () => {
        const video = $("#review-video");
        video.play().then(() => hide($("#review-play-overlay"))).catch(() => {});
    });

    async function reviewDecide(decision) {
        if (reviewIndex >= reviewVideos.length) return;
        const v = reviewVideos[reviewIndex];

        try {
            await api("/api/review/decide", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    path: v.path,
                    decision: decision,
                    base_folder: reviewBaseFolder,
                }),
            });

            // Mark this video as decided
            v._decided = decision;

            // Move to next
            if (reviewIndex < reviewVideos.length - 1) {
                reviewIndex++;
                showReviewVideo();
            } else {
                // All done
                hide($("#review-player-area"));
                show($("#review-done-msg"));
                const decidedCount = reviewVideos.filter(x => x._decided).length;
                $("#review-done-msg").textContent =
                    `All done! ${decidedCount} video(s) reviewed.`;
            }
        } catch (e) {
            toast("Review error: " + e.message, true);
        }
    }

    $("#review-load-btn").addEventListener("click", loadReview);
    $("#review-static-btn").addEventListener("click", () => reviewDecide("static"));
    $("#review-dynamic-btn").addEventListener("click", () => reviewDecide("dynamic"));
    $("#review-skip-btn").addEventListener("click", () => reviewDecide("skip"));
    $("#review-prev-btn").addEventListener("click", () => {
        if (reviewIndex > 0) { reviewIndex--; showReviewVideo(); }
    });
    $("#review-next-btn").addEventListener("click", () => {
        if (reviewIndex < reviewVideos.length - 1) { reviewIndex++; showReviewVideo(); }
    });

    // Keyboard shortcuts
    document.addEventListener("keydown", (e) => {
        // Don't trigger when typing in inputs
        if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
        // Only when review tab is active
        if (!$("#tab-review").classList.contains("active")) return;
        if (!reviewVideos.length) return;

        if (e.key === "s" || e.key === "S") { e.preventDefault(); reviewDecide("static"); }
        if (e.key === "d" || e.key === "D") { e.preventDefault(); reviewDecide("dynamic"); }
        if (e.key === " ")                  { e.preventDefault(); reviewDecide("skip"); }
    });

    // ═══════════ EXTRACT TAB ═══════════

    let extractJobId = null;
    let extractEventSource = null;

    // Quality slider
    $("#extract-quality").addEventListener("input", (e) => {
        $("#extract-quality-value").textContent = e.target.value;
    });

    // Format toggle — hide quality for PNG
    $$('input[name="extract-format"]').forEach(r => {
        r.addEventListener("change", () => {
            const isPng = document.querySelector('input[name="extract-format"]:checked').value === "png";
            if (isPng) hide($("#extract-quality-field"));
            else show($("#extract-quality-field"));
        });
    });

    function startExtraction() {
        const folder = $("#extract-folder").value.trim();
        if (!folder) { toast("Enter a folder path", true); return; }

        const fmt     = document.querySelector('input[name="extract-format"]:checked').value;
        const quality = parseInt($("#extract-quality").value) || 95;
        const workers = parseInt($("#extract-workers").value) || 4;
        const output  = $("#extract-output").value.trim() || null;
        const skipExisting = $("#extract-skip-existing").checked;

        // Reset UI
        $("#extract-log").innerHTML = "";
        show($("#extract-log"));
        show($("#extract-progress-section"));
        hide($("#extract-summary"));
        hide($("#extract-gallery"));
        hide($("#extract-run-btn"));
        show($("#extract-cancel-btn"));
        $("#extract-progress-bar").style.width = "0%";
        $("#extract-progress-count").textContent = "";

        api("/api/extract", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                folder, output_dir: output, format: fmt,
                quality, workers, skip_existing: skipExisting,
            }),
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

        const log = $("#extract-log");
        const entry = document.createElement("div");
        entry.className = "log-entry";

        const status = msg.status || "unknown";
        let resultClass = status === "ok" ? "ok" : "error";

        entry.innerHTML =
            `<span class="filename">${escapeHtml(msg.filename)}</span>` +
            `<span class="result ${resultClass}">${escapeHtml(status)}</span>`;
        log.appendChild(entry);
        log.scrollTop = log.scrollHeight;
    }

    function onExtractDone(msg) {
        if (extractEventSource) { extractEventSource.close(); extractEventSource = null; }
        resetExtractUI();
        $("#extract-progress-label").textContent = "Complete";

        // Show summary if we have it
        if (msg.extracted !== undefined) {
            show($("#extract-summary"));
            $("#ext-sum-extracted").textContent = msg.extracted || 0;
            $("#ext-sum-skipped").textContent = msg.skipped || 0;
            $("#ext-sum-errors").textContent = msg.errors || 0;
        }

        // Load gallery
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
        const folder = $("#extract-folder").value.trim();
        if (!folder) return;

        try {
            const data = await api("/api/frames?folder=" + encodeURIComponent(folder));
            const frames = data.frames || [];
            if (frames.length === 0) return;

            const gallery = $("#extract-gallery");
            gallery.innerHTML = "";
            show(gallery);

            frames.forEach(f => {
                const thumb = document.createElement("div");
                thumb.className = "gallery-thumb";
                thumb.innerHTML = `<img src="/api/image?path=${encodeURIComponent(f.path)}" loading="lazy" alt="${escapeHtml(f.filename)}">`;
                thumb.addEventListener("click", () => openLightbox(f.path));
                gallery.appendChild(thumb);
            });
        } catch (e) {
            // Gallery load failed, that's fine
        }
    }

    function openLightbox(path) {
        const lb = $("#lightbox");
        $("#lightbox-img").src = "/api/image?path=" + encodeURIComponent(path);
        show(lb);
    }

    function closeLightbox() {
        hide($("#lightbox"));
        $("#lightbox-img").src = "";
    }

    $(".lightbox-backdrop").addEventListener("click", closeLightbox);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !$("#lightbox").classList.contains("hidden")) {
            closeLightbox();
        }
    });

    $("#extract-run-btn").addEventListener("click", startExtraction);
    $("#extract-cancel-btn").addEventListener("click", () => {
        if (extractJobId) {
            api("/api/extract/" + extractJobId + "/cancel", { method: "POST" })
                .catch(() => {});
        }
    });

    // ═══════════ SETTINGS TAB ═══════════

    // Settings quality slider
    $("#cfg-quality").addEventListener("input", (e) => {
        $("#cfg-quality-value").textContent = e.target.value;
    });

    async function loadSettings() {
        try {
            const cfg = await api("/api/config");

            // Sensitivity
            const sensRadio = document.querySelector(`input[name="cfg-sensitivity"][value="${cfg.sensitivity || "medium"}"]`);
            if (sensRadio) sensRadio.checked = true;

            // Workers
            $("#cfg-workers").value = cfg.workers || 4;

            // Format
            const fmtRadio = document.querySelector(`input[name="cfg-format"][value="${cfg.output_format || "jpg"}"]`);
            if (fmtRadio) fmtRadio.checked = true;

            // Quality
            $("#cfg-quality").value = cfg.quality || 95;
            $("#cfg-quality-value").textContent = cfg.quality || 95;

            // Apply to other tabs
            applyConfigDefaults(cfg);
        } catch (e) {
            // Config load failed, use defaults
        }
    }

    function applyConfigDefaults(cfg) {
        // Detect tab
        const detectSens = document.querySelector(`input[name="sensitivity"][value="${cfg.sensitivity || "medium"}"]`);
        if (detectSens) detectSens.checked = true;
        $("#detect-workers").value = cfg.workers || 4;

        // Extract tab
        const extFmt = document.querySelector(`input[name="extract-format"][value="${cfg.output_format || "jpg"}"]`);
        if (extFmt) extFmt.checked = true;
        $("#extract-quality").value = cfg.quality || 95;
        $("#extract-quality-value").textContent = cfg.quality || 95;
        $("#extract-workers").value = cfg.workers || 4;

        // Visibility
        const isPng = (cfg.output_format || "jpg") === "png";
        if (isPng) hide($("#extract-quality-field"));
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

    // ─── HTML Escape ───
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ─── Init ───
    loadSettings();
})();
