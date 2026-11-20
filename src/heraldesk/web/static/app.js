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

  var themeButton = document.getElementById("theme-toggle");
  if (themeButton) {
    function currentTheme() {
      if (root.dataset.theme === "light" || root.dataset.theme === "dark") {
        return root.dataset.theme;
      }
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
    }

    function updateThemeLabel() {
      var next = currentTheme() === "dark" ? "light" : "dark";
      themeButton.setAttribute("aria-label", "Switch to " + next + " mode");
      themeButton.textContent = currentTheme() === "dark" ? "Light" : "Dark";
    }

    themeButton.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        window.localStorage.setItem(storageKey, next);
      } catch (error) {
        /* Ignore storage failures; the choice still applies for this page. */
      }
      updateThemeLabel();
    });

    updateThemeLabel();
  }

  /* Toast dismissal (works for toasts added later via htmx). */
  document.addEventListener("click", function (event) {
    var close = event.target.closest(".toast-close");
    if (close) {
      var toast = close.closest(".toast");
      if (toast) {
        toast.remove();
      }
    }
  });

  /* Live character counters for editable fields with data-counter. */
  document.addEventListener("input", function (event) {
    var field = event.target.closest("[data-counter]");
    if (!field) {
      return;
    }
    var counter = document.querySelector(field.getAttribute("data-counter"));
    if (counter) {
      counter.textContent = field.value.length + " chars";
    }
  });

  /* Keyboard shortcuts, only on the queue pages and only outside form fields. */
  var shortcutsPanel = document.getElementById("shortcuts");
  var onQueue = document.body && document.body.getAttribute("data-shortcuts") === "queue";

  function editing() {
    var active = document.activeElement;
    if (!active) {
      return false;
    }
    var tag = active.tagName;
    return (
      tag === "INPUT" ||
      tag === "TEXTAREA" ||
      tag === "SELECT" ||
      (active.isContentEditable === true)
    );
  }

  function focusRow(direction) {
    var rows = Array.prototype.slice.call(document.querySelectorAll(".queue-row-link"));
    if (!rows.length) {
      return;
    }
    var current = document.activeElement && document.activeElement.closest(".queue-row-link");
    var index = current ? rows.indexOf(current) : -1;
    var next = direction === "next" ? (index + 1) % rows.length : (index - 1 + rows.length) % rows.length;
    rows[next].focus();
    rows[next].click();
  }

  if (onQueue) {
    document.addEventListener("keydown", function (event) {
      if (editing() || event.altKey || event.ctrlKey || event.metaKey) {
        return;
      }
      switch (event.key) {
        case "j":
          focusRow("next");
          event.preventDefault();
          break;
        case "k":
          focusRow("prev");
          event.preventDefault();
          break;
        case "a": {
          var approve = document.querySelector("#queue-detail .button.primary:not(:disabled)");
          if (approve) {
            approve.focus();
            event.preventDefault();
          }
          break;
        }
        case "r": {
          var reason = document.querySelector('#queue-detail input[name="reason"]');
          if (reason) {
            reason.focus();
            event.preventDefault();
          }
          break;
        }
        case "e": {
          var edit = document.querySelector("#queue-detail #edit-body");
          if (edit) {
            edit.focus();
            event.preventDefault();
          }
          break;
        }
        case "?":
          if (shortcutsPanel) {
            shortcutsPanel.hidden = !shortcutsPanel.hidden;
            event.preventDefault();
          }
          break;
      }
    });
  }
})();
