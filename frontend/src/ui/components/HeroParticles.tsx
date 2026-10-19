import { useEffect } from "react";

// particles.js (v2.0.0, MIT — Vincent Garreau) registers window.particlesJS
// and window.pJSDom as side effects of this import.
import "../../vendor/particles.js";

declare global {
  interface Window {
    particlesJS: (tagId: string, params: Record<string, unknown>) => void;
    // The library's destroypJS() nulls this global (v2.0.0 quirk).
    pJSDom: Array<{
      pJS: {
        canvas: { el: HTMLCanvasElement };
        fn: { vendors: { destroypJS: () => void } };
      };
    }> | null;
  }
}

const CONTAINER_ID = "hero-particles";

// Dots and links in the design's palette, kept faint so the hero grid and
// headline stay dominant.
const PARTICLES_CONFIG: Record<string, unknown> = {
  particles: {
    number: { value: 70, density: { enable: true, value_area: 900 } },
    color: { value: ["#4A6CF7", "#111110"] },
    shape: { type: "circle" },
    opacity: { value: 0.35, random: true, anim: { enable: false } },
    size: { value: 2.6, random: true, anim: { enable: false } },
    line_linked: {
      enable: true,
      distance: 140,
      color: "#4A6CF7",
      opacity: 0.18,
      width: 1,
    },
    move: {
      enable: true,
      speed: 1.1,
      direction: "none",
      random: false,
      straight: false,
      out_mode: "out",
      bounce: false,
    },
  },
  interactivity: {
    detect_on: "canvas",
    events: {
      onhover: { enable: false },
      onclick: { enable: false },
      resize: true,
    },
  },
  retina_detect: true,
};

export function HeroParticles() {
  useEffect(() => {
    // destroypJS() nulls the global registry, so re-init (e.g. React
    // StrictMode's mount-cleanup-mount cycle) must restore it first.
    if (!window.pJSDom) window.pJSDom = [];
    window.particlesJS(CONTAINER_ID, PARTICLES_CONFIG);
    return () => {
      const dom = window.pJSDom ?? [];
      const kept = dom.filter((item) => {
        const canvas = item?.pJS?.canvas?.el;
        if (canvas && canvas.closest(`#${CONTAINER_ID}`)) {
          item.pJS.fn.vendors.destroypJS();
          return false;
        }
        return true;
      });
      window.pJSDom = kept;
    };
  }, []);

  return (
    <div
      id={CONTAINER_ID}
      className="heroParticles"
      style={{
        position: "absolute",
        top: 0,
        bottom: 0,
        right: 0,
        width: "44%",
        zIndex: 0,
        pointerEvents: "none",
        // Fade the field out toward the headline so dots never sit under text.
        WebkitMaskImage: "linear-gradient(to right, transparent, black 28%)",
        maskImage: "linear-gradient(to right, transparent, black 28%)",
      }}
    />
  );
}
