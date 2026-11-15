import type { Metadata } from "next";
import Link from "next/link";
import { Logo, ThemeToggle } from "@/components/Brand";
import { ConfidenceMeter, LinkButton, TypeGlyph } from "@/components/ui";
import { MigrationJourney, TARGETS } from "@/components/MigrationJourney";
import { HeroScene } from "@/components/HeroScene";
import { Reveal3D } from "@/components/Reveal3D";
import { Tilt3D } from "@/components/Tilt3D";
import { CountUp } from "@/components/CountUp";
import { ScrollProgress } from "@/components/ScrollProgress";
import { PainViz, SecurityIcon, type IconKind } from "@/components/SectionArt";
import { CONTACT_EMAIL, FAQ, HOME_DESCRIPTION, HOME_TITLE, PATHS, jsonLd } from "@/lib/site";

export const metadata: Metadata = {
  title: { absolute: HOME_TITLE },
  description: HOME_DESCRIPTION,
  alternates: { canonical: "/" },
};

const CODE = [
  [16, "def cancel(registration):"],
  [17, "    workshop = registration.workshop"],
  [18, "    if not registration.waitlisted:"],
  [19, "        nxt = workshop.registrations.filter(waitlisted=True).first()"],
  [20, "        if nxt:"],
  [21, "            nxt.waitlisted = False"],
  [22, "            nxt.save()"],
] as const;

const PROOF = [
  ["GET /api/orders", "200", "200"],
  ["POST /api/orders (empty cart)", "400", "400"],
  ["DELETE /api/products/999", "404", "404"],
] as const;

const PAINS = [
  ["Nobody knows what the old app really does", "Parity maps it first. Every business rule it finds cites a file and a line, and is checked against your own docs.", "map"],
  ["A rewrite changes behaviour and nobody notices", "The same recorded cases run against the old and the new app, and every difference is listed before you cut over.", "diff"],
  ["A big-bang cut-over is a gamble", "Move one approved slice at a time, with a router and a rollback for every step.", "slices"],
  ["The bill is unknown until the project is over", "Each run is estimated first and capped with a hard budget that stops it gracefully.", "budget"],
] as const;

const SECURITY: readonly (readonly [string, string, IconKind])[] = [
  ["Your repository is never touched", "Source is copied into a project. The migrated app is written to its own folder.", "repo"],
  ["Analysis never runs your code", "The model reads a path-jailed, read-only copy. It has no shell and no network tool.", "noexec"],
  ["Generated code runs only in a sandbox", "Locked-down Docker containers: tests with no network, builds only through the mirror you allow.", "sandbox"],
  ["Secrets are redacted at ingest", "Credential files are withheld and secret patterns removed before anything reaches a model.", "lock"],
  ["You approve every gate", "The map, each slice and the cut-over need a person's sign-off, and an approval goes stale if the evidence changes.", "approve"],
  ["Spend is capped", "A hard budget stops a run gracefully, and re-running unchanged code makes no model calls.", "gauge"],
];

const WHO = [
  ["Platform and engineering leads", "Your Java 8, Django 3 or aging Node service is a risk, and a rewrite you cannot verify is a bigger one."],
  ["Agencies and consultancies", "Deliver modernisation projects with a defensible, case-by-case record of what changed and what did not."],
  ["Teams who inherited an app", "Nobody who wrote it is left. Get a cited map of what it does before anyone touches it."],
] as const;

const RESULTS: readonly { to: number; pre?: string; post?: string; dec?: number; text: string }[] = [
  { to: 24, post: " / 24", text: "recorded cases matched between the old and the new Express app in the sandbox trial; a deliberately changed status code was caught." },
  { to: 90, text: "fuzz variants compared between old and new in that trial, with no difference on identical code." },
  { to: 100, post: "%", text: "of cited evidence is checked against a real file and line range before it can steer the migration." },
  { to: 0.077, pre: "$", dec: 3, text: "to map Spring PetClinic (4,297 lines) in 51 seconds, with a hard budget that stops a run gracefully." },
  { to: 5, text: "migration paths covered: Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5, Express to Fastify, JavaScript to Go." },
  { to: 0, text: "changes to your repository. Your source is copied; the new app is written to its own folder." },
];

const COMPARE = [
  ["Do we know what the app does?", "Reverse-engineered from memory and stale docs.", "A map where every claim cites a file and line, checked against your own docs."],
  ["Did anything change in behaviour?", "Found by users, after release.", "Every difference between old and new listed, case by case, before cut-over."],
  ["Can we move gradually?", "Often a big-bang switch.", "Slice by slice, with a router and a rollback per step."],
  ["What will it cost?", "Unknown until it is over.", "Estimated before each run, capped with a hard budget."],
  ["Who decides?", "Whoever merges last.", "You approve the map, every slice and the cut-over."],
] as const;

const VERS: Record<string, readonly (readonly [string, string])[]> = {
  "java-spring": [["Java 8", "Java 21"], ["Spring Boot 2", "Spring Boot 3"]],
  django: [["Django 3", "Django 5"], ["Python 3.8", "Python 3.12"]],
  "express-fastify": [["Express", "Fastify"]],
  "js-go": [["JavaScript", "Go"]],
};

const NAV = [["#why", "Why Parity"], ["#how", "How it works"], ["#stacks", "Stacks"], ["#results", "Results"], ["#security", "Security"], ["#faq", "FAQ"]] as const;

export default function Landing() {
  return (
    <div className="min-h-dvh">
      <ScrollProgress />
      {jsonLd().map((d, i) => (
        <script key={i} type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(d).replace(/</g, "\\u003c") }} />
      ))}
      <header className="sticky top-0 z-30 border-b border-line bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-3 px-4 sm:px-6">
          <Logo />
          <nav aria-label="Primary" className="flex items-center gap-1">
            {NAV.map(([h, l]) => (
              <a key={h} href={h} className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 lg:block">{l}</a>
            ))}
            <ThemeToggle />
            <Link href="/login" className="rounded-lg px-3 py-2 text-sm font-medium hover:bg-surface-2">Log in</Link>
            <LinkButton href="/signup" variant="primary" className="hidden sm:inline-flex">Get early access</LinkButton>
          </nav>
        </div>
      </header>

      <main id="main">
        <section className="dotgrid relative overflow-hidden border-b border-line">
          <HeroScene />
          <div className="relative z-10 mx-auto grid max-w-6xl gap-12 px-4 pb-24 pt-14 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-24">
            <div>
              <p className="eyebrow rise mb-5">Legacy application migration and modernisation</p>
              <h1 className="display rise rise-1 text-[3.1rem] sm:text-7xl">
                Migrate legacy apps. <em className="text-accent">Prove parity.</em>
              </h1>
              <p className="rise rise-2 mt-6 max-w-xl text-lg text-muted">
                Upgrade to the latest version or move to a new stack, one slice at a time. Parity records how your running app behaves and shows, case by case, that the new one answers the same way.
              </p>
              <div className="rise rise-3 mt-8 flex flex-wrap gap-3">
                <LinkButton href="/signup" variant="primary" className="min-h-12 px-6 text-base">Get early access</LinkButton>
                <LinkButton href="#how" className="min-h-12 px-6 text-base">See how it works</LinkButton>
              </div>
              <ul className="rise rise-4 mt-7 space-y-1.5 text-sm text-muted" aria-label="Guarantees">
                {["Your repository is never modified", "Analysis never executes your code", "You approve the map, every slice and the cut-over"].map((t) => (
                  <li key={t} className="flex items-center gap-2"><span className="text-accent" aria-hidden="true">{"✓"}</span>{t}</li>
                ))}
              </ul>
              <ul className="rise rise-4 mt-6 flex flex-wrap gap-1.5" aria-label="Supported migrations">
                {TARGETS.map((t) => (
                  <li key={t} className="rounded-full border border-line-strong bg-surface px-3 py-1 font-mono text-xs text-muted">{t}</li>
                ))}
              </ul>
              <p className="rise rise-4 mt-4 font-mono text-xs text-muted">Early access, beta. Tried on three reference apps: Spring, Django and Express.</p>
            </div>

            <figure className="rise rise-2" aria-label="An illustrative migration: a legacy app becomes a new app, and the two are compared">
              <Tilt3D>
                <div className="rounded-2xl border border-line-strong bg-surface p-5 shadow-[var(--shadow)]">
                  <p className="eyebrow">Illustrative example</p>
                  <div className="mt-4 grid items-center gap-3 sm:grid-cols-[1fr_auto_1fr]">
                    <div className="rounded-xl border border-line p-4">
                      <p className="font-mono text-[11px] uppercase text-muted">Legacy app</p>
                      <p className="mt-1 font-semibold">orders-service</p>
                      <p className="font-mono text-xs text-muted">Java 8 · Spring Boot 2</p>
                    </div>
                    <span className="mx-auto font-mono text-xl text-accent" aria-hidden="true">{"→"}</span>
                    <div className="rounded-xl border border-accent bg-accent-soft p-4" style={{ transform: "translateZ(30px)" }}>
                      <p className="font-mono text-[11px] uppercase text-accent">New app</p>
                      <p className="mt-1 font-semibold">orders-service</p>
                      <p className="font-mono text-xs text-muted">Java 21 · Spring Boot 3</p>
                    </div>
                  </div>
                  <div className="mt-5 overflow-hidden rounded-xl border border-line" style={{ transform: "translateZ(18px)" }}>
                    <div className="grid grid-cols-[1fr_auto_auto_auto] gap-x-4 border-b border-line bg-surface-2 px-4 py-2 font-mono text-[11px] uppercase text-muted">
                      <span>Recorded case</span><span>Old</span><span>New</span><span aria-label="Result">=</span>
                    </div>
                    {PROOF.map(([c, a, b]) => (
                      <div key={c} className="grid grid-cols-[1fr_auto_auto_auto] gap-x-4 border-b border-line px-4 py-2 font-mono text-xs last:border-0">
                        <span className="truncate">{c}</span><span>{a}</span><span>{b}</span><span className="text-ok">{"✓"}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </Tilt3D>
              <figcaption className="mt-3 pl-1 text-xs text-muted">The old app&apos;s own answers are recorded, never written by the model. A slice moves on only when the new app matches.</figcaption>
            </figure>
          </div>
        </section>

        <section id="why" aria-labelledby="why-h" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <Reveal3D>
            <p className="eyebrow mb-3">Why Parity</p>
            <h2 id="why-h" className="display max-w-3xl text-4xl sm:text-5xl">Legacy migrations fail quietly. Parity makes every difference visible.</h2>
          </Reveal3D>
          <div className="mt-10 grid gap-4 md:grid-cols-2">
            {PAINS.map(([p, a, kind], i) => (
              <Reveal3D key={p} delay={i * 0.08}>
                <article className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                  <p className="font-mono text-xs uppercase text-danger">The problem</p>
                  <h3 className="mt-2 text-lg font-semibold">{p}</h3>
                  <p className="mt-3 font-mono text-xs uppercase text-accent">With Parity</p>
                  <p className="mt-1 text-sm text-muted">{a}</p>
                  <PainViz kind={kind} />
                </article>
              </Reveal3D>
            ))}
          </div>
        </section>

        <section id="how" aria-labelledby="how-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <Reveal3D>
              <p className="eyebrow mb-3">How it works</p>
              <h2 id="how-h" className="display max-w-3xl text-4xl sm:text-5xl">Five steps from the app you have to the app you want.</h2>
              <p className="mt-4 max-w-2xl text-muted">You cannot migrate safely what you cannot describe, so step 1 maps the legacy app. Everything after it is built on that approved map.</p>
            </Reveal3D>
            <Reveal3D className="mt-10" delay={0.1}><MigrationJourney level={3} animate /></Reveal3D>
            <div className="mt-6 grid gap-4 md:grid-cols-2">
              <Reveal3D>
                <div className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                  <p className="eyebrow mb-2">Why the map comes first</p>
                  <p className="text-sm text-muted">The plan, the test cases and the review of every generated slice point back to items in this map. Each item cites a file and a line, so a claim that cannot be checked is dropped before it can steer the migration.</p>
                </div>
              </Reveal3D>
              <Reveal3D delay={0.08}>
                <div className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                  <p className="eyebrow mb-2">Why recorded behaviour decides</p>
                  <p className="text-sm text-muted">The old app is the oracle. Test inputs are run against it twice, noise is masked, and its answers become the expected result. Where you decided to fix a bug instead of keeping it, you approve the corrected expectation.</p>
                </div>
              </Reveal3D>
            </div>
          </div>
        </section>

        <section id="stacks" aria-labelledby="stacks-h" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <Reveal3D>
            <p className="eyebrow mb-3">Supported stacks</p>
            <h2 id="stacks-h" className="display max-w-3xl text-4xl sm:text-5xl">Upgrade to the latest version, or move to a different stack.</h2>
            <p className="mt-4 max-w-2xl text-muted">Deterministic tools do the mechanical rewrite first, so the AI model is left only with the parts that need judgement, and every result is compared with the running app.</p>
          </Reveal3D>
          <div className="mt-10 grid gap-4 md:grid-cols-2">
            {PATHS.map((p, i) => (
              <Reveal3D key={p.id} delay={i * 0.08}>
                <article id={p.id} className="card3d h-full rounded-2xl border border-line-strong bg-surface p-6">
                  <h3 className="display text-2xl">{p.name}</h3>
                  <p className="mt-2 text-sm text-muted">{p.text}</p>
                  <div className="ver" aria-hidden="true">
                    {(VERS[p.id] ?? []).map(([a, b], n) => (
                      <p key={a} className="line" style={{ "--p": n } as React.CSSProperties}><span className="old">{a}</span><span className="arrow">{"→"}</span><span className="new">{b}</span></p>
                    ))}
                  </div>
                </article>
              </Reveal3D>
            ))}
          </div>
          <p className="mt-6 text-sm text-muted">Need a different stack? {CONTACT_EMAIL ? <>Tell us at <a className="underline" href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.</> : "Tell us when you create your workspace."} Other moves are not covered yet.</p>
        </section>

        <section id="results" aria-labelledby="results-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <Reveal3D>
              <p className="eyebrow mb-3">Results</p>
              <h2 id="results-h" className="display max-w-3xl text-4xl sm:text-5xl">Less guesswork, no silent regressions, and a bill you control.</h2>
              <p className="mt-4 max-w-2xl text-muted">Migrations fail quietly: behaviour drifts and nobody notices until production. Here is what changes, with the numbers we have actually measured.</p>
            </Reveal3D>
            <dl className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {RESULTS.map((r, i) => (
                <Reveal3D key={r.text} delay={(i % 3) * 0.08}>
                  <div className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                    <dt className="display text-5xl tnum text-accent"><CountUp to={r.to} pre={r.pre} post={r.post} decimals={r.dec} /></dt>
                    <dd className="mt-2 text-sm text-muted">{r.text}</dd>
                  </div>
                </Reveal3D>
              ))}
            </dl>
            <p className="mt-4 max-w-2xl text-xs text-muted">Measured on our reference apps with no AI model in the migration steps. Treat these as a regression guide, not a benchmark. Accuracy on large real repositories is not measured yet.</p>

            <Reveal3D className="mt-14">
              <h3 className="display text-3xl">Rewriting by hand versus migrating with Parity</h3>
              <div className="mt-5 overflow-x-auto rounded-2xl border border-line bg-surface">
                <table className="cmp w-full min-w-[640px] text-left text-sm">
                  <caption className="sr-only">Manual legacy rewrite compared with Parity</caption>
                  <thead className="border-b border-line bg-surface-2 font-mono text-[11px] uppercase text-muted"><tr><th scope="col" className="px-4 py-3 font-normal">Question</th><th scope="col" className="px-4 py-3 font-normal">Manual rewrite</th><th scope="col" className="px-4 py-3 font-normal text-accent">Parity</th></tr></thead>
                  <tbody>
                    {COMPARE.map(([q, a, b], i) => (
                      <tr key={q} style={{ "--i": i } as React.CSSProperties} className="border-b border-line last:border-0"><th scope="row" className="px-4 py-3 font-medium">{q}</th><td className="px-4 py-3 text-muted">{a}</td><td className="px-4 py-3">{b}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Reveal3D>
          </div>
        </section>

        <section aria-labelledby="map-h" className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
          <Reveal3D>
            <p className="eyebrow mb-3">Step 1, in detail</p>
            <h2 id="map-h" className="display max-w-3xl text-4xl sm:text-5xl">A map of what the app does that you can check, not a summary you have to trust.</h2>
          </Reveal3D>
          <div className="mt-12 grid gap-12 lg:grid-cols-2">
            <Reveal3D>
              <figure aria-label="A claim linked to the lines of code that prove it">
                <Tilt3D max={5}>
                  <div className="rounded-2xl border border-line-strong bg-surface p-5 shadow-[var(--shadow)]">
                    <div className="flex items-center justify-between gap-3">
                      <span className="inline-flex items-center gap-2 text-sm font-medium"><TypeGlyph type="business_rule" size={16} />Waitlist promotion</span>
                      <ConfidenceMeter value="medium" />
                    </div>
                    <p className="mt-2 text-sm text-muted">When a non-waitlisted registration is cancelled, the first waitlisted person moves to active.</p>
                    <div className="mt-4 flex items-center gap-2 font-mono text-xs text-accent">
                      <span aria-hidden="true">{"└─"}</span> library/services.py:18-22
                    </div>
                  </div>
                </Tilt3D>
                <div className="quote ml-6 mt-4 overflow-hidden sm:ml-12">
                  <div className="px-4 py-3 font-mono text-[12.5px] leading-6">
                    {CODE.map(([n, line]) => (
                      <div key={n} style={{ "--i": n - 18 } as React.CSSProperties} className={`flex gap-4 ${n >= 18 ? "cite-line bg-accent-soft -mx-4 px-4" : "text-muted"}`}>
                        <span className="w-5 select-none text-right opacity-60">{n}</span>
                        <code className="whitespace-pre">{line}</code>
                      </div>
                    ))}
                  </div>
                </div>
                <figcaption className="mt-3 pl-1 text-xs text-muted">Every business rule the migration must preserve points at real lines.</figcaption>
              </figure>
            </Reveal3D>
            <Reveal3D delay={0.1}>
              <div className="card3d space-y-3 rounded-2xl border border-line-strong bg-surface p-5">
                <p className="eyebrow">Catching a wrong requirement before it is migrated</p>
                <div className="flex items-center justify-between"><strong>Owner search page size</strong><span className="sev rounded bg-danger-soft px-2 py-0.5 font-mono text-[11px] font-semibold uppercase text-danger">high</span></div>
                <blockquote style={{ "--i": 0 } as React.CSSProperties} className="qL quote px-4 py-2 text-sm"><span className="eyebrow block">docs/user-guide.md:41</span>Search results for owners are displayed 10 per page.</blockquote>
                <blockquote style={{ "--i": 1 } as React.CSSProperties} className="qR quote px-4 py-2"><span className="eyebrow block">OwnerController.java:122</span><code className="font-mono text-[12.5px]">int pageSize = 5;</code></blockquote>
                <p className="text-sm text-muted">A Verifier that never sees your code compares the map with your own docs. You decide which is right, and the migration then preserves the right behaviour.</p>
              </div>
            </Reveal3D>
          </div>
        </section>

        <section id="security" aria-labelledby="security-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <Reveal3D>
              <p className="eyebrow mb-3">Security and control</p>
              <h2 id="security-h" className="display max-w-3xl text-4xl sm:text-5xl">Built for teams that cannot afford a surprise.</h2>
            </Reveal3D>
            <div className="mt-10 grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {SECURITY.map(([t, d, icon], i) => (
                <Reveal3D key={t} delay={(i % 3) * 0.08}>
                  <article className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                    <SecurityIcon kind={icon} i={i} />
                    <h3 className="font-semibold">{t}</h3>
                    <p className="mt-2 text-sm text-muted">{d}</p>
                  </article>
                </Reveal3D>
              ))}
            </div>
            <Reveal3D className="mt-6">
              <div className="rounded-2xl border border-line-strong bg-surface p-6">
                <p className="eyebrow mb-2">What we do not claim</p>
                <p className="text-sm text-muted">Parity is not certified or audited. Excerpts of your code, redacted on a best-effort basis, are sent to the AI model provider during analysis, and there is no zero-retention agreement with a provider yet. No migration step has run with a real AI model so far, so the quality of model-written code is unmeasured. Read the <Link href="/privacy" className="underline">privacy page</Link> for what is stored and how deletion works.</p>
              </div>
            </Reveal3D>
          </div>
        </section>

        <section id="who" aria-labelledby="who-h" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <Reveal3D>
            <p className="eyebrow mb-3">Who it is for</p>
            <h2 id="who-h" className="display max-w-3xl text-4xl sm:text-5xl">For the people who own the app nobody wants to touch.</h2>
          </Reveal3D>
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            {WHO.map(([t, d], i) => (
              <Reveal3D key={t} delay={i * 0.08}>
                <article className="card3d h-full rounded-2xl border border-line bg-surface p-6">
                  <span className="orb" aria-hidden="true" />
                  <h3 className="display text-2xl">{t}</h3>
                  <p className="mt-2 text-sm text-muted">{d}</p>
                </article>
              </Reveal3D>
            ))}
          </div>
        </section>

        <section id="access" aria-labelledby="access-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <Reveal3D>
              <p className="eyebrow mb-3">Early access</p>
              <h2 id="access-h" className="display max-w-3xl text-4xl sm:text-5xl">Start in minutes. See your legacy app mapped today.</h2>
              <p className="mt-4 max-w-2xl text-muted">Parity is in early access. Billing is not enabled yet, so workspaces run on the internal plan with a hard spending cap. The first step runs here; approval and migration run in the command-line tool.</p>
            </Reveal3D>
            <Reveal3D className="mt-10"><ol className="grid gap-4 md:grid-cols-3">
              {[
                ["1", "Create a workspace", "Sign up and connect your legacy app: a public git URL or an archive."],
                ["2", "Get the map", "See the estimated cost first, then read the cited map and review the findings."],
                ["3", "Approve and migrate", "Continue in the command-line tool: approve, migrate slice by slice, prove, cut over."],
              ].map(([n, t, d], i) => (
                <li key={n} className="astep card3d relative rounded-2xl border border-line bg-surface p-6" style={{ "--i": i } as React.CSSProperties}>
                  <span className="anum font-mono text-sm text-accent">{n}</span>
                  <h3 className="display mt-4 text-2xl">{t}</h3>
                  <p className="mt-2 text-sm text-muted">{d}</p>
                </li>
              ))}
            </ol></Reveal3D>
            <Reveal3D className="mt-8 flex flex-wrap items-center gap-3">
              <LinkButton href="/signup" variant="primary" className="min-h-12 px-8 text-base">Get early access</LinkButton>
              {CONTACT_EMAIL && <a href={`mailto:${CONTACT_EMAIL}`} className="inline-flex min-h-12 items-center rounded-lg border border-line-strong px-6 text-base hover:bg-surface">Talk to us</a>}
            </Reveal3D>
          </div>
        </section>

        <section id="faq" aria-labelledby="faq-h" className="mx-auto max-w-4xl scroll-mt-20 px-4 py-20 sm:px-6">
          <Reveal3D>
            <p className="eyebrow mb-3">FAQ</p>
            <h2 id="faq-h" className="display text-4xl sm:text-5xl">Questions about migrating a legacy app</h2>
          </Reveal3D>
          <div className="mt-8 divide-y divide-line rounded-2xl border border-line bg-surface">
            {FAQ.map((f) => (
              <details key={f.q} className="group px-5 py-4">
                <summary className="cursor-pointer list-none font-medium"><h3 className="inline text-base">{f.q}</h3></summary>
                <p className="mt-2 text-sm text-muted">{f.a}</p>
              </details>
            ))}
          </div>
        </section>

        <section className="px-4 pb-24 sm:px-6">
          <div className="relative mx-auto max-w-6xl overflow-hidden rounded-3xl bg-ink px-6 py-14 text-center text-bg sm:px-14">
            <div className="aurora" aria-hidden="true"><i /><i /></div>
            <h2 className="display relative text-4xl sm:text-6xl">Move the app you have to the stack you want.</h2>
            <p className="relative mx-auto mt-3 max-w-lg opacity-75">Create a workspace, connect your legacy app and get the cited map. Early access is free while billing is not enabled.</p>
            <LinkButton href="/signup" variant="primary" className="shine relative mt-8 min-h-12 px-8 text-base">Get early access</LinkButton>
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto grid max-w-6xl gap-8 px-4 py-12 text-sm sm:grid-cols-2 sm:px-6 lg:grid-cols-4">
          <div className="space-y-3">
            <Logo />
            <p className="max-w-xs text-muted">Migrate legacy apps. Prove parity.</p>
          </div>
          <nav aria-label="Product" className="space-y-2">
            <p className="eyebrow">Product</p>
            {[["#why", "Why Parity"], ["#how", "How it works"], ["#stacks", "Supported stacks"], ["#results", "Results"]].map(([h, l]) => <a key={h} href={h} className="block text-muted hover:text-ink">{l}</a>)}
          </nav>
          <nav aria-label="Trust" className="space-y-2">
            <p className="eyebrow">Trust</p>
            <a href="#security" className="block text-muted hover:text-ink">Security and control</a>
            <Link href="/privacy" className="block text-muted hover:text-ink">Privacy</Link>
            <Link href="/terms" className="block text-muted hover:text-ink">Terms</Link>
          </nav>
          <nav aria-label="Account" className="space-y-2">
            <p className="eyebrow">Get started</p>
            <Link href="/signup" className="block text-muted hover:text-ink">Get early access</Link>
            <Link href="/login" className="block text-muted hover:text-ink">Log in</Link>
            {CONTACT_EMAIL && <a href={`mailto:${CONTACT_EMAIL}`} className="block text-muted hover:text-ink">Contact</a>}
          </nav>
        </div>
        <div className="border-t border-line px-4 py-5 text-center text-xs text-muted">Early access. Your source is read in a sandbox, secrets are redacted at ingest, and migration code runs only in locked-down Docker containers.</div>
      </footer>
    </div>
  );
}
