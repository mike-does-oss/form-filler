// Turn a picked or dropped folder into batch sets: one sub-folder = one set = one filled form.
// Mirrors backend/app/jobs/batch.py:group_files.

export const SUPPORTED_EXT = [".bmp", ".docx", ".gif", ".jpeg", ".jpg", ".md", ".pdf", ".png", ".tif", ".tiff", ".txt", ".webp"];
const JUNK = new Set([".ds_store", "thumbs.db", "desktop.ini"]);

export interface PickedFile { path: string; file: File } // path relative to the batch folder: "alice/2024/x.pdf"
export interface SourceSet { name: string; files: { name: string; file: File }[] } // name inside the set: "2024/x.pdf"
export interface Grouped { root: string | null; sets: SourceSet[]; skipped: { path: string; reason: string }[] }

export function groupFiles(picked: PickedFile[], root: string | null): Grouped {
  const sets = new Map<string, SourceSet>();
  const skipped: Grouped["skipped"] = [];
  for (const { path, file } of picked) {
    const parts = path.split(/[\\/]/).filter((p) => p && p !== ".");
    if (!parts.length || parts.includes("..")) { skipped.push({ path, reason: "invalid path" }); continue; }
    const name = parts[parts.length - 1];
    if (JUNK.has(name.toLowerCase()) || parts.some((p) => p.startsWith("."))) continue;
    if (parts.length < 2) { skipped.push({ path, reason: "not inside a sub-folder" }); continue; }
    const ext = name.includes(".") ? name.slice(name.lastIndexOf(".")).toLowerCase() : "";
    if (!SUPPORTED_EXT.includes(ext)) { skipped.push({ path, reason: "unsupported file type" }); continue; }
    const set = sets.get(parts[0]) ?? { name: parts[0], files: [] };
    set.files.push({ name: parts.slice(1).join("/"), file });
    sets.set(parts[0], set);
  }
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
  const out = [...sets.values()].sort((a, b) => collator.compare(a.name, b.name));
  out.forEach((s) => s.files.sort((a, b) => collator.compare(a.name, b.name)));
  return { root, sets: out, skipped };
}

/** From <input webkitdirectory>: paths include the chosen folder, which is the batch folder. */
export function fromInput(list: FileList): Grouped {
  const files = Array.from(list);
  const root = files[0]?.webkitRelativePath.split("/")[0] ?? null;
  const picked = files.map((f) => ({ path: f.webkitRelativePath.split("/").slice(1).join("/") || f.name, file: f }));
  return groupFiles(picked, root);
}

/**
 * From drag and drop. Dropping one folder that contains sub-folders treats it as the batch folder;
 * dropping several folders treats each one as a set.
 */
export async function fromDrop(items: DataTransferItemList): Promise<Grouped> {
  const entries = Array.from(items)
    .map((i) => i.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  const dirs = entries.filter((e) => e.isDirectory) as FileSystemDirectoryEntry[];
  if (entries.length === 1 && dirs.length === 1) {
    const children = await readAll(dirs[0]);
    if (children.some((c) => c.isDirectory)) {
      const picked: PickedFile[] = [];
      for (const c of children) picked.push(...(await walk(c, c.name)));
      return groupFiles(picked, dirs[0].name);
    }
  }
  const picked: PickedFile[] = [];
  for (const e of entries) picked.push(...(await walk(e, e.name)));
  return groupFiles(picked, null);
}

async function walk(entry: FileSystemEntry, path: string): Promise<PickedFile[]> {
  if (entry.isFile) {
    const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
    return [{ path, file }];
  }
  const out: PickedFile[] = [];
  for (const c of await readAll(entry as FileSystemDirectoryEntry)) out.push(...(await walk(c, `${path}/${c.name}`)));
  return out;
}

async function readAll(dir: FileSystemDirectoryEntry): Promise<FileSystemEntry[]> {
  const reader = dir.createReader();
  const all: FileSystemEntry[] = [];
  // readEntries returns at most ~100 entries per call; keep reading until empty
  for (;;) {
    const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) return all;
    all.push(...batch);
  }
}
