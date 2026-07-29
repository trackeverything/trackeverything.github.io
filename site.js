/* TrackEverything project page: sequence selector, lazy video playback,
   sliding clip carousel, lightbox. */

const prefersReducedMotion = () =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

const scrollMode = () => (prefersReducedMotion() ? "auto" : "smooth");

/* ---------------------------------------------------------------- viewer --- */

const setupSequenceViewer = () => {
  const list = document.getElementById("sequenceList");
  const video = document.getElementById("viewerVideo");
  if (!list || !video) return;

  list.addEventListener("click", (event) => {
    const button = event.target.closest(".sequence-item");
    if (!button) return;

    const src = button.dataset.video;
    if (!src || video.getAttribute("src") === src) return;

    list.querySelectorAll(".sequence-item").forEach((item) => item.classList.remove("is-active"));
    button.classList.add("is-active");

    video.src = src;
    video.load();
    video.play().catch(() => {});
  });
};

/* ------------------------------------------------------- lazy playback ----- */

/* Low-FPS qualitative clips play faster so demos feel snappier.
   DAVIS / PStudio / POD are ~12 fps → 2x (~24 fps effective).
   MeViS is ~4 fps → 4x (~16 fps effective). */
const applyPlaybackRate = (video) => {
  if (!video) return;
  const src = video.getAttribute("src") || video.dataset.src || "";
  const isQual =
    src.includes("/good_cases/") ||
    src.includes("/failure_cases/") ||
    src.includes("/static_dynamic/");
  if (!isQual) {
    video.playbackRate = 1;
    return;
  }
  video.playbackRate = /mevis_/i.test(src) ? 4 : 2;
};

const ensureSource = (video) => {
  if (video && !video.getAttribute("src") && video.dataset.src) {
    video.setAttribute("src", video.dataset.src);
  }
  applyPlaybackRate(video);
};

const releaseSource = (video) => {
  if (!video || video.dataset.unload !== "true" || !video.getAttribute("src")) return;
  video.pause();
  video.removeAttribute("src");
  video.load();
};

/* Grid clips load and play on viewport intersection. Carousel clips are
   excluded; the rail manages its own loading window. */
const setupVideoPlayback = () => {
  const videos = [...document.querySelectorAll("video[data-autoplay]")].filter(
    (video) => !video.closest("[data-rail]"),
  );
  if (!videos.length) return;

  if (!("IntersectionObserver" in window)) {
    videos.forEach((video) => {
      ensureSource(video);
      video.play().catch(() => {});
    });
    return;
  }

  const loadObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) =>
        entry.isIntersecting ? ensureSource(entry.target) : releaseSource(entry.target),
      );
    },
    { rootMargin: "400px 0px" },
  );

  const playObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          ensureSource(entry.target);
          entry.target.play().catch(() => {});
        } else {
          entry.target.pause();
        }
      });
    },
    { threshold: 0.15 },
  );

  videos.forEach((video) => {
    loadObserver.observe(video);
    playObserver.observe(video);
  });
};

/* ------------------------------------------------------- clip carousel ----- */

/* Horizontal snap-scrolled strip. Only clips near the rail viewport hold a
   source. rootMargin cannot expand past the rail's own clipping box, so the
   window is measured from client rects instead of an observer. */
const setupClipRails = () => {
  document.querySelectorAll("[data-carousel]").forEach((carousel) => {
    const shell = carousel.querySelector(".rail-shell");
    const rail = carousel.querySelector("[data-rail]");
    const prev = carousel.querySelector("[data-rail-prev]");
    const next = carousel.querySelector("[data-rail-next]");
    const dotsHost = carousel.querySelector("[data-rail-dots]");
    const countHost = carousel.querySelector("[data-rail-count]");
    if (!rail) return;

    const clips = [...rail.querySelectorAll(".clip")];
    if (!clips.length) return;

    let dots = [];
    let pages = 1;
    let onScreen = true;

    const page = () => {
      const view = rail.clientWidth;
      return view ? Math.min(pages - 1, Math.round(rail.scrollLeft / view)) : 0;
    };

    const update = () => {
      const view = rail.clientWidth;
      if (!view) return;

      const railLeft = rail.getBoundingClientRect().left;
      let first = -1;
      let last = -1;

      clips.forEach((clip, i) => {
        const video = clip.querySelector("video");
        if (!video) return;

        const rect = clip.getBoundingClientRect();
        const start = rect.left - railLeft;
        const end = rect.right - railLeft;
        const visible = end > 4 && start < view - 4;

        if (visible) {
          if (first < 0) first = i;
          last = i;
        }

        /* Load one page either side, drop past two: the gap between the two
           thresholds keeps a slow scroll from thrashing load/unload. */
        if (end > -view && start < 2 * view) ensureSource(video);
        else if (end < -2 * view || start > 3 * view) releaseSource(video);

        if (visible && onScreen) video.play().catch(() => {});
        else video.pause();
      });

      const atStart = rail.scrollLeft <= 2;
      const atEnd = rail.scrollLeft >= rail.scrollWidth - view - 2;
      if (prev) prev.disabled = atStart;
      if (next) next.disabled = atEnd;
      shell?.classList.toggle("is-start", atStart);
      shell?.classList.toggle("is-end", atEnd);

      const active = page();
      dots.forEach((dot, i) => {
        dot.classList.toggle("is-active", i === active);
        dot.setAttribute("aria-selected", i === active ? "true" : "false");
      });

      if (countHost && first >= 0) {
        countHost.textContent =
          first === last
            ? `Clip ${first + 1} of ${clips.length}`
            : `Clips ${first + 1}-${last + 1} of ${clips.length}`;
      }
    };

    const goToPage = (index) => {
      const view = rail.clientWidth;
      rail.scrollTo({ left: index * view, behavior: scrollMode() });
    };

    const measure = () => {
      const view = rail.clientWidth;
      const count = view ? Math.max(1, Math.ceil((rail.scrollWidth - 2) / view)) : 1;

      if (dotsHost && count !== pages) {
        dotsHost.textContent = "";
        dots = Array.from({ length: count }, (_, i) => {
          const dot = document.createElement("button");
          dot.type = "button";
          dot.className = "rail-dot";
          dot.setAttribute("role", "tab");
          dot.setAttribute("aria-label", `Go to page ${i + 1} of ${count}`);
          dot.addEventListener("click", () => goToPage(i));
          dotsHost.append(dot);
          return dot;
        });
      }

      pages = count;
      update();
    };

    /* -------------------------------------------------------- controls --- */

    const nudge = (direction) =>
      rail.scrollBy({ left: direction * rail.clientWidth, behavior: scrollMode() });

    prev?.addEventListener("click", () => nudge(-1));
    next?.addEventListener("click", () => nudge(1));

    rail.addEventListener("keydown", (event) => {
      const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
      if (step) {
        event.preventDefault();
        nudge(step);
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        goToPage(event.key === "Home" ? 0 : pages - 1);
      }
    });

    /* Mouse drag to slide. Snapping is off during the drag so the strip tracks
       the pointer, and back on at release to settle on a card. */
    let dragging = false;
    let originX = 0;
    let originScroll = 0;
    let travel = 0;

    rail.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "mouse" || event.button !== 0) return;
      dragging = true;
      travel = 0;
      originX = event.clientX;
      originScroll = rail.scrollLeft;
    });

    rail.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      const dx = event.clientX - originX;
      if (Math.abs(dx) < 3 && !travel) return;
      travel = Math.max(travel, Math.abs(dx));
      rail.classList.add("is-dragging");
      rail.scrollLeft = originScroll - dx;
      event.preventDefault();
    });

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      rail.classList.remove("is-dragging");
    };

    ["pointerup", "pointercancel", "pointerleave"].forEach((type) =>
      rail.addEventListener(type, endDrag),
    );

    /* Swallow the click that follows a drag so it does not open the lightbox. */
    rail.addEventListener(
      "click",
      (event) => {
        if (travel > 6) {
          event.stopPropagation();
          event.preventDefault();
        }
        travel = 0;
      },
      true,
    );

    /* ---------------------------------------------------- observation ---- */

    let queued = false;
    rail.addEventListener(
      "scroll",
      () => {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => {
          queued = false;
          update();
        });
      },
      { passive: true },
    );

    if ("ResizeObserver" in window) {
      new ResizeObserver(() => measure()).observe(rail);
    } else {
      window.addEventListener("resize", measure);
    }

    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            onScreen = entry.isIntersecting;
          });
          update();
        },
        { threshold: 0.05 },
      ).observe(rail);
    }

    measure();
    carousel.dataset.railReady = "true";
  });
};

/* ----------------------------------------------------------- lightbox ----- */

const setupLightbox = () => {
  const lightbox = document.getElementById("lightbox");
  const video = document.getElementById("lightboxVideo");
  const caption = document.getElementById("lightboxCaption");
  const prev = document.getElementById("lightboxPrev");
  const next = document.getElementById("lightboxNext");
  if (!lightbox || !video) return;

  let group = [];
  let index = -1;

  const sourceOf = (figure) => {
    const clipVideo = figure.querySelector("video");
    return clipVideo?.getAttribute("src") || clipVideo?.dataset.src || "";
  };

  const labelOf = (figure) =>
    figure.querySelector("figcaption")?.textContent.trim().replace(/\s+/g, " ") || "";

  const syncNav = () => {
    const many = group.length > 1;
    [prev, next].forEach((button) => {
      if (button) button.hidden = !many;
    });
    if (prev) prev.disabled = index <= 0;
    if (next) next.disabled = index >= group.length - 1;
  };

  const show = (i) => {
    const figure = group[i];
    const src = figure && sourceOf(figure);
    if (!src) return;

    index = i;
    video.src = src;
    applyPlaybackRate(video);
    if (caption) caption.textContent = labelOf(figure);
    video.play().catch(() => {});
    syncNav();

    /* Move the strip along with the lightbox, so closing it lands on the
       clip that was last on screen. */
    if (figure.closest("[data-rail]")) {
      figure.scrollIntoView({ block: "nearest", inline: "center", behavior: scrollMode() });
    }
  };

  const close = () => {
    if (!lightbox.classList.contains("is-open")) return;
    lightbox.classList.remove("is-open");
    lightbox.setAttribute("aria-hidden", "true");
    video.pause();
    video.removeAttribute("src");
    if (caption) caption.textContent = "";
    document.documentElement.style.overflow = "";
    group = [];
    index = -1;
  };

  const step = (direction) => {
    const target = index + direction;
    if (target < 0 || target >= group.length) return;
    show(target);
  };

  document.addEventListener("click", (event) => {
    const media = event.target.closest(".clip-media");
    if (!media) return;

    const figure = media.closest(".clip");
    if (!figure || !sourceOf(figure)) return;

    /* Siblings of the clicked clip become the lightbox's playlist. */
    const container = figure.closest("[data-rail]") || figure.closest(".clip-grid");
    group = container ? [...container.querySelectorAll(".clip")] : [figure];
    const start = Math.max(0, group.indexOf(figure));

    lightbox.classList.add("is-open");
    lightbox.setAttribute("aria-hidden", "false");
    document.documentElement.style.overflow = "hidden";
    show(start);
  });

  prev?.addEventListener("click", (event) => {
    event.stopPropagation();
    step(-1);
  });

  next?.addEventListener("click", (event) => {
    event.stopPropagation();
    step(1);
  });

  lightbox.querySelector(".lightbox-close")?.addEventListener("click", close);
  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox || event.target.classList.contains("lightbox-inner")) close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      close();
      return;
    }
    if (!lightbox.classList.contains("is-open")) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      step(-1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      step(1);
    }
  });
};

setupSequenceViewer();
setupVideoPlayback();
setupClipRails();
setupLightbox();
