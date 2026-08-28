(() => {
  "use strict";

  const SESSION_TOKEN_KEY = "makeInteractivePdfsToken";
  const THEME_STORAGE_KEY = "makeInteractivePdfsTheme";
  const POLL_DELAY_MS = 850;
  const HEARTBEAT_DELAY_MS = 5000;
  const HEARTBEAT_TIMEOUT_MS = 3500;
  const HEARTBEAT_FAILURE_LIMIT = 2;
  const DEFAULT_DOCUMENT_TITLE = document.title;
  const STAGES = ["PREFLIGHT", "ANALYZING", "WRITING", "VERIFYING"];
  const STAGE_PROGRESS = {
    PREFLIGHT: 12,
    ANALYZING: 38,
    WRITING: 67,
    VERIFYING: 88,
  };
  const STAGE_MESSAGES = {
    PREFLIGHT: "Checking whether this PDF can be processed safely.",
    ANALYZING: "Finding the table of contents and visible addresses.",
    WRITING: "Adding links to a separate copy of your PDF.",
    VERIFYING: "Checking every link before the file is released.",
  };

  const elements = {
    views: Array.from(document.querySelectorAll(".view")),
    selectView: document.querySelector("#selectView"),
    readyView: document.querySelector("#readyView"),
    processingView: document.querySelector("#processingView"),
    passView: document.querySelector("#passView"),
    reviewView: document.querySelector("#reviewView"),
    failView: document.querySelector("#failView"),
    closedView: document.querySelector("#closedView"),
    sessionNotice: document.querySelector("#sessionNotice"),
    dropZone: document.querySelector("#dropZone"),
    pdfInput: document.querySelector("#pdfInput"),
    fileError: document.querySelector("#fileError"),
    selectedFileName: document.querySelector("#selectedFileName"),
    selectedFileSize: document.querySelector("#selectedFileSize"),
    processingFileName: document.querySelector("#processingFileName"),
    readyMessage: document.querySelector("#readyMessage"),
    chooseAnotherButton: document.querySelector("#chooseAnotherButton"),
    startButton: document.querySelector("#startButton"),
    cancelButton: document.querySelector("#cancelButton"),
    processingMessage: document.querySelector("#processingMessage"),
    activityOverview: document.querySelector("#activityOverview"),
    activityLabel: document.querySelector("#activityLabel"),
    activityMeasure: document.querySelector("#activityMeasure"),
    activityCurrent: document.querySelector("#activityCurrent"),
    activityTotal: document.querySelector("#activityTotal"),
    activityUnit: document.querySelector("#activityUnit"),
    activityHistoryRegion: document.querySelector("#activityHistoryRegion"),
    activityHistory: document.querySelector("#activityHistory"),
    progressAnnouncement: document.querySelector("#progressAnnouncement"),
    scanVisual: document.querySelector("#scanVisual"),
    progressTrack: document.querySelector("#progressTrack"),
    progressFill: document.querySelector("#progressFill"),
    stageItems: Array.from(document.querySelectorAll("#stageList li")),
    totalLinks: document.querySelector("#totalLinks"),
    contentsLinks: document.querySelector("#contentsLinks"),
    webLinks: document.querySelector("#webLinks"),
    reviewReasons: document.querySelector("#reviewReasons"),
    failMessage: document.querySelector("#failMessage"),
    downloadPdfButton: document.querySelector("#downloadPdfButton"),
    downloadPassReportButton: document.querySelector("#downloadPassReportButton"),
    downloadReviewReportButton: document.querySelector("#downloadReviewReportButton"),
    passDownloadMessage: document.querySelector("#passDownloadMessage"),
    reviewDownloadMessage: document.querySelector("#reviewDownloadMessage"),
    retryButton: document.querySelector("#retryButton"),
    passStartOverButton: document.querySelector("#passStartOverButton"),
    reviewStartOverButton: document.querySelector("#reviewStartOverButton"),
    failStartOverButton: document.querySelector("#failStartOverButton"),
    closedMessage: document.querySelector("#closedMessage"),
    utilityPanel: document.querySelector(".utility-panel"),
    themeToggle: document.querySelector("#themeToggle"),
    appVersion: document.querySelector("#appVersion"),
  };

  const state = {
    token: captureSessionToken(),
    file: null,
    jobId: null,
    pollSequence: 0,
    lastPayload: null,
    uploadController: null,
    requestControllers: new Set(),
    heartbeatFailures: 0,
    heartbeatInFlight: false,
    heartbeatTimer: null,
    appClosed: false,
    connectionUnavailable: false,
    viewBeforeDisconnect: null,
    titleBeforeDisconnect: DEFAULT_DOCUMENT_TITLE,
    lastProgressSequence: -1,
    lastAnnouncedPhase: "",
    lastAnnouncedBucket: -1,
    localActivityHistory: [],
  };

  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  function effectiveTheme() {
    const selected = document.documentElement.dataset.theme;
    if (selected === "light" || selected === "dark") {
      return selected;
    }
    return systemTheme.matches ? "dark" : "light";
  }

  function updateThemeControl() {
    const current = effectiveTheme();
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.effectiveTheme = current;
    elements.themeToggle.setAttribute("aria-label", `Switch to ${next} mode`);
    elements.themeToggle.setAttribute("title", `Switch to ${next} mode`);
    elements.themeToggle.setAttribute("aria-pressed", String(current === "dark"));
  }

  function initializeTheme() {
    updateThemeControl();
    elements.themeToggle.addEventListener("click", () => {
      const selected = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = selected;
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, selected);
      } catch (_error) {
        // The current page can still switch theme when storage is unavailable.
      }
      updateThemeControl();
    });

    const followSystemTheme = () => {
      if (!document.documentElement.dataset.theme) {
        updateThemeControl();
      }
    };
    if (typeof systemTheme.addEventListener === "function") {
      systemTheme.addEventListener("change", followSystemTheme);
    } else {
      systemTheme.addListener(followSystemTheme);
    }
  }

  async function loadAppVersion() {
    try {
      const response = await window.fetch("/healthz", { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      if (typeof payload.version === "string" && payload.version.trim()) {
        const version = payload.version.trim();
        elements.appVersion.textContent = `v${version}`;
        elements.appVersion.setAttribute("aria-label", `App version ${version}`);
      }
    } catch (_error) {
      // Keep the bundled version label if the controller is already closing.
    }
  }

  function captureSessionToken() {
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const queryParams = new URLSearchParams(window.location.search);
    const suppliedToken = hashParams.get("token") || queryParams.get("token");
    let savedToken = "";

    try {
      savedToken = window.sessionStorage.getItem(SESSION_TOKEN_KEY) || "";
      if (suppliedToken) {
        window.sessionStorage.setItem(SESSION_TOKEN_KEY, suppliedToken);
      }
    } catch (_error) {
      savedToken = "";
    }

    if (window.location.hash || window.location.search) {
      window.history.replaceState(null, document.title, window.location.pathname);
    }

    return suppliedToken || savedToken;
  }

  async function trackedFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-App-Token", state.token);
    const controller = new AbortController();
    const externalSignal = options.signal;
    const forwardAbort = () => controller.abort(externalSignal?.reason);

    if (externalSignal?.aborted) {
      forwardAbort();
    } else {
      externalSignal?.addEventListener("abort", forwardAbort, { once: true });
    }

    state.requestControllers.add(controller);
    try {
      return await window.fetch(path, { ...options, headers, signal: controller.signal });
    } finally {
      state.requestControllers.delete(controller);
      externalSignal?.removeEventListener("abort", forwardAbort);
    }
  }

  async function apiFetch(path, options = {}) {
    if (state.appClosed) {
      throw new DOMException("The local app is closed.", "AbortError");
    }

    try {
      const response = await trackedFetch(path, options);
      if (response.status === 401 || response.status === 403) {
        showLocalAppClosed("auth");
      } else {
        recordTransportSuccess();
      }
      return response;
    } catch (error) {
      if (error?.name !== "AbortError" && isNetworkFailure(error)) {
        recordTransportFailure();
      }
      throw error;
    }
  }

  async function sendHeartbeat() {
    if (!state.token || state.appClosed || state.heartbeatInFlight) {
      return;
    }

    state.heartbeatInFlight = true;
    const timeoutController = new AbortController();
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      timeoutController.abort();
    }, HEARTBEAT_TIMEOUT_MS);

    try {
      const response = await trackedFetch("/api/session/heartbeat", {
        method: "POST",
        signal: timeoutController.signal,
      });
      if (response.status === 401 || response.status === 403) {
        showLocalAppClosed("auth");
        return;
      }
      // Any non-auth HTTP response proves that the local server is alive.
      recordTransportSuccess();
    } catch (error) {
      if (!state.appClosed && (timedOut || isNetworkFailure(error))) {
        recordTransportFailure();
      }
    } finally {
      window.clearTimeout(timeout);
      state.heartbeatInFlight = false;
    }
  }

  function isNetworkFailure(error) {
    return error instanceof TypeError;
  }

  function recordTransportSuccess() {
    state.heartbeatFailures = 0;
    if (state.connectionUnavailable && !state.appClosed) {
      restoreLocalAppConnection();
    }
  }

  function recordTransportFailure() {
    state.heartbeatFailures += 1;
    if (state.heartbeatFailures >= HEARTBEAT_FAILURE_LIMIT) {
      showLocalAppClosed("network");
    }
  }

  function showLocalAppClosed(reason) {
    const permanent = reason === "auth";
    if (state.appClosed || (!permanent && state.connectionUnavailable)) {
      return;
    }

    if (permanent) {
      state.appClosed = true;
    } else {
      state.connectionUnavailable = true;
      state.viewBeforeDisconnect = elements.views.find((view) => !view.hidden) || null;
      state.titleBeforeDisconnect = document.title || DEFAULT_DOCUMENT_TITLE;
    }
    ++state.pollSequence;
    state.uploadController?.abort();
    state.uploadController = null;
    state.requestControllers.forEach((controller) => controller.abort());
    state.requestControllers.clear();
    if (permanent && state.heartbeatTimer !== null) {
      window.clearInterval(state.heartbeatTimer);
      state.heartbeatTimer = null;
    }

    if (permanent) {
      elements.utilityPanel.querySelectorAll("button, input").forEach((control) => {
        control.disabled = true;
      });
    }
    elements.dropZone.classList.remove("is-dragging");
    elements.dropZone.setAttribute("aria-disabled", "true");
    elements.sessionNotice.hidden = true;
    elements.closedMessage.textContent =
      reason === "auth"
        ? "This browser session is no longer authorized. Reopen the Make Interactive PDFs EXE to start a new private session."
        : "The local app stopped responding or was closed. Trying to reconnect automatically… If you closed the EXE, reopen it to continue.";
    document.title = "Local app is closed | Make Interactive PDFs";
    showView(elements.closedView, true);
  }

  function restoreLocalAppConnection() {
    if (!state.connectionUnavailable || state.appClosed) {
      return;
    }

    state.connectionUnavailable = false;
    state.heartbeatFailures = 0;
    elements.dropZone.setAttribute("aria-disabled", state.token ? "false" : "true");
    document.title = state.titleBeforeDisconnect || DEFAULT_DOCUMENT_TITLE;
    const previousView = state.viewBeforeDisconnect;
    state.viewBeforeDisconnect = null;

    if (state.jobId) {
      if (state.lastPayload && handleJobPayload(state.lastPayload)) {
        return;
      }
      showView(elements.processingView);
      const sequence = ++state.pollSequence;
      void pollJob(sequence).catch((error) => {
        if (error?.name === "AbortError" || sequence !== state.pollSequence) {
          return;
        }
        showFailure(friendlyError(error));
      });
      return;
    }

    const fallbackView = state.file ? elements.readyView : elements.selectView;
    const restoredView =
      previousView && ![elements.closedView, elements.processingView].includes(previousView)
        ? previousView
        : fallbackView;
    if (previousView === elements.processingView && state.file) {
      setInlineMessage(elements.readyMessage, "Connection restored. Select Make interactive to try again.");
    }
    showView(restoredView);
  }

  async function discardJob(jobId) {
    if (!jobId) {
      return;
    }
    try {
      await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    } catch (_error) {
      // The server also removes every launch directory when the app closes.
    }
  }

  function showView(view, focusHeading = false) {
    elements.views.forEach((candidate) => {
      candidate.hidden = candidate !== view;
    });
    document.body.classList.toggle(
      "is-processing",
      view === elements.processingView,
    );

    if (focusHeading) {
      const heading = view.querySelector("h2[tabindex='-1']");
      window.requestAnimationFrame(() => heading?.focus({ preventScroll: true }));
    }
  }

  function setInlineMessage(element, message) {
    element.textContent = message;
    element.hidden = !message;
  }

  function chooseFile(fileList) {
    setInlineMessage(elements.fileError, "");

    if (!state.token || state.appClosed) {
      return;
    }

    if (!fileList || fileList.length === 0) {
      return;
    }

    if (fileList.length > 1) {
      setInlineMessage(elements.fileError, "Please choose one PDF at a time.");
      return;
    }

    const file = fileList[0];
    const hasPdfExtension = file.name.toLowerCase().endsWith(".pdf");
    const hasPdfType = file.type === "application/pdf";

    if (!hasPdfExtension && !hasPdfType) {
      setInlineMessage(elements.fileError, "That file is not a PDF. Please choose a file ending in .pdf.");
      elements.pdfInput.value = "";
      return;
    }

    if (file.size === 0) {
      setInlineMessage(elements.fileError, "This PDF is empty. Please choose a different file.");
      elements.pdfInput.value = "";
      return;
    }

    state.file = file;
    elements.selectedFileName.textContent = file.name;
    elements.selectedFileSize.textContent = formatFileSize(file.size);
    elements.processingFileName.textContent = file.name;
    setInlineMessage(elements.readyMessage, "");
    showView(elements.readyView, true);
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) {
      return `${bytes} bytes`;
    }

    const units = ["KB", "MB", "GB"];
    let value = bytes / 1024;
    let unitIndex = 0;

    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex += 1;
    }

    const digits = value >= 10 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unitIndex]}`;
  }

  function resetProgress() {
    state.lastProgressSequence = -1;
    state.lastAnnouncedPhase = "";
    state.lastAnnouncedBucket = -1;
    state.localActivityHistory = [];
    updateProgress("PREFLIGHT", 4);
    elements.processingMessage.textContent = "Starting the local processor.";
    elements.activityLabel.textContent = "Preparing your PDF";
    elements.activityMeasure.hidden = true;
    elements.activityUnit.textContent = "Starting locally";
    elements.activityHistory.replaceChildren();
    elements.activityHistoryRegion.hidden = true;
    elements.scanVisual.classList.add("is-scanning");
    elements.progressTrack.classList.add("is-indeterminate");
    elements.progressTrack.removeAttribute("aria-valuenow");
    elements.progressTrack.setAttribute("aria-valuetext", "Preparing your PDF");
    elements.progressAnnouncement.textContent = "Preparing your PDF.";
  }

  function normalizeStage(value) {
    const stage = String(value || "").trim().toUpperCase();

    if (["QUEUED", "PENDING", "STARTING", "UPLOADING", "PREPARING", "CHECKING"].includes(stage)) {
      return "PREFLIGHT";
    }
    if (
      [
        "ANALYSIS",
        "DETECTING",
        "MAPPING",
        "READING",
        "READING_PAGES",
        "OCR_PAGES",
        "PLANNING_LINKS",
      ].includes(stage)
    ) {
      return "ANALYZING";
    }
    if (["MAKING", "LINKING", "GENERATING", "SAVING", "WRITING_LINKS"].includes(stage)) {
      return "WRITING";
    }
    if (
      [
        "VERIFY",
        "VALIDATING",
        "VALIDATING_PAGES",
        "CHECKING_OUTPUT",
        "VERIFYING_PAGES",
        "VERIFYING_LINKS",
      ].includes(
        stage,
      )
    ) {
      return "VERIFYING";
    }

    return STAGES.includes(stage) ? stage : "PREFLIGHT";
  }

  function updateProgress(stageValue, explicitProgress) {
    const stage = normalizeStage(stageValue);
    const currentIndex = STAGES.indexOf(stage);
    const numericProgress = Number(explicitProgress);
    const progress = Number.isFinite(numericProgress)
      ? Math.min(100, Math.max(4, numericProgress <= 1 ? numericProgress * 100 : numericProgress))
      : STAGE_PROGRESS[stage];

    elements.progressFill.style.transform = `scaleX(${progress / 100})`;
    elements.progressTrack.classList.remove("is-indeterminate");
    elements.progressTrack.setAttribute("aria-valuemax", "100");
    elements.progressTrack.setAttribute("aria-valuenow", String(Math.round(progress)));
    elements.progressTrack.setAttribute("aria-valuetext", `${Math.round(progress)} percent complete`);

    elements.stageItems.forEach((item, index) => {
      const isComplete = index < currentIndex;
      const isCurrent = index === currentIndex;
      item.classList.toggle("is-complete", isComplete);
      item.classList.toggle("is-current", isCurrent);
      item.toggleAttribute("aria-current", isCurrent);
      const status = item.querySelector(".stage-status");
      status.textContent = isComplete ? "Complete" : isCurrent ? "In progress" : "Pending";
    });
  }

  function optionalCount(value) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.floor(number) : null;
  }

  function phaseLabel(phaseValue) {
    const phase = String(phaseValue || "").trim().toUpperCase();
    const labels = {
      PREFLIGHT: "Checking the PDF",
      READING_PAGES: "Reading pages",
      OCR_PAGES: "Reading scanned pages",
      PLANNING_LINKS: "Matching link targets",
      ANALYZING: "Finding link targets",
      WRITING_LINKS: "Adding links",
      WRITING: "Adding links",
      VALIDATING_PAGES: "Checking the new PDF",
      VERIFYING_PAGES: "Verifying pages",
      VERIFYING_LINKS: "Checking links",
      VERIFYING: "Verifying the result",
    };
    return labels[phase] || "Processing your PDF";
  }

  function activityFromPayload(payload) {
    const raw = payload?.activity;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return null;
    }

    const phase = String(raw.phase || payload.stage || "PREFLIGHT").trim().toUpperCase();
    const page = optionalCount(raw.page);
    const totalPages = optionalCount(raw.total_pages);
    const completed = optionalCount(raw.completed);
    const total = optionalCount(raw.total);
    const label = typeof raw.label === "string" && raw.label.trim() ? raw.label.trim() : phaseLabel(phase);

    const rawUnit = String(raw.unit || "item").trim().toLowerCase();
    const usesPages = ["page", "pages"].includes(rawUnit);
    const usesOcrCandidateCount = phase === "OCR_PAGES" && usesPages;
    const unit = ["page", "pages"].includes(rawUnit)
      ? "page"
      : ["link", "links"].includes(rawUnit)
        ? "link"
        : rawUnit;

    return {
      phase,
      label,
      current: usesOcrCandidateCount ? completed : usesPages ? page : completed,
      total: usesOcrCandidateCount ? total : usesPages ? totalPages : total,
      unit: usesPages ? "page" : unit,
      history: Array.isArray(raw.history) ? raw.history : [],
    };
  }

  function activityUnitText(activity) {
    const plural = activity.total === 1 ? "" : "s";
    if (activity.unit === "page") {
      if (activity.phase === "OCR_PAGES") {
        return `OCR page${plural} processed`;
      }
      return activity.phase.includes("VERIFY") ? `page${plural} verified` : `page${plural} checked`;
    }
    if (activity.unit === "link") {
      return activity.phase.includes("VERIFY") ? `link${plural} verified` : `link${plural} added`;
    }
    const cleanUnit = activity.unit && activity.unit !== "item" ? activity.unit : "item";
    return `${cleanUnit}${plural} complete`;
  }

  function activityDetail(activity) {
    if (activity.current === null || activity.total === null || activity.total <= 0) {
      return "";
    }
    return `${activity.current} of ${activity.total} ${activityUnitText(activity)}`;
  }

  function normalizedHistoryItem(item, fallbackPhase) {
    if (typeof item === "string") {
      return {
        phase: String(fallbackPhase || "ACTIVITY").trim().toUpperCase(),
        label: item.trim() || "Processing your PDF",
        detail: "",
      };
    }
    if (!item || typeof item !== "object") {
      return null;
    }

    const phase = String(item.phase || fallbackPhase || "").trim().toUpperCase();
    const label =
      typeof item.label === "string" && item.label.trim() ? item.label.trim() : phaseLabel(phase);
    const page = optionalCount(item.page);
    const totalPages = optionalCount(item.total_pages);
    const completed = optionalCount(item.completed);
    const total = optionalCount(item.total);
    const rawUnit = String(item.unit || "item").trim().toLowerCase();
    const usesPages = ["page", "pages"].includes(rawUnit);
    const usesOcrCandidateCount = phase === "OCR_PAGES" && usesPages;
    const unit = ["page", "pages"].includes(rawUnit)
      ? "page"
      : ["link", "links"].includes(rawUnit)
        ? "link"
        : rawUnit;
    const activity = {
      phase,
      label,
      current: usesOcrCandidateCount ? completed : usesPages ? page : completed,
      total: usesOcrCandidateCount ? total : usesPages ? totalPages : total,
      unit: usesPages ? "page" : unit,
    };
    return { phase, label, detail: activityDetail(activity) };
  }

  function updateLocalActivityHistory(activity) {
    const entry = {
      phase: activity.phase,
      label: activity.label,
      detail: activityDetail(activity),
    };
    const latest = state.localActivityHistory.at(-1);
    if (latest?.phase === entry.phase && latest?.label === entry.label) {
      state.localActivityHistory[state.localActivityHistory.length - 1] = entry;
    } else {
      state.localActivityHistory.push(entry);
      state.localActivityHistory = state.localActivityHistory.slice(-4);
    }
  }

  function renderActivityHistory(activity) {
    const source = activity.history
      .map((item) => normalizedHistoryItem(item, activity.phase))
      .filter(Boolean);
    const collapsed = [];
    source.forEach((entry) => {
      const latest = collapsed.at(-1);
      if (latest?.phase === entry.phase && latest?.label === entry.label) {
        collapsed[collapsed.length - 1] = entry;
      } else {
        collapsed.push(entry);
      }
    });

    updateLocalActivityHistory(activity);
    const entries = (collapsed.length ? collapsed : state.localActivityHistory).slice(-4);
    elements.activityHistory.replaceChildren();
    entries.forEach((entry) => {
      const item = document.createElement("li");
      const marker = document.createElement("span");
      const label = document.createElement("span");
      const detail = document.createElement("span");
      marker.className = "history-marker";
      marker.setAttribute("aria-hidden", "true");
      label.className = "history-label";
      label.textContent = entry.label;
      detail.className = "history-detail";
      detail.textContent = entry.detail;
      item.append(marker, label, detail);
      elements.activityHistory.append(item);
    });
    elements.activityHistoryRegion.hidden = entries.length === 0;
  }

  function announceActivity(activity) {
    const hasMeasure = activity.current !== null && activity.total !== null && activity.total > 0;
    const bucket = hasMeasure
      ? Math.min(10, Math.floor((Math.min(activity.current, activity.total) / activity.total) * 10))
      : -1;
    const phaseChanged = activity.phase !== state.lastAnnouncedPhase;
    const milestoneChanged = bucket >= 0 && bucket > state.lastAnnouncedBucket;
    const complete = hasMeasure && activity.current >= activity.total;

    if (phaseChanged || milestoneChanged || complete) {
      const detail = activityDetail(activity);
      elements.progressAnnouncement.textContent = detail
        ? `${activity.label}. ${detail}.`
        : `${activity.label}.`;
      state.lastAnnouncedPhase = activity.phase;
      state.lastAnnouncedBucket = bucket;
    }
  }

  function renderActivity(payload) {
    const activity = activityFromPayload(payload);
    if (!activity) {
      return;
    }

    const sequence = optionalCount(payload.progress_sequence);
    if (sequence !== null && sequence <= state.lastProgressSequence) {
      return;
    }
    if (sequence !== null) {
      state.lastProgressSequence = sequence;
    }

    const hasMeasure = activity.current !== null && activity.total !== null && activity.total > 0;
    elements.activityLabel.textContent = activity.label;
    elements.activityMeasure.hidden = !hasMeasure;
    elements.activityUnit.textContent = hasMeasure ? activityUnitText(activity) : "Working locally";
    elements.scanVisual.classList.toggle("is-scanning", true);

    if (hasMeasure) {
      const current = Math.min(activity.current, activity.total);
      elements.activityCurrent.textContent = String(current);
      elements.activityTotal.textContent = String(activity.total);
    }

    renderActivityHistory(activity);
    announceActivity(activity);
  }

  function valueFrom(object, paths) {
    for (const path of paths) {
      const value = path.split(".").reduce((current, key) => current?.[key], object);
      if (value !== undefined && value !== null) {
        return value;
      }
    }
    return undefined;
  }

  function normalizedStatus(payload) {
    return String(
      valueFrom(payload, ["outcome", "result.status", "result.outcome", "status", "state"]) || "RUNNING",
    )
      .trim()
      .toUpperCase()
      .replace(/[ -]+/g, "_");
  }

  function reportIsAvailable(payload, fallback = false) {
    const value = valueFrom(payload, [
      "report_available",
      "has_report",
      "downloads.report.available",
      "downloads.report",
      "result.report_available",
    ]);
    return value === undefined ? fallback : Boolean(value);
  }

  function getPayloadMessage(payload) {
    const message = valueFrom(payload, [
      "message",
      "error.message",
      "error",
      "detail.message",
      "detail",
      "result.message",
    ]);
    return typeof message === "string" ? message : "";
  }

  async function readResponse(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      return response.json();
    }

    const text = await response.text();
    return text ? { message: text } : {};
  }

  async function startJob() {
    if (!state.file || !state.token || state.appClosed) {
      return;
    }

    const sequence = ++state.pollSequence;
    const uploadController = new AbortController();
    state.uploadController = uploadController;
    state.jobId = null;
    state.lastPayload = null;
    elements.startButton.disabled = true;
    elements.startButton.textContent = "Starting...";
    resetProgress();
    showView(elements.processingView, true);

    const formData = new FormData();
    formData.append("file", state.file, state.file.name);

    try {
      const response = await apiFetch("/api/jobs", {
        method: "POST",
        body: formData,
        signal: uploadController.signal,
      });
      const payload = await readResponse(response);

      if (!response.ok) {
        throw new Error(getPayloadMessage(payload) || friendlyHttpError(response.status));
      }

      const returnedJobId = valueFrom(payload, ["id", "job_id", "job.id"]);
      if (sequence !== state.pollSequence) {
        void discardJob(returnedJobId);
        return;
      }

      state.jobId = returnedJobId;
      state.lastPayload = payload;

      if (handleJobPayload(payload)) {
        return;
      }

      if (!state.jobId) {
        throw new Error("The local processor did not return a job number. Please try again.");
      }

      await pollJob(sequence);
    } catch (error) {
      if (error?.name === "AbortError" || sequence !== state.pollSequence) {
        return;
      }
      showFailure(friendlyError(error));
    } finally {
      if (state.uploadController === uploadController) {
        state.uploadController = null;
      }
      if (!state.appClosed) {
        elements.startButton.disabled = false;
        elements.startButton.textContent = "Make interactive";
      }
    }
  }

  async function pollJob(sequence) {
    while (sequence === state.pollSequence && state.jobId) {
      let response;
      try {
        response = await apiFetch(`/api/jobs/${encodeURIComponent(state.jobId)}`);
      } catch (error) {
        if (error?.name === "AbortError" || sequence !== state.pollSequence) {
          return;
        }
        if (isNetworkFailure(error) && !state.appClosed) {
          await delay(POLL_DELAY_MS);
          continue;
        }
        throw error;
      }
      if (sequence !== state.pollSequence) {
        return;
      }
      const payload = await readResponse(response);

      if (sequence !== state.pollSequence) {
        return;
      }

      if (!response.ok) {
        throw new Error(getPayloadMessage(payload) || friendlyHttpError(response.status));
      }

      state.lastPayload = payload;
      if (handleJobPayload(payload)) {
        return;
      }

      await delay(POLL_DELAY_MS);
    }
  }

  function handleJobPayload(payload) {
    const status = normalizedStatus(payload);
    const stage = valueFrom(payload, ["stage", "progress.stage", "result.stage", "current_stage"]);
    const activityPhase = valueFrom(payload, ["activity.phase"]);
    const progress = valueFrom(payload, ["progress.percent", "progress.value", "percent", "progress"]);

    if (["PASS", "PASSED", "SUCCESS", "SUCCEEDED"].includes(status)) {
      showPass(payload);
      return true;
    }

    if (["NEEDS_REVIEW", "REVIEW", "REVIEW_REQUIRED"].includes(status)) {
      showReview(payload);
      return true;
    }

    if (["FAIL", "FAILED", "ERROR"].includes(status)) {
      showFailure(getPayloadMessage(payload));
      return true;
    }

    if (["CANCELLED", "CANCELED"].includes(status)) {
      returnToReady("Processing stopped. Your original PDF was not changed.");
      return true;
    }

    const normalized = normalizeStage(activityPhase || stage || status);
    updateProgress(normalized, progress);
    elements.processingMessage.textContent = getPayloadMessage(payload) || STAGE_MESSAGES[normalized];
    renderActivity(payload);
    return false;
  }

  function linkCounts(payload) {
    const source =
      valueFrom(payload, ["link_counts", "links", "summary.link_counts", "result.link_counts", "result.links"]) ||
      payload;
    const contents = finiteCount(valueFrom(source, ["contents", "toc", "internal", "toc_links", "internal_links"]));
    const web = finiteCount(valueFrom(source, ["web", "urls", "external", "web_links", "url_links"]));
    const email = finiteCount(valueFrom(source, ["email", "emails", "email_links", "mailto_links"]));
    const reportedTotal = finiteCount(valueFrom(source, ["total", "total_links"]), null);
    return {
      contents,
      web,
      email,
      total: reportedTotal === null ? contents + web + email : reportedTotal,
    };
  }

  function finiteCount(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? Math.round(number) : fallback;
  }

  function showPass(payload) {
    ++state.pollSequence;
    updateProgress("VERIFYING", 100);
    const counts = linkCounts(payload);
    elements.totalLinks.textContent = String(counts.total);
    elements.contentsLinks.textContent = String(counts.contents);
    elements.webLinks.textContent = String(counts.web);
    elements.downloadPassReportButton.hidden = !reportIsAvailable(payload, false);
    elements.passDownloadMessage.textContent = "";
    showView(elements.passView, true);
  }

  function reviewReasons(payload) {
    const source = valueFrom(payload, [
      "reasons",
      "review_reasons",
      "details.reasons",
      "result.reasons",
      "result.review_reasons",
    ]);
    const list = Array.isArray(source) ? source : source ? [source] : [];
    const reasons = list
      .map((item) => {
        if (typeof item === "string") {
          return item;
        }
        return item?.message || item?.reason || item?.description || "";
      })
      .filter(Boolean);

    if (reasons.length === 0) {
      const message = getPayloadMessage(payload);
      reasons.push(message || "Some page references could not be matched with enough confidence.");
    }

    return reasons;
  }

  function showReview(payload) {
    ++state.pollSequence;
    elements.reviewReasons.replaceChildren();
    reviewReasons(payload).forEach((reason) => {
      const item = document.createElement("li");
      item.textContent = reason;
      elements.reviewReasons.append(item);
    });
    elements.downloadReviewReportButton.hidden = !reportIsAvailable(payload, true);
    elements.reviewDownloadMessage.textContent = "";
    showView(elements.reviewView, true);
  }

  function showFailure(message) {
    if (state.appClosed) {
      return;
    }
    ++state.pollSequence;
    elements.failMessage.textContent =
      message || "The file may be damaged or use a feature this version does not support.";
    showView(elements.failView, true);
  }

  function returnToReady(message = "") {
    if (state.appClosed) {
      return;
    }
    ++state.pollSequence;
    state.jobId = null;
    state.lastPayload = null;
    setInlineMessage(elements.readyMessage, message);
    showView(elements.readyView, true);
  }

  function startOver() {
    const jobId = state.jobId;
    ++state.pollSequence;
    state.uploadController?.abort();
    state.uploadController = null;
    state.file = null;
    state.jobId = null;
    state.lastPayload = null;
    elements.pdfInput.value = "";
    elements.passDownloadMessage.textContent = "";
    elements.reviewDownloadMessage.textContent = "";
    setInlineMessage(elements.readyMessage, "");
    setInlineMessage(elements.fileError, "");
    showView(elements.selectView, true);
    void discardJob(jobId);
  }

  async function cancelJob() {
    const jobId = state.jobId;
    ++state.pollSequence;
    state.uploadController?.abort();
    state.uploadController = null;
    elements.cancelButton.disabled = true;
    elements.cancelButton.textContent = "Cancelling...";

    try {
      if (jobId) {
        const response = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
        if (!response.ok && response.status !== 404 && response.status !== 409) {
          const payload = await readResponse(response);
          throw new Error(getPayloadMessage(payload) || friendlyHttpError(response.status));
        }
      }
      returnToReady("Processing stopped. Your original PDF was not changed.");
    } catch (error) {
      if (!state.appClosed) {
        showFailure(`The app could not stop the job cleanly. ${friendlyError(error)}`);
      }
    } finally {
      if (!state.appClosed) {
        elements.cancelButton.disabled = false;
        elements.cancelButton.textContent = "Cancel";
      }
    }
  }

  async function downloadResult(kind, button, messageElement) {
    if (!state.jobId) {
      messageElement.textContent = "This download is no longer available. Please process the PDF again.";
      return;
    }

    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = "Preparing download...";
    messageElement.textContent = "";

    try {
      const response = await apiFetch(`/api/jobs/${encodeURIComponent(state.jobId)}/${kind}`);
      if (!response.ok) {
        const payload = await readResponse(response);
        throw new Error(getPayloadMessage(payload) || friendlyHttpError(response.status));
      }

      const blob = await response.blob();
      const fileName = fileNameFromResponse(response, kind);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = fileName;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      messageElement.textContent = "Download started.";
    } catch (error) {
      if (!state.appClosed) {
        messageElement.textContent = friendlyError(error);
      }
    } finally {
      if (!state.appClosed) {
        button.disabled = false;
        button.textContent = originalLabel;
      }
    }
  }

  function fileNameFromResponse(response, kind) {
    const disposition = response.headers.get("content-disposition") || "";
    const encodedMatch = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plainMatch = disposition.match(/filename="?([^";]+)"?/i);

    if (encodedMatch) {
      try {
        return decodeURIComponent(encodedMatch[1]);
      } catch (_error) {
        return encodedMatch[1];
      }
    }

    if (plainMatch) {
      return plainMatch[1];
    }

    const baseName = (state.file?.name || "document.pdf").replace(/\.pdf$/i, "");
    return kind === "pdf" ? `${baseName} - Interactive.pdf` : `${baseName} - Report.json`;
  }

  function friendlyHttpError(status) {
    if (status === 401 || status === 403) {
      return "This local session has expired. Close this tab and launch the app again.";
    }
    if (status === 413) {
      return "This PDF is larger than the app can process safely.";
    }
    if (status === 429) {
      return "The local processor is busy. Wait a moment, then try again.";
    }
    return "The local processor could not complete the request. Please try again.";
  }

  function friendlyError(error) {
    if (!error || !error.message || error instanceof TypeError) {
      return "The local processor is not responding. Close this tab, reopen the app, and try again.";
    }
    return error.message;
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  elements.pdfInput.addEventListener("change", (event) => chooseFile(event.target.files));

  elements.dropZone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (state.token) {
        elements.pdfInput.click();
      }
    }
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    elements.dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      if (state.token) {
        elements.dropZone.classList.add("is-dragging");
      }
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    elements.dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      elements.dropZone.classList.remove("is-dragging");
    });
  });

  elements.dropZone.addEventListener("drop", (event) => {
    if (state.token) {
      chooseFile(event.dataTransfer.files);
    }
  });

  elements.chooseAnotherButton.addEventListener("click", () => {
    elements.pdfInput.value = "";
    elements.pdfInput.click();
  });
  elements.startButton.addEventListener("click", startJob);
  elements.cancelButton.addEventListener("click", cancelJob);
  elements.retryButton.addEventListener("click", startJob);
  elements.passStartOverButton.addEventListener("click", startOver);
  elements.reviewStartOverButton.addEventListener("click", startOver);
  elements.failStartOverButton.addEventListener("click", startOver);
  elements.downloadPdfButton.addEventListener("click", () =>
    downloadResult("pdf", elements.downloadPdfButton, elements.passDownloadMessage),
  );
  elements.downloadPassReportButton.addEventListener("click", () =>
    downloadResult("report", elements.downloadPassReportButton, elements.passDownloadMessage),
  );
  elements.downloadReviewReportButton.addEventListener("click", () =>
    downloadResult("report", elements.downloadReviewReportButton, elements.reviewDownloadMessage),
  );

  initializeTheme();
  void loadAppVersion();

  if (!state.token) {
    elements.sessionNotice.hidden = false;
    elements.pdfInput.disabled = true;
    elements.dropZone.setAttribute("aria-disabled", "true");
  } else {
    void sendHeartbeat();
    state.heartbeatTimer = window.setInterval(() => void sendHeartbeat(), HEARTBEAT_DELAY_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !state.appClosed) {
        void sendHeartbeat();
      }
    });
  }
})();
