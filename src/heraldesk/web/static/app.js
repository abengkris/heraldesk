(function () {
  "use strict";

  var root = document.documentElement;
  var storageKey = "heraldesk-theme";

  try {
    var saved = window.localStorage.getItem(storageKey);
    if (saved === "light" || saved === "dark") {
      root.dataset.theme = saved;
    }
  } catch (error) {
    /* localStorage can be unavailable in private modes; the OS theme remains. */
  }

  var button = document.getElementById("theme-toggle");
  if (!button) {
    return;
  }

  function currentTheme() {
    if (root.dataset.theme === "light" || root.dataset.theme === "dark") {
      return root.dataset.theme;
    }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function updateLabel() {
    var next = currentTheme() === "dark" ? "light" : "dark";
    button.setAttribute("aria-label", "Switch to " + next + " mode");
    button.textContent = currentTheme() === "dark" ? "Light" : "Dark";
  }

  button.addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try {
      window.localStorage.setItem(storageKey, next);
    } catch (error) {
      /* Ignore storage failures; the choice still applies for this page. */
    }
    updateLabel();
  });

  updateLabel();
})();
