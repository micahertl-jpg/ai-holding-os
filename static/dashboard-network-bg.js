/*
 * dashboard-network-bg.js — a subtle, whole-page ambient "web" texture:
 * a sparse field of drifting particles, connected by a thin line
 * whenever two of them are close enough. Same hand-rolled Canvas 2D
 * approach as dashboard-globe.js (no libraries), sitting fixed behind
 * every panel so the "web-like feel" carries across the whole
 * dashboard, not just the System Core hub.
 *
 * Browser-only, like the other dashboard-*.js files -- guarded so
 * requiring this under plain Node is a harmless no-op.
 */
(function () {
  if (typeof document === "undefined") return;

  const PARTICLE_COUNT = 55;
  const LINK_DISTANCE = 150;
  const DRIFT_SPEED = 0.15;

  function initNetworkBackground(canvas) {
    const ctx = canvas.getContext("2d");
    const reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let width = 0;
    let height = 0;
    let particles = [];

    function resize() {
      width = window.innerWidth;
      height = window.innerHeight;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = width * dpr;
      canvas.height = height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function initParticles() {
      particles = [];
      for (let i = 0; i < PARTICLE_COUNT; i++) {
        particles.push({
          x: Math.random() * width,
          y: Math.random() * height,
          vx: (Math.random() - 0.5) * DRIFT_SPEED,
          vy: (Math.random() - 0.5) * DRIFT_SPEED,
        });
      }
    }

    function step() {
      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0) p.x += width;
        if (p.x > width) p.x -= width;
        if (p.y < 0) p.y += height;
        if (p.y > height) p.y -= height;
      }
    }

    function draw() {
      ctx.clearRect(0, 0, width, height);
      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const a = particles[i];
          const b = particles[j];
          const dx = a.x - b.x;
          const dy = a.y - b.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < LINK_DISTANCE) {
            const alpha = (1 - dist / LINK_DISTANCE) * 0.12;
            ctx.strokeStyle = `rgba(79,214,255,${alpha.toFixed(3)})`;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }
      for (const p of particles) {
        ctx.beginPath();
        ctx.fillStyle = "rgba(159,232,255,0.35)";
        ctx.arc(p.x, p.y, 1.3, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    function loop() {
      if (!reduceMotion) step();
      draw();
      requestAnimationFrame(loop);
    }

    resize();
    initParticles();
    loop();

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resize();
        initParticles();
      }, 200);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const canvas = document.getElementById("network-bg");
    if (canvas) initNetworkBackground(canvas);
  });
})();
