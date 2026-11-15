import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Parity",
    short_name: "Parity",
    description: "Migrate a legacy app to the newest version or a different stack, and prove the new one behaves like the old one.",
    start_url: "/",
    display: "standalone",
    background_color: "#0a0a08",
    theme_color: "#0a0a08",
    icons: [{ src: "/icon.svg", sizes: "any", type: "image/svg+xml" }],
  };
}
