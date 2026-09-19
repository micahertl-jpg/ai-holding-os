/*
 * dashboard-globe.js — a real rotating 3D wireframe globe for the
 * System Core hero panel, hand-rolled on a single <canvas> with plain
 * trigonometry (Fibonacci-sphere point distribution, nearest-neighbor
 * mesh edges, a Y/X rotation matrix, and a simple orthographic
 * projection with depth-based opacity/size). No WebGL, no Three.js, no
 * external assets — consistent with this project's zero-dependency
 * convention (dashboard-render.js's tests run under plain Node with no
 * npm packages at all).
 *
 * Browser-only, like dashboard.js: guarded so requiring this file
 * under plain Node (as the test suite does for dashboard-render.js) is
 * a harmless no-op rather than a ReferenceError on `document`.
 */
(function () {
  if (typeof document === "undefined") return;

  function fibonacciSpherePoints(n) {
    const points = [];
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < n; i++) {
      const y = 1 - (i / (n - 1)) * 2;
      const radiusAtY = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = goldenAngle * i;
      points.push([Math.cos(theta) * radiusAtY, y, Math.sin(theta) * radiusAtY]);
    }
    return points;
  }

  // Connects each point to its `k` nearest neighbors (by straight-line
  // 3D distance) to build the triangulated mesh look, de-duplicating
  // shared edges. O(n^2) but n is small (a couple hundred points at
  // most) and this only ever runs once at setup.
  function buildNearestNeighborEdges(points, k) {
    const n = points.length;
    const seen = new Set();
    const edges = [];
    for (let i = 0; i < n; i++) {
      const dists = [];
      for (let j = 0; j < n; j++) {
        if (i === j) continue;
        const dx = points[i][0] - points[j][0];
        const dy = points[i][1] - points[j][1];
        const dz = points[i][2] - points[j][2];
        dists.push([dx * dx + dy * dy + dz * dz, j]);
      }
      dists.sort((a, b) => a[0] - b[0]);
      for (let m = 0; m < k && m < dists.length; m++) {
        const j = dists[m][1];
        const key = i < j ? i + "," + j : j + "," + i;
        if (!seen.has(key)) {
          seen.add(key);
          edges.push([i, j]);
        }
      }
    }
    return edges;
  }

  function lerp(t, a, b) {
    return a + (b - a) * t;
  }

  function initGlobe(canvas) {
    const ctx = canvas.getContext("2d");
    const reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const POINT_COUNT = 190;
    const NEIGHBORS_PER_POINT = 3;
    const points = fibonacciSpherePoints(POINT_COUNT);
    const edges = buildNearestNeighborEdges(points, NEIGHBORS_PER_POINT);

    const TILT_X = 0.4; // fixed slight downward look, like the reference photo
    let angleY = 0.6;
    let size = 0;
    let dpr = Math.min(window.devicePixelRatio || 1, 2);

    function resize() {
      const cssSize = canvas.clientWidth || canvas.parentElement.clientWidth || 260;
      size = cssSize;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = size * dpr;
      canvas.height = size * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function project(p) {
      const cosY = Math.cos(angleY);
      const sinY = Math.sin(angleY);
      const x1 = p[0] * cosY - p[2] * sinY;
      const z1 = p[0] * sinY + p[2] * cosY;
      const y1 = p[1];

      const cosX = Math.cos(TILT_X);
      const sinX = Math.sin(TILT_X);
      const y2 = y1 * cosX - z1 * sinX;
      const z2 = y1 * sinX + z1 * cosX;

      const scale = size * 0.42;
      const cx = size / 2;
      const cy = size / 2;
      return { x: cx + x1 * scale, y: cy + y2 * scale, z: z2 };
    }

    function drawFrame() {
      ctx.clearRect(0, 0, size, size);
      const projected = points.map(project);

      ctx.lineWidth = 1;
      for (const [i, j] of edges) {
        const a = projected[i];
        const b = projected[j];
        const avgZ = (a.z + b.z) / 2; // -1 (far side) .. 1 (near side)
        const alpha = lerp((avgZ + 1) / 2, 0.04, 0.5);
        ctx.strokeStyle = `rgba(34,211,238,${alpha.toFixed(3)})`;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }

      // Back-to-front so near-side nodes correctly draw over far-side
      // mesh lines, matching how a real translucent wireframe sphere reads.
      const order = projected.map((_, i) => i).sort((a, b) => projected[a].z - projected[b].z);
      for (const i of order) {
        const p = projected[i];
        const t = (p.z + 1) / 2;
        const r = lerp(t, 0.9, 2.6);
        const alpha = lerp(t, 0.3, 1);
        ctx.beginPath();
        ctx.fillStyle = `rgba(148,244,255,${alpha.toFixed(3)})`;
        ctx.shadowColor = "rgba(34,211,238,0.9)";
        ctx.shadowBlur = t > 0.55 ? 5 : 0;
        ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.shadowBlur = 0;
    }

    function loop() {
      angleY += 0.0028;
      drawFrame();
      requestAnimationFrame(loop);
    }

    resize();
    if (reduceMotion) {
      drawFrame();
    } else {
      loop();
    }

    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        resize();
        if (reduceMotion) drawFrame();
      }, 150);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const canvas = document.getElementById("system-core-globe");
    if (canvas) initGlobe(canvas);
  });
})();
