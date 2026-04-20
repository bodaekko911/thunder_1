(function () {
  if (window.__appTheme) return;

  const THEME_KEY = "colorMode";
  const LEGACY_KEYS = ["dashboard:theme", "expenses-theme", "refund-theme"];
  const LIGHT = "light";
  const DARK = "dark";

  function normalizeTheme(theme) {
    return theme === LIGHT ? LIGHT : DARK;
  }

  function readStoredTheme() {
    try {
      const stored = localStorage.getItem(THEME_KEY);
      if (stored) return normalizeTheme(stored);

      for (const key of LEGACY_KEYS) {
        const legacy = localStorage.getItem(key);
        if (legacy) return normalizeTheme(legacy);
      }
    } catch (_) {}

    const rootTheme = document.documentElement.getAttribute("data-theme");
    if (rootTheme) return normalizeTheme(rootTheme);
    return document.body && document.body.classList.contains(LIGHT) ? LIGHT : DARK;
  }

  function writeStoredTheme(theme) {
    try {
      localStorage.setItem(THEME_KEY, theme);
      for (const key of LEGACY_KEYS) {
        localStorage.removeItem(key);
      }
    } catch (_) {}
  }

  function updateButtons(theme) {
    const label = theme === LIGHT ? "&#9728;&#65039;" : "&#127769;";
    document.querySelectorAll("#mode-btn").forEach((button) => {
      button.innerHTML = label;
    });
  }

  function applyTheme(theme, options) {
    const settings = Object.assign({ persist: true, dispatch: true }, options);
    const nextTheme = normalizeTheme(theme);

    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.setAttribute("data-theme", nextTheme);
    if (document.body) {
      document.body.classList.toggle(LIGHT, nextTheme === LIGHT);
    }

    updateButtons(nextTheme);

    if (settings.persist) {
      writeStoredTheme(nextTheme);
    }

    if (settings.dispatch) {
      window.dispatchEvent(new CustomEvent("app:themechange", { detail: { theme: nextTheme } }));
    }

    return nextTheme;
  }

  function ensureTheme(options) {
    return applyTheme(readStoredTheme(), options);
  }

  window.__appTheme = {
    get() {
      return normalizeTheme(document.documentElement.dataset.theme || readStoredTheme());
    },
    set(theme) {
      return applyTheme(theme);
    },
    toggle() {
      return applyTheme(this.get() === LIGHT ? DARK : LIGHT);
    },
    sync() {
      return ensureTheme({ persist: false, dispatch: false });
    },
    key: THEME_KEY,
  };

  ensureTheme({ dispatch: false });

  document.addEventListener("DOMContentLoaded", () => {
    ensureTheme({ persist: false, dispatch: false });
  });

  window.addEventListener("storage", (event) => {
    if (![THEME_KEY, ...LEGACY_KEYS].includes(event.key || "")) return;
    ensureTheme({ persist: true, dispatch: true });
  });
})();
