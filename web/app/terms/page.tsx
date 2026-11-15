import type { Metadata } from "next";
import { LegalPage } from "@/components/LegalPage";

export const metadata: Metadata = {
  title: "Terms",
  description: "The early-access terms for using Parity while it is in beta.",
  alternates: { canonical: "/terms" },
};

export default function Terms() {
  return (
    <LegalPage title="Terms" intro="These early-access terms apply while Parity is in beta. They will be replaced by full terms before general availability.">
      <section>
        <h2>Early access</h2>
        <p>Parity is a beta. It is provided as is, without a service level or a warranty, and features, limits and behaviour can change. Generated code and analysis can be wrong: review every slice and every approval yourself before relying on it.</p>
      </section>
      <section>
        <h2>Your code</h2>
        <p>Only connect or upload code you have the right to use. You are responsible for what you submit. Do not submit credentials on purpose: secrets are redacted on a best-effort basis, not guaranteed.</p>
      </section>
      <section>
        <h2>Acceptable use</h2>
        <ul>
          <li>Do not use Parity to attack, probe or overload any system, including this one.</li>
          <li>Do not attempt to bypass limits, budgets or the isolation of the sandbox.</li>
          <li>Do not submit unlawful content.</li>
        </ul>
      </section>
      <section>
        <h2>Cost and limits</h2>
        <p>Billing is not enabled yet. Workspaces run within the limits of their plan, and a run stops when its budget is reached. We can change or end early access, and suspend an account that breaks these terms.</p>
      </section>
      <section>
        <h2>Privacy</h2>
        <p>How your data is handled is described on the <a className="underline" href="/privacy">privacy page</a>.</p>
      </section>
    </LegalPage>
  );
}
