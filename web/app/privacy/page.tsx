import type { Metadata } from "next";
import { LegalPage } from "@/components/LegalPage";

export const metadata: Metadata = {
  title: "Privacy",
  description: "What Parity stores, what it sends to the AI model provider, and how deletion works during early access.",
  alternates: { canonical: "/privacy" },
};

export default function Privacy() {
  return (
    <LegalPage title="Privacy" intro="What Parity does with your data during early access. It describes today's behaviour and will be updated before general availability; retention periods are not final.">
      <section>
        <h2>What we store</h2>
        <ul>
          <li>Your account: email address and a password hash (or an external identity subject), and your role in a workspace.</li>
          <li>Your project data: the source you connect or upload (secrets withheld or redacted at ingest), the understanding document, the knowledge graph, findings, comments and exports.</li>
          <li>Usage events: tokens, cost, model and stage per run. They contain no source content.</li>
          <li>An audit log of actions (who did what, to which item, when). It holds identifiers, not content, and is kept after a project is deleted.</li>
          <li>Application logs for operations. They are designed never to contain source content.</li>
        </ul>
      </section>
      <section>
        <h2>What is sent to the AI model provider</h2>
        <p>During analysis, excerpts of your code and prompts are sent to the model provider that is configured for your workspace. Secret redaction is pattern-based and best-effort: an unusual secret can get through. The provider&apos;s own retention and training terms apply, and there is no zero-retention agreement with a provider yet.</p>
      </section>
      <section>
        <h2>What Parity does not do to your code</h2>
        <p>Your repository is never modified. The analysis never executes your code. Migration steps run generated code only in locked-down Docker containers on the machine where you run the command-line tool.</p>
      </section>
      <section>
        <h2>Deleting your data</h2>
        <p>Deleting a project removes its source, artifacts, findings, comments and results. Backups are kept until they expire, the audit record (identifiers only) remains, and anything the model provider already processed is governed by the provider&apos;s terms. Account deletion is handled by hand on request during early access.</p>
      </section>
      <section>
        <h2>Cookies and tracking</h2>
        <p>This website sets no tracking cookies and runs no analytics. Your browser stores your theme choice, and the signed-in app keeps your session in the browser.</p>
      </section>
    </LegalPage>
  );
}
