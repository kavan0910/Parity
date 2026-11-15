"use client";
import { useRef, type ReactNode } from "react";

/** Pointer-tracking 3D tilt with a light glare. Mouse only; does nothing for touch or reduced motion. */
export function Tilt3D({ children, className = "", max = 7 }: { children: ReactNode; className?: string; max?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const move = (e: React.PointerEvent) => {
    if (e.pointerType !== "mouse" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
    el.style.transform = `perspective(1000px) rotateX(${((0.5 - py) * max).toFixed(2)}deg) rotateY(${((px - 0.5) * max).toFixed(2)}deg)`;
    el.style.setProperty("--gx", `${(px * 100).toFixed(1)}%`);
    el.style.setProperty("--gy", `${(py * 100).toFixed(1)}%`);
  };
  const leave = () => { if (ref.current) ref.current.style.transform = ""; };
  return (
    <div className="persp">
      <div ref={ref} onPointerMove={move} onPointerLeave={leave} className={`tilt relative ${className}`}>
        {children}
        <span className="glare" aria-hidden="true" />
      </div>
    </div>
  );
}
