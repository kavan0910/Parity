import type { Metadata } from "next";
import { AuthForm } from "@/components/AuthForm";

export const metadata: Metadata = {
  title: "Start a legacy app migration",
  description: "Create a workspace, connect your legacy app and start migrating it to the newest version or a different stack.",
  alternates: { canonical: "/signup" },
};
export default function Page() {
  return <AuthForm mode="signup" />;
}
