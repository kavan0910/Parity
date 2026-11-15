import type { Metadata } from "next";
import Link from "next/link";
import { Logo, ThemeToggle } from "@/components/Brand";
import { ConfidenceMeter, LinkButton, TypeGlyph } from "@/components/ui";
import { MigrationJourney, TARGETS } from "@/components/MigrationJourney";
import { FAQ, HOME_DESCRIPTION, HOME_TITLE, PATHS, jsonLd } from "@/lib/site";

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

export default function Landing() {
  return (
    <div className="min-h-dvh">
      {jsonLd().map((d, i) => (
        <script key={i} type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(d).replace(/</g, "\\u003c") }} />
      ))}
      <header className="sticky top-0 z-30 border-b border-line bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-3 px-4 sm:px-6">
          <Logo />
          <nav aria-label="Primary" className="flex items-center gap-1">
            <a href="#mvp" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">The MVP</a>
            <a href="#how" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">How it works</a>
            <a href="#paths" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">Migrations</a>
            <a href="#faq" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">FAQ</a>
            <a href="#status" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">What you get</a>
            <a href="#honest" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">Measured</a>
            <ThemeToggle />
            <Link href="/login" className="rounded-lg px-3 py-2 text-sm font-medium hover:bg-surface-2">Log in</Link>
            <LinkButton href="/signup" variant="primary" className="hidden sm:inline-flex">Start a migration</LinkButton>
          </nav>
        </div>
      </header>

      <main id="main">
        <section className="dotgrid border-b border-line">
          <div className="mx-auto grid max-w-6xl gap-12 px-4 pb-20 pt-14 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-24">
            <div>
              <p className="eyebrow rise mb-5">Legacy application migration and modernisation</p>
              <h1 className="display rise rise-1 text-[3.1rem] sm:text-7xl">
                Upgrade or replatform your <em className="text-accent">legacy app</em>, and prove it still works the same.
              </h1>
              <p className="rise rise-2 mt-6 max-w-xl text-lg text-muted">
                Migration Agent takes the application you already run and migrates it, one slice at a time, to a current version of its stack or to a different technology.
                It records how your running app behaves and shows, case by case, that the new one answers the same way.
              </p>
              <div className="rise rise-3 mt-8 flex flex-wrap gap-3">
                <LinkButton href="/signup" variant="primary" className="min-h-12 px-6 text-base">Start a migration</LinkButton>
                <LinkButton href="/login" className="min-h-12 px-6 text-base">Log in</LinkButton>
              </div>
              <ul className="rise rise-4 mt-6 flex flex-wrap gap-1.5" aria-label="Supported migrations">
                {TARGETS.map((t) => (
                  <li key={t} className="rounded-full border border-line-strong bg-surface px-3 py-1 font-mono text-xs text-muted">{t}</li>
                ))}
              </ul>
              <p className="rise rise-4 mt-4 font-mono text-xs text-muted">Beta. Tried on three reference apps: Spring, Django and Express.</p>
            </div>

            <figure className="rise rise-2" aria-label="An illustrative migration: a legacy app becomes a new app, and the two are compared">
              <div className="rounded-2xl border border-line-strong bg-surface p-5 shadow-[var(--shadow)]">
                <p className="eyebrow">Illustrative example</p>
                <div className="mt-4 grid items-center gap-3 sm:grid-cols-[1fr_auto_1fr]">
                  <div className="rounded-xl border border-line p-4">
                    <p className="font-mono text-[11px] uppercase text-muted">Legacy app</p>
                    <p className="mt-1 font-semibold">orders-service</p>
                    <p className="font-mono text-xs text-muted">Java 8 · Spring Boot 2</p>
                  </div>
                  <span className="mx-auto font-mono text-xl text-accent" aria-hidden="true">{"→"}</span>
                  <div className="rounded-xl border border-accent bg-accent-soft p-4">
                    <p className="font-mono text-[11px] uppercase text-accent">New app</p>
                    <p className="mt-1 font-semibold">orders-service</p>
                    <p className="font-mono text-xs text-muted">Java 21 · Spring Boot 3</p>
                  </div>
                </div>
                <div className="mt-5 overflow-hidden rounded-xl border border-line">
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
              <figcaption className="mt-3 pl-1 text-xs text-muted">The old app's own answers are recorded, never written by the model. A slice moves on only when the new app matches.</figcaption>
            </figure>
          </div>
        </section>

        <section id="mvp" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">The MVP</p>
          <h2 className="display max-w-3xl text-4xl sm:text-5xl">A legacy app goes in. A migrated app, and the proof, come out.</h2>
          <div className="mt-10 grid gap-px overflow-hidden rounded-2xl border border-line bg-line md:grid-cols-3">
            {[
              ["In", "Your legacy app", "A git repository or an archive of the application you run today. It is copied; your repository is never touched."],
              ["Choose", "The target", "A newer version of the same stack, or a different technology: Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5, Express to Fastify, JavaScript to Go."],
              ["Out", "The migrated app and the proof", "New code in its own folder, an equivalence report against the running legacy app, and a cut-over plan you approve. Nothing is deployed for you."],
            ].map(([k, t, d]) => (
              <div key={k} className="bg-surface p-7">
                <span className="font-mono text-sm text-accent">{k}</span>
                <h3 className="display mt-5 text-3xl">{t}</h3>
                <p className="mt-2 text-muted">{d}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="how" aria-labelledby="how-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <p className="eyebrow mb-3">How the migration works</p>
            <h2 className="display max-w-3xl text-4xl sm:text-5xl">Five steps from the app you have to the app you want.</h2>
            <p className="mt-4 max-w-2xl text-muted">Understanding the legacy app is step 1, not the goal: you cannot migrate safely what you cannot describe. Everything after it is built on that approved map.</p>
            <MigrationJourney level={3} className="mt-10" />
            <div className="mt-6 grid gap-4 md:grid-cols-2">
              <div className="rounded-2xl border border-line bg-surface p-6">
                <p className="eyebrow mb-2">Why the map comes first</p>
                <p className="text-sm text-muted">The migration plan, the test cases and the review of every generated slice all point back to items in this map. Each item cites a file and a line, so a claim that cannot be checked is dropped before it can steer the migration.</p>
              </div>
              <div className="rounded-2xl border border-line bg-surface p-6">
                <p className="eyebrow mb-2">Why recorded behaviour decides</p>
                <p className="text-sm text-muted">The old app is the oracle. Test inputs are run against it twice, noise is masked, and its answers become the expected result. Where you decided to fix a bug instead of keeping it, you approve the corrected expectation.</p>
              </div>
            </div>
          </div>
        </section>

        <section id="impact" aria-labelledby="impact-h" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">Impact</p>
          <h2 id="impact-h" className="display max-w-3xl text-4xl sm:text-5xl">Less guesswork, no silent regressions, and a bill you control.</h2>
          <p className="mt-4 max-w-2xl text-muted">Migrations fail quietly: behaviour drifts and nobody notices until production. Here is what changes, with the numbers we have actually measured.</p>
          <dl className="mt-10 grid gap-px overflow-hidden rounded-2xl border border-line bg-line sm:grid-cols-2 lg:grid-cols-3">
            {[
              ["24 / 24", "recorded cases matched between the old and the new Express app in the sandbox trial; a deliberately changed status code was caught."],
              ["90", "fuzz variants compared between old and new in that trial, with no difference on identical code."],
              ["100%", "of cited evidence is checked against a real file and line range before it can steer the migration."],
              ["$0.077", "to map Spring PetClinic (4,297 lines) in 51 seconds, with a hard budget that stops a run gracefully."],
              ["5", "migration paths covered: Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5, Express to Fastify, JavaScript to Go."],
              ["0", "changes to your repository. Your source is copied; the new app is written to its own folder."],
            ].map(([v, l]) => (
              <div key={l} className="bg-surface p-6">
                <dt className="display text-5xl tnum text-accent">{v}</dt>
                <dd className="mt-2 text-sm text-muted">{l}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-4 max-w-2xl text-xs text-muted">Measured on our reference apps with no AI model in the migration steps. Treat these as a regression guide, not a benchmark. Accuracy on large real repositories is not measured yet.</p>

          <h3 className="display mt-14 text-3xl">Rewriting by hand versus migrating with Migration Agent</h3>
          <div className="mt-5 overflow-x-auto rounded-2xl border border-line">
            <table className="w-full min-w-[640px] text-left text-sm">
              <caption className="sr-only">Manual legacy rewrite compared with Migration Agent</caption>
              <thead className="border-b border-line bg-surface-2 font-mono text-[11px] uppercase text-muted"><tr><th scope="col" className="px-4 py-3 font-normal">Question</th><th scope="col" className="px-4 py-3 font-normal">Manual rewrite</th><th scope="col" className="px-4 py-3 font-normal text-accent">Migration Agent</th></tr></thead>
              <tbody>
                {[
                  ["Do we know what the app does?", "Reverse-engineered from memory and stale docs.", "A map where every claim cites a file and line, checked against your own docs."],
                  ["Did anything change in behaviour?", "Found by users, after release.", "Every difference between old and new listed, case by case, before cut-over."],
                  ["Can we move gradually?", "Often a big-bang switch.", "Slice by slice, with a router and a rollback per step."],
                  ["What will it cost?", "Unknown until it is over.", "Estimated before each run, capped with a hard budget."],
                  ["Who decides?", "Whoever merges last.", "You approve the map, every slice and the cut-over."],
                ].map(([q, a, b]) => (
                  <tr key={q} className="border-b border-line last:border-0"><th scope="row" className="px-4 py-3 font-medium">{q}</th><td className="px-4 py-3 text-muted">{a}</td><td className="px-4 py-3">{b}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section id="paths" aria-labelledby="paths-h" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <p className="eyebrow mb-3">Migration paths</p>
            <h2 id="paths-h" className="display max-w-3xl text-4xl sm:text-5xl">Upgrade to the latest version, or move to a different stack.</h2>
            <p className="mt-4 max-w-2xl text-muted">Deterministic tools do the mechanical rewrite first, so the AI model is left only with the parts that need judgement, and every result is compared with the running app.</p>
            <div className="mt-10 grid gap-4 md:grid-cols-2">
              {PATHS.map((p) => (
                <article key={p.id} id={p.id} className="rounded-2xl border border-line-strong bg-surface p-6">
                  <h3 className="display text-2xl">{p.name}</h3>
                  <p className="mt-2 text-sm text-muted">{p.text}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section aria-labelledby="step1-h" className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">Step 1 in detail: understand the legacy app</p>
          <h2 id="step1-h" className="display max-w-3xl text-4xl sm:text-5xl">A map of what the app does that you can check, not a summary you have to trust.</h2>
          <div className="mt-12 grid gap-12 lg:grid-cols-2">
            <figure aria-label="A claim linked to the lines of code that prove it">
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
              <div className="quote ml-6 mt-4 overflow-hidden sm:ml-12">
                <div className="px-4 py-3 font-mono text-[12.5px] leading-6">
                  {CODE.map(([n, line]) => (
                    <div key={n} className={`flex gap-4 ${n >= 18 ? "bg-accent-soft -mx-4 px-4" : "text-muted"}`}>
                      <span className="w-5 select-none text-right opacity-60">{n}</span>
                      <code className="whitespace-pre">{line}</code>
                    </div>
                  ))}
                </div>
              </div>
              <figcaption className="mt-3 pl-1 text-xs text-muted">Every business rule the migration must preserve points at real lines.</figcaption>
            </figure>
            <div className="space-y-3 rounded-2xl border border-line-strong bg-surface p-5">
              <p className="eyebrow">Catching a wrong requirement before it is migrated</p>
              <div className="flex items-center justify-between"><strong>Owner search page size</strong><span className="rounded bg-danger-soft px-2 py-0.5 font-mono text-[11px] font-semibold uppercase text-danger">high</span></div>
              <blockquote className="quote px-4 py-2 text-sm"><span className="eyebrow block">docs/user-guide.md:41</span>Search results for owners are displayed 10 per page.</blockquote>
              <blockquote className="quote px-4 py-2"><span className="eyebrow block">OwnerController.java:122</span><code className="font-mono text-[12.5px]">int pageSize = 5;</code></blockquote>
              <p className="text-sm text-muted">A Verifier that never sees your code compares the map with your own docs. You decide which is right, and the migration then preserves the right behaviour.</p>
            </div>
          </div>
        </section>

        <section id="status" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <p className="eyebrow mb-3">What you get today</p>
            <h2 className="display max-w-3xl text-4xl sm:text-5xl">Where each step runs, and how far along it is.</h2>
            <div className="mt-10 grid gap-4 md:grid-cols-3">
              {[
                ["Available", "In this app", "Step 1: understand", "Connect the legacy app, read it, review findings, explore the graph, add your docs for the Verifier, and track cost and usage."],
                ["Available", "Command line", "Step 2: approve", "Record keep-or-fix decisions per quirk, check readiness, and sign off. The approval writes the migration contract."],
                ["Beta", "Command line, needs Docker", "Steps 3 to 5: migrate, prove, cut over", "Target fit, slices, recorded cases, sandboxed generation, equivalence, fuzzing, a strangler router and a cut-over plan. Run it on your own machine."],
              ].map(([badge, where, t, d]) => (
                <article key={t} className="rounded-2xl border border-line-strong bg-surface p-6">
                  <p className="font-mono text-xs uppercase text-accent">{badge} · {where}</p>
                  <h3 className="display mt-3 text-2xl">{t}</h3>
                  <p className="mt-2 text-sm text-muted">{d}</p>
                </article>
              ))}
            </div>
            <div className="mt-8 rounded-2xl border border-line-strong bg-surface p-6">
              <p className="eyebrow mb-2">Sandbox trial of the migration steps, no model</p>
              <p className="text-sm text-muted">
                On the Express sample, the sandbox built the old app, recorded 24 authenticated cases, and ran a copy as the new one:
                24 of 24 matched and 90 fuzz variants found no difference. A deliberately changed status code was caught.
                A second, subtler change (a price boundary) was <strong className="text-ink">not</strong> caught, because the offline cases never reach it.
                Model-written cases are meant to cover that, and they have not run yet.
              </p>
            </div>
          </div>
        </section>

        <section id="honest" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">Measured, including the weak parts</p>
          <h2 className="display max-w-3xl text-4xl sm:text-5xl">We publish what we can prove and what we cannot.</h2>
          <dl className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
            {[
              ["100%", "of cited evidence points to a real file and line range."],
              ["0", "prompt-injection canary strings found in generated graphs."],
              ["0.57", "F1 for edges on the Express sample. It is our weakest category and we say so."],
              ["0", "migration stages run with a real model so far. Accuracy on large real repositories is also unmeasured."],
            ].map(([v, l]) => (
              <div key={l} className="border-t-2 border-ink pt-4">
                <dt className="display text-6xl tnum">{v}</dt>
                <dd className="mt-2 text-muted">{l}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-8 max-w-2xl text-sm text-muted">Labels are small and hand written: treat these as a regression guide, not a benchmark. The first live run of the migration sandbox found seven bugs that every offline test had missed; all are fixed. Nothing here is certified or audited.</p>
        </section>

        <section id="faq" aria-labelledby="faq-h" className="mx-auto max-w-4xl scroll-mt-20 px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">FAQ</p>
          <h2 id="faq-h" className="display text-4xl sm:text-5xl">Questions about migrating a legacy app</h2>
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
          <div className="mx-auto max-w-6xl rounded-3xl bg-ink px-6 py-14 text-center text-bg sm:px-14">
            <h2 className="display text-4xl sm:text-6xl">Move the app you have to the stack you want.</h2>
            <p className="mx-auto mt-3 max-w-lg opacity-75">Billing is not enabled yet. Create a workspace and connect your legacy app under the internal plan. Approval and migration run in the command-line tool.</p>
            <LinkButton href="/signup" variant="primary" className="mt-8 min-h-12 px-8 text-base">Start a migration</LinkButton>
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-8 text-sm text-muted sm:px-6">
          <Logo />
          <p>Your source is read in a sandbox. Secrets are redacted at ingest. Migration code runs only in locked-down Docker containers.</p>
        </div>
      </footer>
    </div>
  );
}
