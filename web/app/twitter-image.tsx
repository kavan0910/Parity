import { ImageResponse } from "next/og";

export const alt = "Migration Agent: migrate a legacy app to the newest version or a different stack, with proof";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function Image() {
  const chips = ["Java 8 → 21", "Spring Boot 2 → 3", "Django 3 → 5", "Express → Fastify", "JavaScript → Go"];
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between", background: "#0a0a08", color: "#f5f3ec", padding: 72 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 30, color: "#5eead4" }}>
          <div style={{ width: 18, height: 18, borderRadius: 9, background: "#5eead4" }} />
          migration agent
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          <div style={{ fontSize: 76, lineHeight: 1.04, fontWeight: 600, letterSpacing: -2 }}>Upgrade or replatform your legacy app.</div>
          <div style={{ fontSize: 38, color: "#a8a69b" }}>Then prove the new one behaves the same.</div>
        </div>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          {chips.map((c) => (
            <div key={c} style={{ display: "flex", padding: "8px 18px", border: "2px solid #3a3a33", borderRadius: 999, fontSize: 26, color: "#d6d3c6" }}>{c}</div>
          ))}
        </div>
      </div>
    ),
    size,
  );
}
