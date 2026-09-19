/*
 * dashboard-tilt.js — a small hover-driven 3D tilt on every .panel,
 * reinforcing the "three dimensional" feel across the whole dashboard,
 * not just the System Core globe. Pure CSS `transform: perspective()
 * rotateX() rotateY()` set per-element on mousemove, eased back to flat
 * on mouseleave by the .panel rule's existing `transition: transform`.
 *
 * Browser-only, like dashboard.js/dashboard-globe.js — guarded so this
 * file is a harmless no-op if ever required under plain Node.
 */
(function () {
  if (typeof document === "undefined") return;

  const MAX_TILT_DEG = 5;

  document.addEventListener("DOMContentLoaded", () => {
    const reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const finePointer = window.matchMedia && window.matchMedia("(pointer: fine)").matches;
    if (reduceMotion || !finePointer) return;

    let rafId = null;
    let pendingEl = null;
    let pendingX = 0;
    let pendingY = 0;

    function applyTilt() {
      rafId = null;
      if (!pendingEl) return;
      pendingEl.style.transform =
        `perspective(800px) rotateX(${pendingY}deg) rotateY(${pendingX}deg)`;
    }

    function onMove(ev) {
      const panel = ev.currentTarget;
      const rect = panel.getBoundingClientRect();
      const relX = (ev.clientX - rect.left) / rect.width; // 0..1
      const relY = (ev.clientY - rect.top) / rect.height; // 0..1
      pendingEl = panel;
      pendingX = (relX - 0.5) * 2 * MAX_TILT_DEG; // rotateY follows horizontal position
      pendingY = -(relY - 0.5) * 2 * MAX_TILT_DEG; // rotateX follows vertical position, inverted
      if (!rafId) rafId = requestAnimationFrame(applyTilt);
    }

    function onLeave(ev) {
      ev.currentTarget.style.transform = "";
    }

    // .no-tilt opts out very large/full-width panels (e.g. the System
    // Core hero): a big rotateX/rotateY on a very wide element visibly
    // shifts its rendered hit-test box enough that the cursor can end
    // up outside it mid-hover, firing a spurious mouseleave and making
    // the tilt flicker -- this only ever looks right on roughly
    // card-sized panels.
    document.querySelectorAll(".panel:not(.no-tilt)").forEach((panel) => {
      panel.addEventListener("mousemove", onMove);
      panel.addEventListener("mouseleave", onLeave);
    });
  });
})();
