/*
 * dashboard-globe.js — the System Core hero's centerpiece, now the
 * same real Three.js "AI core" object designed in the Command Core
 * concept (wireframe icosahedron, an inner glow, two counter-rotating
 * rings, an orbiting particle shell, and a flattened "projector base"
 * ring underneath so it reads as a projection rather than a free-
 * floating object) rather than the previous hand-rolled Canvas 2D
 * globe.
 *
 * This is a deliberate, explicit exception to this project's usual
 * zero-external-dependency rule (confirmed with the owner) — Three.js
 * loads from cdnjs via a plain <script> tag in dashboard.html, before
 * this file. Everything else in this codebase stays dependency-free;
 * this one visual centerpiece does not.
 *
 * Browser-only, like the other dashboard-*.js files — guarded so
 * requiring this file under plain Node (as the test suite does for
 * dashboard-render.js) is a harmless no-op, and also guarded against
 * THREE failing to load (a network hiccup fetching the CDN script)
 * rather than throwing an uncaught ReferenceError into the page.
 */
(function () {
  if (typeof document === "undefined") return;

  function initCore(canvas) {
    if (typeof THREE === "undefined") return;

    var reduceMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var renderer = new THREE.WebGLRenderer({ canvas: canvas, alpha: true, antialias: true });
    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
    camera.position.set(0, 0, 6);

    function size() {
      var wrap = canvas.parentElement;
      var s = Math.max(1, Math.round((canvas.clientWidth || (wrap && wrap.clientWidth) || 220)));
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
      renderer.setSize(s, s, false);
      camera.aspect = 1;
      camera.updateProjectionMatrix();
    }

    var core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.15, 1),
      new THREE.MeshBasicMaterial({ color: 0x4fd6ff, wireframe: true, transparent: true, opacity: 0.9 })
    );
    scene.add(core);

    var innerGlow = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.55, 1),
      new THREE.MeshBasicMaterial({
        color: 0xbdf2ff, transparent: true, opacity: 0.2,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    scene.add(innerGlow);

    var ring = new THREE.Mesh(
      new THREE.TorusGeometry(1.85, 0.012, 8, 120),
      new THREE.MeshBasicMaterial({
        color: 0xffb15e, transparent: true, opacity: 0.6,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    ring.rotation.x = Math.PI / 2.4;
    scene.add(ring);

    var ring2 = new THREE.Mesh(
      new THREE.TorusGeometry(1.85, 0.012, 8, 120),
      new THREE.MeshBasicMaterial({
        color: 0x4fd6ff, transparent: true, opacity: 0.35,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    ring2.rotation.x = -Math.PI / 2.6;
    ring2.rotation.z = Math.PI / 5;
    scene.add(ring2);

    // Projector base — a flattened glow disc under the core, so it
    // reads as a holographic projection rather than a floating object.
    var baseRing = new THREE.Mesh(
      new THREE.RingGeometry(0.2, 1.5, 48),
      new THREE.MeshBasicMaterial({
        color: 0x4fd6ff, transparent: true, opacity: 0.1, side: THREE.DoubleSide,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    baseRing.rotation.x = -Math.PI / 2.05;
    baseRing.position.y = -1.3;
    scene.add(baseRing);

    var particleCount = 170;
    var positions = new Float32Array(particleCount * 3);
    for (var i = 0; i < particleCount; i++) {
      var r = 2.25 + Math.random() * 0.45;
      var theta = Math.random() * Math.PI * 2;
      var phi = Math.acos((Math.random() * 2) - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      positions[i * 3 + 2] = r * Math.cos(phi);
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    var points = new THREE.Points(
      geo,
      new THREE.PointsMaterial({
        color: 0x9fe8ff, size: 0.035, transparent: true, opacity: 0.85,
        blending: THREE.AdditiveBlending, depthWrite: false,
      })
    );
    scene.add(points);

    // A gentle constant rotation isn't the kind of motion accessibility
    // guidance is aimed at (no parallax, no flashing, no sudden
    // movement) -- reduced motion slows this way down rather than
    // fully freezing it, same choice the previous Canvas 2D globe made.
    var speedFactor = reduceMotion ? 0.08 : 1;

    function animate(t) {
      core.rotation.y += 0.0032 * speedFactor;
      core.rotation.x += 0.0011 * speedFactor;
      ring.rotation.z += 0.0018 * speedFactor;
      ring2.rotation.z -= 0.0013 * speedFactor;
      baseRing.rotation.z += 0.0008 * speedFactor;
      points.rotation.y -= 0.0009 * speedFactor;
      var pulse = 1 + Math.sin(t * 0.0016) * 0.06;
      innerGlow.scale.setScalar(pulse);
      core.material.opacity = 0.78 + Math.sin(t * 0.0011) * 0.12;
      renderer.render(scene, camera);
      requestAnimationFrame(animate);
    }

    size();
    requestAnimationFrame(animate);

    var resizeTimer = null;
    window.addEventListener("resize", function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(size, 150);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var canvas = document.getElementById("system-core-globe");
    if (canvas) initCore(canvas);
  });
})();
