export const MAX_ARCHIVE_BYTES = 100 * 1024 * 1024;
const ARCHIVE = /\.(zip|tar\.gz|tgz)$/i;

export function validateGitUrl(raw: string): string | null {
  let u: URL;
  try {
    u = new URL(raw.trim());
  } catch {
    return "Enter a full URL, for example https://github.com/org/repo.";
  }
  if (u.protocol !== "https:") return "Only https:// URLs are accepted.";
  return null;
}
export function validateArchive(f: { name: string; size: number }): string | null {
  if (!ARCHIVE.test(f.name)) return "Use a .zip, .tar.gz or .tgz archive.";
  if (f.size > MAX_ARCHIVE_BYTES) return "That archive is larger than the 100 MB limit.";
  return null;
}
