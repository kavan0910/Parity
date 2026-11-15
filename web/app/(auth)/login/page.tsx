import type { Metadata } from "next";
import { AuthForm } from "@/components/AuthForm";

export const metadata: Metadata = { title: "Log in", robots: { index: false, follow: true }, alternates: { canonical: "/login" } };
export default function Page() {
  return <AuthForm mode="login" />;
}
