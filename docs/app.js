(function () {
  const searchInput = document.querySelector("#guide-search");
  const searchable = Array.from(document.querySelectorAll(".searchable"));
  const sections = Array.from(document.querySelectorAll(".guide-section"));
  const tocLinks = Array.from(document.querySelectorAll(".toc a"));
  const lightbox = document.querySelector("#lightbox");
  const lightboxImage = lightbox?.querySelector(".lightbox__image");
  const closeLightbox = lightbox?.querySelector(".lightbox__close");

  const empty = document.createElement("div");
  empty.className = "empty-search";
  empty.textContent = "No matching NL Nodes documentation found.";
  document.querySelector(".content")?.prepend(empty);

  function normalize(value) {
    return String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
  }

  function matches(element, query) {
    if (!query) return true;
    const haystack = normalize(`${element.textContent || ""} ${element.dataset.search || ""}`);
    return query.split(" ").every((part) => haystack.includes(part));
  }

  function applySearch() {
    const query = normalize(searchInput?.value || "");
    let visibleSections = 0;

    sections.forEach((section) => {
      const sectionMatches = matches(section, query);
      const childCards = Array.from(section.querySelectorAll(".node-card.searchable, .callout.searchable, .step-card.searchable"));
      let visibleChildren = 0;

      childCards.forEach((card) => {
        const show = matches(card, query);
        card.classList.toggle("is-hidden", !show);
        if (show) visibleChildren += 1;
      });

      const showSection = sectionMatches || visibleChildren > 0 || !query;
      section.classList.toggle("is-hidden", !showSection);
      if (showSection) visibleSections += 1;
    });

    empty.classList.toggle("is-visible", visibleSections === 0);
  }

  function openLightbox(src, alt) {
    if (!lightbox || !lightboxImage) return;
    lightboxImage.src = src;
    lightboxImage.alt = alt || "NL Nodes screenshot";
    lightbox.classList.add("is-open");
    lightbox.setAttribute("aria-hidden", "false");
    closeLightbox?.focus();
  }

  function hideLightbox() {
    if (!lightbox || !lightboxImage) return;
    lightbox.classList.remove("is-open");
    lightbox.setAttribute("aria-hidden", "true");
    lightboxImage.removeAttribute("src");
  }

  document.querySelectorAll("[data-lightbox]").forEach((button) => {
    button.addEventListener("click", () => {
      const image = button.querySelector("img");
      openLightbox(button.getAttribute("data-lightbox"), image?.alt || "");
    });
  });

  closeLightbox?.addEventListener("click", hideLightbox);
  lightbox?.addEventListener("click", (event) => {
    if (event.target === lightbox) hideLightbox();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideLightbox();
  });

  searchInput?.addEventListener("input", applySearch);

  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((entry) => entry.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      tocLinks.forEach((link) => {
        link.classList.toggle("is-active", link.getAttribute("href") === `#${visible.target.id}`);
      });
    },
    { rootMargin: "-20% 0px -70% 0px", threshold: [0.1, 0.25, 0.5] }
  );

  sections.forEach((section) => observer.observe(section));
})();
