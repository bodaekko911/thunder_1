/**
 * theme-init.js
 *
 * Pre-paint theme shim. Must be loaded as the very first <script> in <head>,
 * before any stylesheet, on every authenticated page. Reads localStorage
 * synchronously and sets the data-theme attribute and the "light" class on
 * <html> before the browser paints, eliminating theme-flash (FOUC).
 *
 * The richer theme.js / auth-guard.js logic continues to handle toggle and
 * cross-tab sync after parse — this file just gets the colors right at t=0.
 */
(function () {
  try {
    var stored = localStorage.getItem("colorMode");
    // Legacy keys older builds may still have:
    if (!stored) {
      var legacy = ["dashboard:theme", "expenses-theme", "refund-theme"];
      for (var i = 0; i < legacy.length; i += 1) {
        var v = localStorage.getItem(legacy[i]);
        if (v) { stored = v; break; }
      }
    }
    var theme = stored === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    if (theme === "light") {
      document.documentElement.classList.add("light");
    }
  } catch (_) {
    document.documentElement.setAttribute("data-theme", "dark");
  }
})();
