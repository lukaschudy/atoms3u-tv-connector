(() => {
  "use strict";

  const ACCEPT_LABELS = new Set([
    "accept all",
    "accept all cookies",
    "i agree",
    "agree",
    "p\u0159ijmout v\u0161e",
    "souhlas\u00edm",
    "alle akzeptieren"
  ]);

  const normalize = (value) =>
    String(value || "")
      .normalize("NFKC")
      .replace(/\s+/g, " ")
      .trim()
      .toLocaleLowerCase();

  const roots = () => {
    const found = [document];
    for (let index = 0; index < found.length; index += 1) {
      for (const element of found[index].querySelectorAll("*")) {
        if (element.shadowRoot) {
          found.push(element.shadowRoot);
        }
      }
    }
    return found;
  };

  const acceptConsent = () => {
    for (const root of roots()) {
      const controls = root.querySelectorAll(
        "button, [role='button'], input[type='button'], input[type='submit']"
      );
      for (const control of controls) {
        const label = normalize(
          control.innerText ||
            control.value ||
            control.getAttribute("aria-label") ||
            control.textContent
        );
        if (ACCEPT_LABELS.has(label)) {
          control.click();
          return true;
        }
      }
    }
    return false;
  };

  const openFirstGoogleResult = () => {
    if (
      window.top !== window ||
      window.location.hostname !== "www.google.com" ||
      window.location.pathname !== "/search" ||
      window.location.hash !== "#room-agent-open-first"
    ) {
      return false;
    }

    const resultLinks = document.querySelectorAll("#search a[href]");
    for (const link of resultLinks) {
      if (!link.querySelector("h3") || link.closest("#tads, #taw")) {
        continue;
      }
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search
      );
      link.click();
      return true;
    }
    return false;
  };

  let playbackStarted = false;
  let observer;
  let timer;

  const stop = () => {
    if (observer) {
      observer.disconnect();
    }
    if (timer) {
      clearInterval(timer);
    }
  };

  const tick = () => {
    if (acceptConsent()) {
      return;
    }

    if (openFirstGoogleResult()) {
      stop();
      return;
    }

    const video = document.querySelector("video");
    if (!video || playbackStarted) {
      return;
    }
    if (!video.paused) {
      playbackStarted = true;
      stop();
      return;
    }

    video.play().then(() => {
      playbackStarted = true;
      stop();
    }).catch(() => {
      // The kiosk launcher also retries through Chromium's native media API.
    });
  };

  observer = new MutationObserver(tick);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  timer = setInterval(tick, 500);
  setTimeout(stop, 30000);
  tick();
})();
