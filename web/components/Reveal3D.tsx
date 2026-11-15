"use client";
import { useEffect, useRef, type ReactNode } from "react";

/** Fades a block in with a slight perspective tilt as it scrolls into view. Content stays in the HTML for crawlers. */
export function Reveal3D({ children, className = "", delay = 0 }: { children: ReactNode; className?: string; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (!("IntersectionObserver" in window)) { el.classList.add("in"); return; }
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return <div ref={ref} className={`r3d ${className}`} style={{ ["--d" as string]: `${delay}s` }}>{children}</div>;
}
