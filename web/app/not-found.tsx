import Link from "next/link";
import { LinkButton } from "@/components/ui";
import { Logo } from "@/components/Brand";

export default function NotFound() {
  return (
    <main id="main" className="mx-auto flex min-h-dvh max-w-xl flex-col items-start justify-center gap-5 px-6">
      <Logo />
      <p className="eyebrow">404</p>
      <h1 className="display text-5xl">This page has migrated away.</h1>
      <p className="text-muted">The page you asked for does not exist, or it moved. Nothing is broken on your side.</p>
      <div className="flex gap-3"><LinkButton href="/" variant="primary">Back to Parity</LinkButton><Link href="/dashboard" className="inline-flex items-center px-3 text-sm underline">Your migrations</Link></div>
    </main>
  );
}
