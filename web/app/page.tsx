import Link from "next/link";
import { Logo, ThemeToggle } from "@/components/Brand";
import { ConfidenceMeter, LinkButton, TypeGlyph } from "@/components/ui";

const CODE = [
  [16, "def cancel(registration):"],
  [17, "    workshop = registration.workshop"],
  [18, "    if not registration.waitlisted:"],
  [19, "        nxt = workshop.registrations.filter(waitlisted=True).first()"],
  [20, "        if nxt:"],
  [21, "            nxt.waitlisted = False"],
  [22, "            nxt.save()"],
] as const;

export default function Landing() {
  return (
    <div className="min-h-dvh">
      <header className="sticky top-0 z-30 border-b border-line bg-bg/85 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-3 px-4 sm:px-6">
          <Logo />
          <nav aria-label="Primary" className="flex items-center gap-1">
            <a href="#how" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">How it works</a>
            <a href="#status" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">What you get</a>
            <a href="#migrate" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">Migrate</a>
            <a href="#honest" className="hidden rounded-lg px-3 py-2 text-sm hover:bg-surface-2 sm:block">Measured</a>
            <ThemeToggle />
            <Link href="/login" className="rounded-lg px-3 py-2 text-sm font-medium hover:bg-surface-2">Log in</Link>
            <LinkButton href="/signup" variant="primary" className="hidden sm:inline-flex">Start free</LinkButton>
          </nav>
        </div>
      </header>

      <main id="main">
        <section className="dotgrid border-b border-line">
          <div className="mx-auto grid max-w-6xl gap-12 px-4 pb-20 pt-14 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-24">
            <div>
              <p className="eyebrow rise mb-5">Legacy service modernisation, with evidence at every step</p>
              <h1 className="display rise rise-1 text-[3.1rem] sm:text-7xl">
                Understand it, approve it, then migrate it <em className="text-accent">with proof</em>.
              </h1>
              <p className="rise rise-2 mt-6 max-w-xl text-lg text-muted">
                Migration Agent reads your legacy service in a read-only sandbox and writes an understanding document where every claim cites a file and a line.
                You approve it. Then it migrates the service one slice at a time and checks each slice against the responses your running legacy service actually gives.
              </p>
              <div className="rise rise-3 mt-8 flex flex-wrap gap-3">
                <LinkButton href="/signup" variant="primary" className="min-h-12 px-6 text-base">Create a workspace</LinkButton>
                <LinkButton href="/login" className="min-h-12 px-6 text-base">Log in</LinkButton>
              </div>
              <p className="rise rise-4 mt-5 font-mono text-xs text-muted">Understand and approve run here. Migration is a command-line beta. Tried on three reference repos: Spring, Django and Express.</p>
            </div>

            <figure className="rise rise-2 relative" aria-label="A claim linked to the lines of code that prove it">
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
              <figcaption className="mt-3 pl-1 text-xs text-muted">A claim that cannot point at real lines is dropped before it reaches the graph.</figcaption>
            </figure>
          </div>
        </section>

        <section id="how" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
          <p className="eyebrow mb-3">How it works</p>
          <h2 className="display max-w-3xl text-4xl sm:text-5xl">Two phases, one folder of state, a human sign-off between them.</h2>
          <p className="mt-4 max-w-2xl text-muted">Phase 1 produces a map you can check. You approve it. Phase 2 uses that approved map as the contract for the migration.</p>

          <h3 className="eyebrow mt-12">Phase 1: understand and approve</h3>
          <ol className="mt-4 grid gap-px overflow-hidden rounded-2xl border border-line bg-line md:grid-cols-4">
            {[
              ["01", "Connect", "Paste a public git URL or upload an archive. You see the estimated cost first, and a hard budget stops a run gracefully."],
              ["02", "Read", "Static extraction finds routes, entities and config. Models add the business rules. Evidence that does not exist in the code is rejected."],
              ["03", "Verify", "A blind Verifier compares the document with your own docs and flags contradictions. You resolve each one; only the affected code is re-read."],
              ["04", "Approve", "Decide per quirk whether to preserve or fix it, then sign off. Approval pins the exact graph and document and goes stale if either changes."],
            ].map(([n, t, d]) => (
              <li key={n} className="bg-surface p-6">
                <span className="font-mono text-sm text-accent">{n}</span>
                <h4 className="display mt-5 text-2xl">{t}</h4>
                <p className="mt-2 text-sm text-muted">{d}</p>
              </li>
            ))}
          </ol>

          <h3 className="eyebrow mt-12">Phase 2: migrate and prove equivalence (command-line beta)</h3>
          <ol className="mt-4 grid gap-px overflow-hidden rounded-2xl border border-line bg-line md:grid-cols-4">
            {[
              ["05", "Plan", "Pick the target (Java 8 to 21, Spring Boot 2 to 3, Django 3 to 5, Express to Fastify, JS to Go). Target-fit finds risks; the planner cuts the service into ordered slices."],
              ["06", "Record", "Cases run twice against your live legacy service in a Docker sandbox. Expected behaviour is recorded, never written by the model. Token-protected services get a random token per run."],
              ["07", "Generate", "The model returns structured edits and never gets a shell. Builds and tests run in the sandbox; a fresh-session reviewer checks each slice."],
              ["08", "Prove and cut over", "Old and new restart from the same data and run the same cases. You approve each slice, a router moves its routes, and the cut-over plan needs its own approval."],
            ].map(([n, t, d]) => (
              <li key={n} className="bg-surface p-6">
                <span className="font-mono text-sm text-accent">{n}</span>
                <h4 className="display mt-5 text-2xl">{t}</h4>
                <p className="mt-2 text-sm text-muted">{d}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="border-y border-line bg-surface-2">
          <div className="mx-auto grid max-w-6xl gap-10 px-4 py-20 sm:px-6 md:grid-cols-2">
            <div>
              <p className="eyebrow mb-3">Watch it catch a wrong requirement</p>
              <h2 className="display text-4xl sm:text-5xl">Docs and code disagree more often than anyone admits.</h2>
              <p className="mt-4 text-muted">The Verifier never sees your code. It only compares the generated document with your own docs, then shows both quotes side by side.</p>
            </div>
            <div className="space-y-3 rounded-2xl border border-line-strong bg-surface p-5">
              <div className="flex items-center justify-between"><strong>Owner search page size</strong><span className="rounded bg-danger-soft px-2 py-0.5 font-mono text-[11px] font-semibold uppercase text-danger">high</span></div>
              <blockquote className="quote px-4 py-2 text-sm"><span className="eyebrow block">docs/user-guide.md:41</span>Search results for owners are displayed 10 per page.</blockquote>
              <blockquote className="quote px-4 py-2"><span className="eyebrow block">OwnerController.java:122</span><code className="font-mono text-[12.5px]">int pageSize = 5;</code></blockquote>
              <div className="flex flex-wrap gap-2 pt-1 text-sm">
                {["The document is right", "The code is right", "Not a real problem", "Look again"].map((l) => <span key={l} className="rounded-lg border border-line-strong px-3 py-1.5">{l}</span>)}
              </div>
            </div>
          </div>
        </section>

        <section id="status" className="border-y border-line bg-surface-2">
          <div className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
            <p className="eyebrow mb-3">What you get today</p>
            <h2 className="display max-w-3xl text-4xl sm:text-5xl">Where each part runs, and how far along it is.</h2>
            <div className="mt-10 grid gap-4 md:grid-cols-3">
              {[
                ["Available", "In this app", "Understand and verify", "Connect a repo, read it, review findings, explore the graph, add your docs for the Verifier, and track cost and usage."],
                ["Available", "Command line", "Approve", "Record preserve-or-fix decisions per quirk, check readiness, and sign off. The approval writes the migration contract."],
                ["Beta", "Command line, needs Docker", "Migrate with proof", "Target fit, slices, recorded cases, sandboxed generation, equivalence, fuzzing, a strangler router and a cut-over plan. Run it on your own machine."],
              ].map(([badge, where, t, d]) => (
                <article key={t} className="rounded-2xl border border-line-strong bg-surface p-6">
                  <p className="font-mono text-xs uppercase text-accent">{badge} · {where}</p>
                  <h3 className="display mt-3 text-2xl">{t}</h3>
                  <p className="mt-2 text-sm text-muted">{d}</p>
                </article>
              ))}
            </div>
            <div className="mt-8 rounded-2xl border border-line-strong bg-surface p-6">
              <p className="eyebrow mb-2">Sandbox trial of the migration beta, no model</p>
              <p className="text-sm text-muted">
                On the Express sample, the sandbox built the old service, recorded 24 authenticated cases, and ran a copy as the new one:
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
              <div key={v} className="border-t-2 border-ink pt-4">
                <dt className="display text-6xl tnum">{v}</dt>
                <dd className="mt-2 text-muted">{l}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-8 max-w-2xl text-sm text-muted">Labels are small and hand written: treat these as a regression guide, not a benchmark. The first live run of the migration sandbox found seven bugs that every offline test had missed; all are fixed. Nothing here is certified or audited.</p>
        </section>

        <section className="px-4 pb-24 sm:px-6">
          <div className="mx-auto max-w-6xl rounded-3xl bg-ink px-6 py-14 text-center text-bg sm:px-14">
            <h2 className="display text-4xl sm:text-6xl">Find out what your service really does, then move it safely.</h2>
            <p className="mx-auto mt-3 max-w-lg opacity-75">Billing is not enabled yet. Create a workspace and run your first analysis under the internal plan. Approval and migration run in the command-line tool.</p>
            <LinkButton href="/signup" variant="primary" className="mt-8 min-h-12 px-8 text-base">Create a workspace</LinkButton>
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
