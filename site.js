const VIDEO_CATALOG = [
  {
    id: "pstudio",
    title: "Portrait Studio",
    source: "PStudio",
    video: "assets/pstudio_1.mp4",
    type: "video/mp4",
    tags: ["Indoor scene", "Dense tracks"],
    description:
      "Indoor portrait-studio sequence with dense 3D point tracks across static and dynamic scene content.",
  },
  {
    id: "twirl",
    title: "Twirl",
    source: "DAVIS",
    video: "assets/twirl_1.mp4",
    type: "video/mp4",
    tags: ["Rotation", "Persistent tracks"],
    description:
      "Rotational motion stress test: TrackEverything maintains persistent 3D scene tracks through fast twirling.",
  },
  {
    id: "libby",
    title: "Libby",
    source: "DAVIS",
    video: "assets/davis_libby_mask.mp4",
    type: "video/mp4",
    tags: ["Dense 3D tracking", "Occlusion"],
    description:
      "Dense 3D point tracks on a masked foreground subject, with persistent world-coordinate trajectories through partial occlusion.",
  },
  {
    id: "dance-twirl",
    title: "Dance Twirl",
    source: "DAVIS",
    video: "assets/davis_dance-twirl.mp4",
    type: "video/mp4",
    tags: ["Fast motion", "Articulated object"],
    description:
      "Tracking through rapid rotational motion: dense 3D scene tracks remain stable as the dancer twirls.",
  },
  {
    id: "drive-chicane",
    title: "Drive Chicane",
    source: "DAVIS",
    video: "assets/davis-drive-chicane.mp4",
    type: "video/mp4",
    tags: ["Dynamic scene", "Camera motion"],
    description:
      "Outdoor driving sequence with moving camera and multiple dynamic objects tracked densely in 3D.",
  },
  {
    id: "soap-box",
    title: "Soap Box",
    source: "In the wild",
    video: "assets/soap-box.mp4",
    type: "video/mp4",
    tags: ["Long video", "Multi-object"],
    description:
      "In-the-wild sequence demonstrating dense 3D tracking across a complex dynamic scene.",
  },
  {
    id: "cow",
    title: "Cow",
    source: "DAVIS",
    video: "assets/davis_cow.mp4",
    type: "video/mp4",
    tags: ["Animal motion", "Deformable object"],
    description:
      "Dense tracks on a deformable animal subject, fusing repeated surface observations across frames.",
  },
];

const videoById = new Map(VIDEO_CATALOG.map((item) => [item.id, item]));

const ZOOM_ICON = `
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
      d="M3 3h4M3 3v4M13 13H9M13 13V9M3 13h4M3 13v-4M13 3H9M13 3v4"/>
  </svg>
`;

const escapeHtml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const renderHeroShowcase = () => {
  const stage = document.getElementById("heroShowcase");
  if (!stage) return;

  stage.innerHTML = `
    <div class="hero-video-strip" aria-label="TrackEverything tracking visualizations">
      ${VIDEO_CATALOG.map(
        (item) => `
          <button
            type="button"
            class="hero-video-card"
            data-video-id="${escapeHtml(item.id)}"
            aria-label="Open ${escapeHtml(item.title)} in detail"
          >
            <video muted loop playsinline preload="metadata" data-autoplay>
              <source src="${escapeHtml(item.video)}" type="${escapeHtml(item.type)}">
            </video>
          </button>
        `,
      ).join("")}
    </div>
  `;

  const strip = stage.querySelector(".hero-video-strip");
  const cards = [...strip.querySelectorAll(".hero-video-card")];
  const gap = 6;
  const ratios = new Map();
  const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  cards.forEach((card, index) => {
    if (prefersReducedMotion) {
      card.classList.add("is-in");
      return;
    }
    setTimeout(() => card.classList.add("is-in"), 140 + index * 45);
  });

  const targetRowHeight = () => {
    const w = window.innerWidth;
    if (w <= 600) return 96;
    if (w <= 880) return 128;
    return 168;
  };

  const visibleCount = () => {
    const w = window.innerWidth;
    if (w <= 600) return 6;
    if (w <= 880) return Math.min(9, cards.length);
    return cards.length;
  };

  const layout = () => {
    const containerWidth = strip.clientWidth;
    if (!containerWidth) return;
    const target = targetRowHeight();
    const count = visibleCount();

    cards.forEach((card, index) => {
      card.style.display = index < count ? "block" : "none";
    });

    const activeCards = cards.slice(0, count);

    const flushRow = (rowCards) => {
      if (!rowCards.length) return;
      const ratioSum = rowCards.reduce((sum, card) => sum + (ratios.get(card) || 1.6), 0);
      const gaps = gap * (rowCards.length - 1);
      const rowHeight = (containerWidth - gaps) / ratioSum;
      rowCards.forEach((card) => {
        const ratio = ratios.get(card) || 1.6;
        card.style.flex = "0 0 auto";
        card.style.height = `${rowHeight}px`;
        card.style.width = `${rowHeight * ratio}px`;
      });
    };

    let row = [];
    let rowRatio = 0;
    const maxRowHeight = target * 1.5;

    activeCards.forEach((card) => {
      const ratio = ratios.get(card) || 1.6;
      row.push(card);
      rowRatio += ratio;
      const projectedWidth = rowRatio * target + gap * (row.length - 1);
      if (projectedWidth >= containerWidth) {
        flushRow(row);
        row = [];
        rowRatio = 0;
      }
    });

    if (row.length) {
      const ratioSum = row.reduce((sum, card) => sum + (ratios.get(card) || 1.6), 0);
      const gaps = gap * (row.length - 1);
      const fullWidthHeight = (containerWidth - gaps) / ratioSum;
      if (fullWidthHeight <= maxRowHeight) {
        flushRow(row);
      } else {
        row.forEach((card) => {
          card.style.display = "none";
        });
      }
    }
  };

  cards.forEach((card) => {
    const video = card.querySelector("video");
    if (!video) return;
    const onMeta = () => {
      const ratio = video.videoWidth / video.videoHeight;
      if (Number.isFinite(ratio) && ratio > 0) ratios.set(card, ratio);
      layout();
    };
    if (video.readyState >= 1) onMeta();
    else video.addEventListener("loadedmetadata", onMeta, { once: true });
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(layout, 120);
  });

  layout();
};

const renderExampleGallery = () => {
  const target = document.getElementById("exampleGallery");
  if (!target) return;

  target.innerHTML = VIDEO_CATALOG.map(
    (item) => `
      <article class="example-card" data-video-id="${escapeHtml(item.id)}" data-source="${escapeHtml(item.source)}">
        <div class="example-media">
          <video
            muted
            loop
            playsinline
            preload="none"
            data-autoplay
            data-unload="true"
            data-src="${escapeHtml(item.video)}"
          ></video>
          <button type="button" class="example-zoom" aria-label="Open ${escapeHtml(item.title)} in detail">
            ${ZOOM_ICON}
          </button>
        </div>
        <div class="example-body">
          <div class="example-topline">
            <span>${escapeHtml(item.source)}</span>
          </div>
          <h3>${escapeHtml(item.title)}</h3>
          <div class="example-taxonomy">
            ${item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}
          </div>
          <div class="mini-qa">
            <p>${escapeHtml(item.description)}</p>
          </div>
        </div>
      </article>
    `,
  ).join("");
};

const setupVideoLightbox = () => {
  const lightbox = document.getElementById("exampleLightbox");
  if (!lightbox) return;

  const video = lightbox.querySelector(".example-lightbox-video");
  const body = lightbox.querySelector("#exampleLightboxBody");
  const closeBtn = lightbox.querySelector(".example-lightbox-close");

  const close = () => {
    if (!lightbox.classList.contains("is-open")) return;
    lightbox.classList.remove("is-open");
    lightbox.setAttribute("aria-hidden", "true");
    video.pause();
    video.removeAttribute("src");
    body.innerHTML = "";
    document.documentElement.style.overflow = "";
  };

  const open = (item) => {
    video.src = item.video;
    body.innerHTML = `
      <div class="lightbox-meta">
        <span class="lightbox-source">${escapeHtml(item.source)}</span>
        <h3>${escapeHtml(item.title)}</h3>
        <div class="lightbox-tax">
          ${item.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}
        </div>
      </div>
      <div class="lightbox-description">
        <span class="lightbox-q-label">Tracking visualization</span>
        <p class="lightbox-q">${escapeHtml(item.description)}</p>
      </div>
    `;

    lightbox.classList.add("is-open");
    lightbox.setAttribute("aria-hidden", "false");
    document.documentElement.style.overflow = "hidden";
    video.play().catch(() => {});
  };

  document.addEventListener("click", (event) => {
    const card = event.target.closest(".example-card, .hero-video-card");
    if (!card || lightbox.contains(event.target)) return;
    const videoId = card.dataset.videoId;
    if (!videoId) return;
    const item = videoById.get(videoId);
    if (!item) return;
    event.preventDefault();
    open(item);
  });

  closeBtn.addEventListener("click", close);
  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") close();
  });
};

const setupGalleryCollapse = () => {
  const gallery = document.getElementById("exampleGallery");
  const toggle = document.getElementById("exampleToggle");
  if (!gallery || !toggle) return;

  const cards = gallery.querySelectorAll(".example-card");
  if (cards.length <= 4) {
    toggle.style.display = "none";
    return;
  }

  gallery.classList.add("is-collapsed");

  const setLabel = () => {
    const expanded = !gallery.classList.contains("is-collapsed");
    toggle.textContent = expanded ? "Show fewer" : `Show all ${cards.length} clips`;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  };

  setLabel();
  toggle.addEventListener("click", () => {
    gallery.classList.toggle("is-collapsed");
    setLabel();
    if (gallery.classList.contains("is-collapsed")) {
      gallery.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
};

const setupVideoPlayback = () => {
  const videos = [...document.querySelectorAll("video[data-autoplay]")];
  if (!videos.length) return;

  const ensureSource = (video) => {
    if (!video.getAttribute("src") && video.dataset.src) {
      video.setAttribute("src", video.dataset.src);
    }
  };

  const releaseSource = (video) => {
    if (video.dataset.unload !== "true" || !video.getAttribute("src")) return;
    video.pause();
    video.removeAttribute("src");
    video.load();
  };

  const loadObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) =>
        entry.isIntersecting ? ensureSource(entry.target) : releaseSource(entry.target),
      );
    },
    { rootMargin: "500px 0px" },
  );

  const playObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        const videoEl = entry.target;
        if (entry.isIntersecting) {
          ensureSource(videoEl);
          videoEl.play().catch(() => {});
        } else {
          videoEl.pause();
        }
      });
    },
    { threshold: 0.15 },
  );

  videos.forEach((videoEl) => {
    loadObserver.observe(videoEl);
    playObserver.observe(videoEl);
  });
};

const setupHeroSpotlight = () => {
  const hero = document.querySelector(".hero-panel");
  if (!hero) return;

  hero.addEventListener("pointermove", (event) => {
    const rect = hero.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 100;
    const y = ((event.clientY - rect.top) / rect.height) * 100;
    hero.style.setProperty("--hero-x", `${x.toFixed(1)}%`);
    hero.style.setProperty("--hero-y", `${y.toFixed(1)}%`);
  });
};

const setupReveal = () => {
  const items = document.querySelectorAll(
    [
      ".hero-panel",
      ".leaderboard-head",
      ".leaderboard-board",
      ".section-heading",
      ".vstat-stats",
      ".example-card",
      ".results-table-wrapper",
      ".citation-section pre",
    ].join(", "),
  );

  items.forEach((item, index) => {
    item.classList.add("reveal-item");
    item.style.setProperty("--reveal-delay", `${Math.min(index % 8, 6) * 45}ms`);
  });

  document.body.classList.add("reveal-ready");

  if (!("IntersectionObserver" in window)) {
    items.forEach((item) => item.classList.add("is-visible"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.12 },
  );

  items.forEach((item) => observer.observe(item));
};

const setupScrollProgress = () => {
  const update = () => {
    const scrollTop = window.scrollY;
    const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
    const progress = maxScroll > 0 ? (scrollTop / maxScroll) * 100 : 0;
    document.documentElement.style.setProperty("--scroll-progress", `${progress}%`);
  };

  update();
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
};

const setupActiveNav = () => {
  const links = [...document.querySelectorAll(".site-nav a[href^='#']")];
  const sections = links
    .map((link) => {
      const id = link.getAttribute("href").slice(1);
      const section = document.getElementById(id);
      return section ? { link, section } : null;
    })
    .filter(Boolean);

  if (!sections.length || !("IntersectionObserver" in window)) return;

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        links.forEach((link) => link.classList.remove("is-active"));
        const match = sections.find(({ section }) => section === entry.target);
        if (match) match.link.classList.add("is-active");
      });
    },
    { rootMargin: "-20% 0px -65% 0px", threshold: 0 },
  );

  sections.forEach(({ section }) => observer.observe(section));
};

renderHeroShowcase();
renderExampleGallery();
setupVideoLightbox();
setupGalleryCollapse();
setupVideoPlayback();
setupHeroSpotlight();
setupReveal();
setupScrollProgress();
setupActiveNav();
