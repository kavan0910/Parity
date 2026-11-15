const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

/** The product in one picture: a legacy app goes in, a migrated app plus proof comes out. */
export const TARGETS = [
  "Java 8 → Java 21",
  "Spring Boot 2 → 3",
  "Django 3 → 5",
  "Express → Fastify",
  "JavaScript → Go",
] as const;

const STEPS = [
  { n: "01", t: "Understand", d: "Read the legacy app. Every claim cites a file and a line.", where: "app" },
  { n: "02", t: "Approve", d: "You sign off the map and decide, per quirk, to keep or fix it.", where: "cli" },
  { n: "03", t: "Migrate", d: "Generate the new app slice by slice, in a sandbox.", where: "cli" },
  { n: "04", t: "Prove", d: "Compare it with the running legacy app, case by case.", where: "cli" },
  { n: "05", t: "Cut over", d: "Move traffic slice by slice, with a plan you approve.", where: "cli" },
] as const;

export function MigrationJourney({ done = 0, compact = false, className, level = 2 }: { done?: number; compact?: boolean; className?: string; level?: 2 | 3 }) {
  const H = (level === 2 ? "h2" : "h3") as "h2" | "h3";
  const S = (level === 2 ? "h3" : "h4") as "h3" | "h4";
  return (
    <section aria-label="The migration journey" className={cx("rounded-2xl border border-line-strong bg-surface p-5 sm:p-6", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="eyebrow">The goal</p>
          <H className="display mt-1 text-2xl sm:text-3xl">Legacy app in, migrated app out, with proof.</H>
        </div>
        <ul className="flex flex-wrap gap-1.5" aria-label="Supported migrations">
          {TARGETS.map((t) => (
            <li key={t} className="rounded-full border border-line-strong px-2.5 py-1 font-mono text-[11px] text-muted">{t}</li>
          ))}
        </ul>
      </div>
      <ol className={cx("mt-5 grid gap-px overflow-hidden rounded-xl border border-line bg-line", compact ? "sm:grid-cols-5" : "md:grid-cols-5")}>
        {STEPS.map((s, i) => {
          const isDone = i < done;
          return (
            <li key={s.n} className="bg-surface p-4">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs text-accent">{s.n}</span>
                <span className={cx("rounded-full px-2 py-0.5 font-mono text-[10px] uppercase", isDone ? "bg-ok-soft text-ok" : s.where === "app" ? "bg-accent-soft text-accent" : "border border-line-strong text-muted")}>
                  {isDone ? "done" : s.where === "app" ? "in this app" : "CLI beta"}
                </span>
              </div>
              <S className="mt-3 font-semibold">{s.t}</S>
              {!compact && <p className="mt-1 text-sm text-muted">{s.d}</p>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
