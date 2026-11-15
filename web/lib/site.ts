/** Single source for everything search engines and social cards read. The page renders from the same data. */
export const SITE_URL = (process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3100").replace(/\/$/, "");
export const SITE_NAME = "Parity";
/** Optional public contact address. When unset, no mailto links are rendered. */
export const CONTACT_EMAIL = process.env.NEXT_PUBLIC_CONTACT_EMAIL || "";
export const LEGAL_UPDATED = "2026-11-15";

// <title> <= 60 characters, description <= 160.
export const HOME_TITLE = "Parity: Legacy App Migration, Upgrade or Replatform, Proven";
export const HOME_DESCRIPTION =
  "Parity migrates legacy apps to the latest version or a new stack, then proves parity with the old app. Java 8 to 21, Spring Boot 3, Django 5, Fastify, Go.";
export const KEYWORDS = [
  "legacy application migration", "legacy code modernization", "application modernization tool", "replatform legacy app",
  "upgrade Java 8 to Java 21", "Spring Boot 2 to 3 migration", "Django 3 to 5 upgrade", "Express to Fastify migration",
  "JavaScript to Go migration", "strangler fig migration", "behaviour-preserving migration", "characterization tests",
];

export const PATHS = [
  {
    id: "java-spring",
    name: "Java 8 to Java 21, Spring Boot 2 to 3",
    text: "Deterministic OpenRewrite recipes handle the mechanical upgrade first (javax to jakarta, language and API changes) in Maven or Gradle projects; the model fixes only what they leave. Run in the sandbox on a real Spring app.",
  },
  {
    id: "django",
    name: "Django 3 to Django 5",
    text: "django-upgrade and pyupgrade rewrite deprecated imports and syntax before any model call, so the model is left with the parts that need judgement.",
  },
  {
    id: "express-fastify",
    name: "Express to Fastify",
    text: "A framework move with no deterministic codemod: the model translates slice by slice, and every slice is compared with the running Express app before you approve it.",
  },
  {
    id: "js-go",
    name: "JavaScript to Go",
    text: "A language move, best run as a strangler migration: routes move to the Go service one approved slice at a time while the old app keeps serving the rest.",
  },
] as const;

export const STEPS = [
  { name: "Understand the legacy app", text: "Read the source in a read-only sandbox. Every claim in the resulting map cites a file and a line, and a blind Verifier compares it with your own documentation." },
  { name: "Approve the map", text: "Resolve the findings, decide per quirk whether to keep or fix the behaviour, and sign off. The approval pins the exact map and goes stale if it changes." },
  { name: "Migrate slice by slice", text: "Plan ordered slices and generate the new app in a Docker sandbox. The model returns structured edits and never gets a shell." },
  { name: "Prove it behaves the same", text: "Record the running legacy app's answers, then run the same cases against the new app and list every difference." },
  { name: "Cut over", text: "Route traffic to the new app one approved slice at a time, with a cut-over plan that needs its own approval." },
] as const;

export const FAQ = [
  { q: "What is Parity?", a: "A tool that takes an existing legacy application and migrates it to the newest version of its stack, or to a different technology, then checks that the new app behaves like the old one. Understanding the legacy code is step 1 of five: understand, approve, migrate, prove, cut over." },
  { q: "Which migrations does it support?", a: "Java 8 to 21 and Spring Boot 2 to 3 are the first-class path. Library and API-risk tables also cover Django 3 to 5, Express to Fastify and JavaScript to Go. Other moves are not covered yet." },
  { q: "How does it prove the new app behaves like the old one?", a: "The running legacy app is the oracle. Test inputs are run against it twice, noise is masked, and its answers become the expected results. The same cases then run against the new app, both starting from the same data, and every difference is listed. The model proposes inputs but never writes an expected answer." },
  { q: "Does it change my code?", a: "No. Your source is copied into a project folder and analysed there; the migrated app is written to its own folder, never into your repository." },
  { q: "Does the AI model run my code?", a: "No. The model returns structured file edits only and has no shell or network tool. Builds and tests run in locked-down Docker containers: tests with no network, builds only through the package mirror you configure." },
  { q: "Is it production ready?", a: "It is a beta. Every step is built and tested, and the sandbox has run for real on three sample apps, but no migration stage has run with a real AI model yet, so the quality of model-written code is unmeasured. Run it on a copy and review every slice before you approve it." },
  { q: "Where does each step run?", a: "The hosted web app runs step 1, understanding the legacy app. Approval and the migration steps run in the command-line tool on your own machine, with Docker." },
  { q: "How do I get access?", a: "Parity is in early access. Create a workspace, connect your legacy app (a git URL or an archive) and the first step runs straight away. The approval and migration steps run in the command-line tool on your own machine, with Docker." },
  { q: "Is my code safe?", a: "Your repository is never modified: the source is copied into a project, secrets are withheld or redacted at ingest, and the analysis never executes your code. Generated code runs only in locked-down Docker containers. Excerpts of your code (redacted on a best-effort basis) are sent to the AI model provider during analysis, and there is no zero-retention agreement with a provider yet. Parity is not certified or audited." },
  { q: "How much does it cost?", a: "Billing is not enabled yet. Model usage is metered and every run can be capped with a hard budget that stops it gracefully; re-running unchanged code makes no model calls." },
] as const;

export function jsonLd() {
  const org = { "@type": "Organization", "@id": `${SITE_URL}/#org`, name: SITE_NAME, url: SITE_URL, logo: `${SITE_URL}/icon.svg` };
  return [
    { "@context": "https://schema.org", "@graph": [
      org,
      { "@type": "WebSite", "@id": `${SITE_URL}/#website`, url: SITE_URL, name: SITE_NAME, description: HOME_DESCRIPTION, publisher: { "@id": `${SITE_URL}/#org` }, inLanguage: "en" },
      { "@type": "WebPage", "@id": `${SITE_URL}/#webpage`, url: SITE_URL, name: HOME_TITLE, description: HOME_DESCRIPTION, isPartOf: { "@id": `${SITE_URL}/#website` }, about: { "@id": `${SITE_URL}/#app` }, inLanguage: "en" },
      {
        "@type": "SoftwareApplication", "@id": `${SITE_URL}/#app`, name: SITE_NAME, url: SITE_URL,
        applicationCategory: "DeveloperApplication", operatingSystem: "macOS, Linux, Windows (with Docker)",
        description: HOME_DESCRIPTION, publisher: { "@id": `${SITE_URL}/#org` },
        featureList: [
          "Evidence-backed understanding of a legacy app: every claim cites a file and line",
          "Blind verification of the generated map against your own documentation",
          "Slice-by-slice migration generated in a Docker sandbox",
          "Equivalence checks against responses recorded from the running legacy app",
          "Differential fuzzing and latency comparison between old and new",
          "Strangler-style routing and an approved cut-over plan",
        ],
      },
    ] },
    { "@context": "https://schema.org", "@type": "HowTo", name: "How to migrate a legacy app to a newer version or a different stack",
      description: HOME_DESCRIPTION, step: STEPS.map((s, i) => ({ "@type": "HowToStep", position: i + 1, name: s.name, text: s.text })) },
    { "@context": "https://schema.org", "@type": "FAQPage", mainEntity: FAQ.map((f) => ({ "@type": "Question", name: f.q, acceptedAnswer: { "@type": "Answer", text: f.a } })) },
  ];
}
