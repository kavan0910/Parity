import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "@/components/Brand";
import { CONTACT_EMAIL, LEGAL_UPDATED } from "@/lib/site";

export function LegalPage({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  return (
    <div className="min-h-dvh">
      <header className="border-b border-line">
        <div className="mx-auto flex h-16 max-w-3xl items-center justify-between px-4 sm:px-6">
          <Logo />
          <Link href="/" className="text-sm text-muted hover:text-ink">Back to home</Link>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-3xl px-4 py-14 sm:px-6">
        <h1 className="display text-5xl">{title}</h1>
        <p className="mt-3 text-muted">{intro}</p>
        <p className="mt-1 font-mono text-xs text-muted">Last updated {LEGAL_UPDATED}</p>
        <div className="mt-10 space-y-8 text-[15px] leading-7 [&_h2]:display [&_h2]:mb-2 [&_h2]:text-2xl [&_li]:ml-5 [&_li]:list-disc [&_p]:text-muted [&_li]:text-muted">
          {children}
          <section>
            <h2>Contact</h2>
            {CONTACT_EMAIL ? <p>Questions about this page: <a className="underline" href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>.</p> : <p>Questions about this page: use the contact route you received your invitation through.</p>}
          </section>
        </div>
      </main>
    </div>
  );
}
