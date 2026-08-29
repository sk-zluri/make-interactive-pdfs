(() => {
  const root = document.documentElement;
  const themeToggle = document.querySelector(".theme-toggle");
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");

  const effectiveTheme = () => {
    const explicit = root.dataset.theme;
    if (explicit === "light" || explicit === "dark") {
      return explicit;
    }
    return systemTheme.matches ? "dark" : "light";
  };

  const updateThemeControl = () => {
    if (!themeToggle) {
      return;
    }
    const current = effectiveTheme();
    const next = current === "dark" ? "light" : "dark";
    themeToggle.setAttribute("aria-label", `Switch to ${next} theme`);
    themeToggle.setAttribute("title", `Switch to ${next} theme`);
  };

  themeToggle?.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try {
      localStorage.setItem("mip-site-theme", next);
    } catch (_error) {
      // The chosen theme still applies for this page when storage is unavailable.
    }
    updateThemeControl();
  });

  systemTheme.addEventListener?.("change", updateThemeControl);
  updateThemeControl();

  const revealItems = Array.from(document.querySelectorAll(".reveal"));
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  if (reducedMotion.matches || !("IntersectionObserver" in window)) {
    revealItems.forEach((item) => item.classList.add("is-visible"));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.14, rootMargin: "0px 0px -5% 0px" });

    revealItems.forEach((item) => observer.observe(item));
    window.addEventListener("pagehide", () => observer.disconnect(), { once: true });
  }
})();
