"use client";
export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-xl flex-col items-start justify-center gap-5 px-6">
      <p className="eyebrow">Something went wrong</p>
      <h1 className="display text-5xl">That did not work.</h1>
      <p className="text-muted">An unexpected error stopped this page. Your data is unaffected. Try again, and if it keeps happening, reload the page.</p>
      <button type="button" onClick={reset} className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-bg">Try again</button>
    </main>
  );
}
