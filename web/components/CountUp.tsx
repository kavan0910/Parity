"use client";
import { useEffect, useRef, useState } from "react";

/** Counts up to its value when scrolled into view. The server renders the final text, so crawlers and no-JS readers see it. */
export function CountUp({ to, pre = "", post = "", decimals = 0 }: { to: number; pre?: string; post?: string; decimals?: number }) {
  const ref = useRef<HTMLSpanElement>(null);
  const final = `${pre}${to.toFixed(decimals)}${post}`;
  const [text, setText] = useState(final);
  useEffect(() => {
    const el = ref.current;
    if (!el || window.matchMedia("(prefers-reduced-motion: reduce)").matches || !("IntersectionObserver" in window)) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setText(`${pre}${(0).toFixed(decimals)}${post}`);
    let raf = 0;
    const io = new IntersectionObserver((es) => {
      if (!es[0]?.isIntersecting) return;
      io.disconnect();
      const t0 = performance.now(), dur = 1500;
      const step = (now: number) => {
        const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
        setText(`${pre}${(to * e).toFixed(decimals)}${post}`);
        if (p < 1) raf = requestAnimationFrame(step);
      };
      raf = requestAnimationFrame(step);
    }, { threshold: 0.4 });
    io.observe(el);
    return () => { io.disconnect(); cancelAnimationFrame(raf); };
  }, [to, pre, post, decimals]);
  return <span ref={ref} aria-label={final}>{text}</span>;
}
