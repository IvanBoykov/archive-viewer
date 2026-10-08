/*
 * Accessible modal video player for the hourly clip list.
 *
 * Scope rules:
 *   - Directory navigation is server-rendered; this file only runs on an hour page.
 *   - Intra-hour clip data comes from <script type="application/json" id="hour-clips-data">.
 *   - Cross-boundary targets come from #hour-nav-data, computed by the backend.
 *   - The video element is never muted. If the browser refuses to autoplay with
 *     sound, playback stays paused and the user presses Play. Audio wins.
 */
(function () {
  "use strict";

  var CLIPS_ID = "hour-clips-data";
  var NAV_ID = "hour-nav-data";
  var MODAL_ID = "player-modal";
  var LIVE_ID = "live-region";
  var HASH_FIRST = "#auto-first";
  var HASH_LAST = "#auto-last";
  /* Remembers where a cross-boundary jump started, so the destination page can
     name the kind of boundary it crossed. */
  var ORIGIN_KEY = "archive:last-hour";

  var clips = readJson(CLIPS_ID, []);
  var nav = readJson(NAV_ID, {});
  var modal = document.getElementById(MODAL_ID);
  var live = document.getElementById(LIVE_ID);

  if (!modal || !Array.isArray(clips) || clips.length === 0) {
    return;
  }

  var video = modal.querySelector("[data-player]");
  var closeButton = modal.querySelector("[data-close]");
  var backdrop = modal.querySelector("[data-close-backdrop]");
  var prevButton = modal.querySelector("[data-prev]");
  var nextButton = modal.querySelector("[data-next]");
  var autoAdvance = modal.querySelector("[data-auto-advance]");
  var nowPlaying = modal.querySelector("[data-now-playing]");
  var autoplayNotice = modal.querySelector("[data-autoplay-notice]");

  var FOCUSABLE =
    'a[href], button:not([disabled]), video[controls], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

  var isOpen = false;
  var index = -1;
  var returnFocusTo = null;

  // -- helpers ----------------------------------------------------------

  function readJson(id, fallback) {
    var node = document.getElementById(id);
    if (!node) {
      return fallback;
    }
    try {
      return JSON.parse(node.textContent || "null") || fallback;
    } catch (error) {
      return fallback;
    }
  }

  /* Announce through the visually hidden live region only. The visible heading
     never carries aria-live, so it is not read on every keystroke. */
  function announce(message) {
    if (!live) {
      return;
    }
    // Clearing first makes repeated identical messages announce again.
    live.textContent = "";
    window.setTimeout(function () {
      live.textContent = message;
    }, 60);
  }

  function describeClip(clip) {
    if (!clip) {
      return "No clip selected";
    }
    var parts = ["Now playing clip " + (clip.absolute_time || clip.time)];
    if (nav.camera_label) {
      parts.push(nav.camera_label);
    }
    if (nav.date_label) {
      parts.push(nav.date_label);
    }
    if (clip.truncated) {
      parts.push("this recording is incomplete");
    }
    return parts.join(", ") + ".";
  }

  /* A message about how the user arrived at this hour, based on the hour that
     started the cross-boundary navigation. */
  function describeBoundary(origin) {
    if (!origin || !nav.date) {
      return "Transitioning to the next available hour.";
    }
    if (origin.date !== nav.date) {
      var days = Math.abs(dateOrdinal(nav.date) - dateOrdinal(origin.date));
      if (days > 31) {
        return "Transitioning to a different month.";
      }
      return "Transitioning to " + (days === 1 ? "the next day" : "another day") + ".";
    }
    return "Transitioning to " + (Number(nav.hour.slice(0, 2)) >= Number(origin.hour.slice(0, 2))
      ? "a later hour"
      : "an earlier hour") + ".";
  }

  function dateOrdinal(iso) {
    var parts = String(iso).split("-").map(Number);
    return Date.UTC(parts[0], (parts[1] || 1) - 1, parts[2] || 1) / 86400000;
  }

  function rememberOrigin() {
    if (!nav.date) {
      return;
    }
    try {
      sessionStorage.setItem(ORIGIN_KEY, JSON.stringify({ date: nav.date, hour: nav.hour }));
    } catch (error) {
      /* Storage disabled (private mode): boundary wording stays generic. */
    }
  }

  function takeOrigin() {
    try {
      var raw = sessionStorage.getItem(ORIGIN_KEY);
      sessionStorage.removeItem(ORIGIN_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  // -- modal open / close ----------------------------------------------

  function openModal(startIndex, announcement) {
    returnFocusTo = document.activeElement;
    isOpen = true;
    modal.hidden = false;
    document.documentElement.classList.add("has-modal");
    selectClip(startIndex, false);
    document.addEventListener("keydown", onKeydown, true);
    video.focus();
    announce(announcement || describeClip(clips[index]));
  }

  function closeModal() {
    if (!isOpen) {
      return;
    }
    isOpen = false;
    // Stop the download of any further video bytes before hiding the player.
    video.pause();
    video.removeAttribute("src");
    video.load();
    modal.hidden = true;
    document.documentElement.classList.remove("has-modal");
    document.removeEventListener("keydown", onKeydown, true);
    if (autoplayNotice) {
      autoplayNotice.hidden = true;
    }
    announce("Player closed.");
    if (returnFocusTo && typeof returnFocusTo.focus === "function") {
      returnFocusTo.focus();
    }
    returnFocusTo = null;
  }

  /* Keep Tab inside the dialog: wrap at both ends of the focusable set. */
  function trapFocus(event) {
    var focusables = Array.prototype.filter.call(
      modal.querySelectorAll(FOCUSABLE),
      function (element) {
        return element.offsetParent !== null || element === document.activeElement;
      }
    );
    if (focusables.length === 0) {
      event.preventDefault();
      video.focus();
      return;
    }
    var first = focusables[0];
    var last = focusables[focusables.length - 1];
    var active = document.activeElement;
    var inside = modal.contains(active);

    if (event.shiftKey) {
      if (!inside || active === first) {
        event.preventDefault();
        last.focus();
      }
    } else if (!inside || active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function onKeydown(event) {
    if (!isOpen) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeModal();
    } else if (event.key === "Tab") {
      trapFocus(event);
    }
  }

  // -- clip loading ------------------------------------------------------

  function selectClip(nextIndex, shouldAnnounce) {
    var clip = clips[nextIndex];
    if (!clip) {
      return;
    }
    index = nextIndex;
    if (nowPlaying) {
      nowPlaying.textContent = describeClip(clip);
    }
    if (clip.truncated) {
      video.setAttribute("data-truncated", "true");
    } else {
      video.removeAttribute("data-truncated");
    }
    video.src = clip.video_url;
    video.load();
    play();
    prevButton.disabled = false;
    nextButton.disabled = false;
    updateControlLabels();
    if (shouldAnnounce) {
      announce(describeClip(clip));
    }
  }

  /* Autoplay with sound. A rejected promise means the browser wants a user
     gesture: leave the video paused rather than muting it. */
  function play() {
    if (autoplayNotice) {
      autoplayNotice.hidden = true;
    }
    var attempt = video.play();
    if (attempt && typeof attempt.catch === "function") {
      attempt.catch(function () {
        video.pause();
        if (autoplayNotice) {
          autoplayNotice.hidden = false;
        }
        announce("Playback needs your permission. Press Play to start with sound.");
      });
    }
  }

  function updateControlLabels() {
    var atStart = index <= 0;
    var atEnd = index >= clips.length - 1;
    var previousUrl = atStart ? nav.previous_autolast : null;
    var nextUrl = atEnd ? nav.next_autofirst : null;
    prevButton.textContent = atStart
      ? previousUrl ? "Previous hour" : "Previous hour (none)"
      : "Previous clip";
    nextButton.textContent = atEnd
      ? nextUrl ? "Next hour" : "Next hour (none)"
      : "Next clip";
    prevButton.disabled = atStart && !previousUrl;
    nextButton.disabled = atEnd && !nextUrl;
  }

  /* Move one clip back/forward. At the edges of the hour, hand over to the
     backend's cross-boundary URL so the next page auto-opens its last/first
     clip. */
  function step(delta) {
    var target = index + delta;
    if (target >= 0 && target < clips.length) {
      selectClip(target, true);
      return;
    }
    var url = delta > 0 ? nav.next_autofirst : nav.previous_autolast;
    if (!url) {
      announce(delta > 0 ? "This is the last clip of the archive."
                         : "This is the first clip of the archive.");
      return;
    }
    rememberOrigin();
    window.location.assign(url);
  }

  // -- wiring ------------------------------------------------------------

  Array.prototype.forEach.call(document.querySelectorAll("[data-clip-index]"), function (button) {
    button.addEventListener("click", function () {
      var startIndex = parseInt(button.getAttribute("data-clip-index"), 10);
      openModal(isNaN(startIndex) ? 0 : startIndex);
    });
  });

  closeButton.addEventListener("click", closeModal);
  prevButton.addEventListener("click", function () {
    step(-1);
  });
  nextButton.addEventListener("click", function () {
    step(1);
  });
  if (backdrop) {
    backdrop.addEventListener("click", closeModal);
  }

  video.addEventListener("ended", function () {
    if (autoAdvance && autoAdvance.checked) {
      step(1);
    }
  });

  video.addEventListener("error", function () {
    if (isOpen && video.getAttribute("src")) {
      announce("This clip could not be loaded.");
    }
  });

  // -- hash routing ------------------------------------------------------

  /* #auto-first / #auto-last open the first or last clip of the hour, used when
     the previous page crossed an hour, day, month or year boundary. */
  function handleHash() {
    var hash = window.location.hash;
    if (hash !== HASH_FIRST && hash !== HASH_LAST) {
      return;
    }
    var origin = takeOrigin();
    var startIndex = hash === HASH_FIRST ? 0 : clips.length - 1;
    var boundary = describeBoundary(origin);
    openModal(startIndex);
    // Two sentences: the boundary crossed, then what is playing now.
    announce(boundary + " " + describeClip(clips[startIndex]));
    /* Drop the fragment so a manual reload of the same URL does not silently
       reopen the dialog before the user asks for it. */
    if (window.history && window.history.replaceState) {
      window.history.replaceState("", document.title, window.location.pathname + window.location.search);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", handleHash);
  } else {
    handleHash();
  }
})();
