import type { CSSProperties } from "react";

const v = (o: Record<string, string | number>) => o as CSSProperties;

/** Tiny live demos for the four problems. Decorative: each one animates when its card scrolls into view. */
export function PainViz({ kind }: { kind: "map" | "diff" | "slices" | "budget" }) {
  if (kind === "map")
    return (
      <div className="viz viz-map" aria-hidden="true">
        {[["services.py:18-22", 0], ["OrderRules.java:41", 1]].map(([c, r]) => (
          <div key={c as string} className="row" style={v({ "--r": r as number })}><span className="bar" /><b>{c}</b></div>
        ))}
      </div>
    );
  if (kind === "diff")
    return (
      <div className="viz viz-diff" aria-hidden="true">
        <span>200<b>200</b></span><span>400<b>400</b></span><span className="bad">404<b>410</b></span>
      </div>
    );
  if (kind === "slices")
    return (
      <div className="viz viz-slices" aria-hidden="true">
        {[0, 1, 2, 3, 4].map((i) => <i key={i} style={v({ "--i": i })} />)}<em>1 slice at a time</em>
      </div>
    );
  return (
    <div className="viz viz-budget" aria-hidden="true">
      <div className="track"><span className="fill" /><span className="cap" /></div>
      <p><span>spend</span><span>hard cap</span></p>
    </div>
  );
}

export type IconKind = "repo" | "noexec" | "sandbox" | "lock" | "approve" | "gauge";
const ICONS: Record<IconKind, string[]> = {
  repo: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  noexec: ["M4 5h16v14H4z", "M8 10l3 2-3 2", "M13 15h3", "M3 3l18 18"],
  sandbox: ["M12 3l8 4.5v9L12 21l-8-4.5v-9z", "M12 12l8-4.5", "M12 12v9", "M12 12L4 7.5"],
  lock: ["M6 11h12v9H6z", "M8.5 11V8a3.5 3.5 0 0 1 7 0v3", "M12 15v2"],
  approve: ["M4 12.5l5 5L20 6.5", "M3 21h18"],
  gauge: ["M4 17a8 8 0 1 1 16 0", "M12 17l4-5", "M12 17h.01"],
};
export function SecurityIcon({ kind, i = 0 }: { kind: IconKind; i?: number }) {
  return (
    <svg className="sicon" width="34" height="34" viewBox="0 0 24 24" aria-hidden="true">
      {ICONS[kind].map((d, n) => <path key={n} d={d} pathLength={60} className="draw" style={v({ "--i": i + n })} />)}
    </svg>
  );
}
