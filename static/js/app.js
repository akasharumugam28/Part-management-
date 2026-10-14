/* Part Manager — global behaviour (vanilla JS, no frameworks). */
(function () {
  "use strict";

  const IMAGE_TYPES = ["image/jpeg", "image/png", "image/webp"];
  const IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"];
  const OFFLINE_MESSAGE = "You are offline. Reconnect to save changes.";

  /* ------------------------------------------------------------- toasts */
  function dismissToast(toast) {
    if (!toast || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    setTimeout(() => toast.remove(), 220);
  }

  function armToast(toast) {
    toast.querySelector(".toast-close")?.addEventListener("click", () => dismissToast(toast));
    // Errors stay longer so they can be read; all toasts can be dismissed.
    setTimeout(() => dismissToast(toast), toast.classList.contains("toast-error") ? 8000 : 5000);
  }

  function toast(message, type = "info") {
    const container = document.getElementById("toasts");
    if (!container) return;
    const duplicate = Array.from(container.querySelectorAll(".toast:not(.is-leaving) .toast-text"))
      .some((t) => t.textContent === message);
    if (duplicate) return;
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    const icon = type === "error" ? "alert" : type === "success" ? "check" : "info";
    el.innerHTML =
      `<svg class="icon" aria-hidden="true"><use href="#i-${icon}"></use></svg>` +
      '<span class="toast-text"></span><button type="button" class="toast-close" aria-label="Dismiss">&times;</button>';
    el.querySelector(".toast-text").textContent = message;
    container.appendChild(el);
    armToast(el);
    // Keep the stack short; drop the oldest.
    const all = container.querySelectorAll(".toast:not(.is-leaving)");
    for (let i = 0; i < all.length - 3; i += 1) dismissToast(all[i]);
  }

  /* ----------------------------------------------------- loading states */
  function setLoading(button, loading) {
    if (!button) return;
    button.classList.toggle("is-loading", loading);
    button.disabled = loading;
    button.setAttribute("aria-busy", loading ? "true" : "false");
  }

  function resetForms() {
    document.querySelectorAll("form[data-submitting]").forEach((form) => {
      delete form.dataset.submitting;
      form.querySelectorAll(".is-loading").forEach((b) => setLoading(b, false));
    });
  }

  /* ----------------------------------------------------- confirm dialog */
  function confirmAction(message, okLabel = "Delete") {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(message));
    }
    dialog.querySelector("#confirm-message").textContent = message;
    dialog.querySelector("#confirm-ok").textContent = okLabel;
    dialog.returnValue = "";
    dialog.showModal();
    dialog.querySelector('button[value="cancel"]').focus();
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    });
  }

  /* --------------------------------------------------- online / offline */
  function isOnline() {
    return navigator.onLine !== false;
  }

  function networkControls() {
    return document.querySelectorAll(
      '[data-requires-network], form[method="post" i] button[type="submit"]'
    );
  }

  function applyNetworkState() {
    const online = isOnline();
    const banner = document.getElementById("offline-banner");
    if (banner) banner.hidden = online;
    networkControls().forEach((el) => {
      if (!online) {
        if (!el.disabled) {
          el.disabled = true;
          el.dataset.offlineDisabled = "1";
          el.title = OFFLINE_MESSAGE;
        }
      } else if (el.dataset.offlineDisabled) {
        el.disabled = false;
        delete el.dataset.offlineDisabled;
        el.removeAttribute("title");
      }
    });
  }

  window.addEventListener("online", () => {
    applyNetworkState();
    toast("Back online.", "success");
  });
  // The sticky offline banner carries the message; no toast needed.
  window.addEventListener("offline", applyNetworkState);

  /* -------------------------------------------------------- form submit */
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;

    if (!isOnline()) {
      event.preventDefault();
      toast(OFFLINE_MESSAGE, "error");
      return;
    }

    if (form.dataset.confirm && !form.dataset.confirmed) {
      event.preventDefault();
      confirmAction(form.dataset.confirm).then((ok) => {
        if (!ok) return;
        form.dataset.confirmed = "1";
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.submit();
      });
      return;
    }
    delete form.dataset.confirmed;

    if (form.hasAttribute("data-loading")) {
      if (form.dataset.submitting) {
        event.preventDefault(); // duplicate submission
        return;
      }
      form.dataset.submitting = "1";
      const button = event.submitter || form.querySelector('button[type="submit"]');
      // Defer so the browser has already captured the form data.
      setTimeout(() => setLoading(button, true), 0);
    }
  });

  // Restore buttons when the page is shown from the back/forward cache.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) resetForms();
    applyNetworkState();
  });

  /* ------------------------------------------------------ image preview */
  function validateImage(file) {
    if (!file) return null;
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (!IMAGE_EXTENSIONS.includes(ext) || (file.type && !IMAGE_TYPES.includes(file.type))) {
      return "Invalid image format.";
    }
    const max = Number(document.body.dataset.maxImageBytes || 0);
    if (max && file.size > max) {
      return `Image is too large. The maximum size is ${Math.round(max / 1048576)} MB.`;
    }
    return null;
  }

  function setPreview(img, url) {
    if (!img) return;
    if (img.dataset.objectUrl) URL.revokeObjectURL(img.dataset.objectUrl);
    delete img.dataset.objectUrl;
    if (url && url.startsWith("blob:")) img.dataset.objectUrl = url;
    img.src = url || img.dataset.original || img.dataset.placeholder;
  }

  function clearFileInput(input) {
    input.value = "";
    const img = document.querySelector(input.dataset.preview);
    setPreview(img, null);
    const clearBtn = document.querySelector(`[data-clear-file="#${input.id}"]`);
    if (clearBtn) clearBtn.hidden = true;
    input.dispatchEvent(new CustomEvent("tm:file-cleared", { bubbles: true }));
  }

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (input.matches?.('input[type="file"][data-preview]')) {
      const file = input.files && input.files[0];
      if (!file) return;
      const error = validateImage(file);
      if (error) {
        toast(error, "error");
        clearFileInput(input);
        event.stopImmediatePropagation();
        return;
      }
      const url = URL.createObjectURL(file);
      // Make sure the browser can actually decode it (catches renamed files).
      const probe = new Image();
      probe.src = url;
      probe.decode().then(
        () => {
          if (input.files[0] !== file) return URL.revokeObjectURL(url);
          const img = document.querySelector(input.dataset.preview);
          setPreview(img, url);
          img?.classList.remove("is-removed");
          const clearBtn = document.querySelector(`[data-clear-file="#${input.id}"]`);
          if (clearBtn) clearBtn.hidden = false;
          const remove = input.closest(".image-field")?.querySelector("[data-remove-image]");
          if (remove) remove.checked = false;
        },
        () => {
          URL.revokeObjectURL(url);
          if (input.files[0] !== file) return;
          toast("Invalid image format.", "error");
          clearFileInput(input);
        }
      );
    } else if (input.matches?.("[data-remove-image]")) {
      const img = document.querySelector(input.dataset.removeImage);
      const fileInput = input.closest(".image-field")?.querySelector('input[type="file"]');
      if (input.checked && fileInput?.files.length) clearFileInput(fileInput);
      if (img) {
        img.classList.toggle("is-removed", input.checked);
        img.src = input.checked ? img.dataset.placeholder : img.dataset.original;
      }
    }
  });

  document.addEventListener("click", (event) => {
    const clearBtn = event.target.closest?.("[data-clear-file]");
    if (clearBtn) {
      const input = document.querySelector(clearBtn.dataset.clearFile);
      if (input) clearFileInput(input);
    }
  });

  /* ---------------------------------------------------- simple dialogs */
  document.addEventListener("click", (event) => {
    const opener = event.target.closest?.("[data-open-dialog]");
    if (opener) {
      const dialog = document.querySelector(opener.dataset.openDialog);
      if (dialog?.showModal) {
        const form = dialog.querySelector("form");
        if (form && opener.dataset.formAction) form.action = opener.dataset.formAction;
        const name = dialog.querySelector("input[type=text]");
        if (name && opener.dataset.fileName !== undefined) name.value = opener.dataset.fileName;
        dialog.showModal();
        dialog.querySelector("input[type=text]")?.select();
      }
      return;
    }
    if (event.target.closest?.("[data-close-dialog]")) event.target.closest("dialog")?.close();
  });

  // Downloads don't leave the page, so just close the dialog once submitted.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.id !== "download-form") return;
    const name = form.elements.name;
    name.value = name.value.trim();
    if (!name.value) {
      event.preventDefault();
      toast("Please enter a file name.", "error");
      name.focus();
      return;
    }
    if (!isOnline()) {
      event.preventDefault();
      toast("You are offline. Reconnect to download.", "error");
      return;
    }
    setTimeout(() => {
      form.closest("dialog")?.close();
      toast("Download started.", "success");
    }, 0);
  });

  /* ------------------------------------------------ trolley qty presets */
  function syncQtyChips(input) {
    input.closest(".qty-picker")?.querySelectorAll("[data-qty-preset]").forEach((chip) => {
      chip.setAttribute("aria-pressed", String(chip.dataset.qtyPreset === input.value.trim()));
    });
  }

  document.addEventListener("click", (event) => {
    const chip = event.target.closest?.("[data-qty-preset]");
    if (!chip) return;
    const input = chip.closest(".qty-picker").querySelector("input");
    input.value = chip.dataset.qtyPreset;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches?.(".qty-picker input")) syncQtyChips(event.target);
  });

  /* -------------------------------------------------------- live search */
  function liveFilter(input) {
    const container = document.querySelector(input.dataset.liveSearch);
    if (!container) return;
    const term = input.value.trim().toLowerCase();
    let shown = 0;
    container.querySelectorAll(":scope > [data-search]").forEach((item) => {
      const match = !term || item.dataset.search.includes(term);
      item.hidden = !match;
      if (match) shown += 1;
    });
    const empty = container.dataset.empty && document.querySelector(container.dataset.empty);
    if (empty) empty.hidden = shown > 0;
  }

  document.addEventListener("input", (event) => {
    const input = event.target;
    if (input.matches?.("[data-live-search]")) liveFilter(input);
    // Clear a field's error message as soon as the user corrects it.
    const field = input.closest?.(".field.has-error");
    if (field && input.value.trim()) {
      field.classList.remove("has-error");
      input.removeAttribute("aria-invalid");
      const err = field.querySelector(".field-error");
      if (err) err.hidden = true;
    }
  });

  /* ------------------------------------------------- PWA: install + SW */
  let deferredInstall = null;
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    deferredInstall = event;
    const btn = document.getElementById("install-button");
    if (btn) btn.hidden = false;
  });
  window.addEventListener("appinstalled", () => {
    deferredInstall = null;
    const btn = document.getElementById("install-button");
    if (btn) btn.hidden = true;
    toast("Part Manager installed.", "success");
  });

  let reloadRequested = false;

  function showUpdateBanner(worker) {
    const banner = document.getElementById("update-banner");
    if (!banner) return;
    banner.hidden = false;
    document.getElementById("update-reload").onclick = () => {
      reloadRequested = true;
      worker.postMessage({ type: "SKIP_WAITING" });
    };
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    const url = document.body.dataset.swUrl || "/service-worker.js";
    navigator.serviceWorker
      .register(url, { scope: "/" })
      .then((reg) => {
        if (reg.waiting && navigator.serviceWorker.controller) showUpdateBanner(reg.waiting);
        reg.addEventListener("updatefound", () => {
          const worker = reg.installing;
          if (!worker) return;
          worker.addEventListener("statechange", () => {
            if (worker.state === "installed" && navigator.serviceWorker.controller) {
              showUpdateBanner(worker);
            }
          });
        });
        // Check for a new version hourly while the app stays open.
        setInterval(() => reg.update().catch(() => {}), 60 * 60 * 1000);
      })
      .catch((err) => console.warn("Service worker registration failed:", err));

    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (reloadRequested) window.location.reload();
    });
  }

  /* --------------------------------------------------------------- init */
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#toasts .toast").forEach(armToast);
    document.querySelectorAll(".qty-picker input").forEach(syncQtyChips);
    document.querySelectorAll("[data-live-search]").forEach((input) => {
      if (input.value) liveFilter(input);
    });
    const installBtn = document.getElementById("install-button");
    installBtn?.addEventListener("click", async () => {
      if (!deferredInstall) return;
      deferredInstall.prompt();
      await deferredInstall.userChoice;
      deferredInstall = null;
      installBtn.hidden = true;
    });
    const retry = document.getElementById("retry-link");
    retry?.addEventListener("click", (e) => {
      e.preventDefault();
      window.location.reload();
    });
    applyNetworkState();
    registerServiceWorker();
  });

  window.TM = { toast, setLoading, confirmAction, isOnline, validateImage, setPreview, clearFileInput, OFFLINE_MESSAGE };
})();
