export type IncomingFile = { file: File; path: string };

type DroppedEntry = {
  name: string;
  isFile: boolean;
  isDirectory: boolean;
  file?: (ok: (file: File) => void, fail: (error: DOMException) => void) => void;
  createReader?: () => {
    readEntries: (
      ok: (entries: DroppedEntry[]) => void,
      fail: (error: DOMException) => void,
    ) => void;
  };
};

const MAX_FILES = 10_000;
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024;

function safePath(value: string) {
  const parts = value.replaceAll("\\", "/").split("/");
  if (parts.some((part) => !part || part === "." || part === ".." || /[:\0]/.test(part)))
    throw new Error("拖入的文件路径无效（FILE_DROP_PATH_INVALID）");
  return parts.join("/");
}

/** Read only the browser entries explicitly supplied by the user's drop gesture. */
export async function droppedItems(data: DataTransfer): Promise<{
  files: IncomingFile[];
  emptyDirectories: string[];
}> {
  const result: IncomingFile[] = [];
  const emptyDirectories: string[] = [];
  let nodeCount = 0;
  const add = (file: File, path: string) => {
    if (result.length >= MAX_FILES)
      throw new Error("单次拖入文件超过 10000 项，请分批复制（FILE_DROP_TOO_MANY）");
    if (file.size > MAX_UPLOAD_BYTES)
      throw new Error("单个文件超过 8 GiB，请通过系统文件管理器复制（FILE_UPLOAD_TOO_LARGE）");
    result.push({ file, path: safePath(path) });
  };
  const walk = async (entry: DroppedEntry, prefix = "", depth = 0): Promise<void> => {
    if (depth > 64) throw new Error("文件夹层级过深（FILE_DROP_TOO_DEEP）");
    nodeCount += 1;
    if (nodeCount > MAX_FILES)
      throw new Error("单次拖入文件及文件夹超过 10000 项，请分批复制（FILE_DROP_TOO_MANY）");
    const path = safePath(prefix + entry.name);
    if (entry.isFile && entry.file) {
      const file = await new Promise<File>((resolve, reject) => entry.file!(resolve, reject));
      add(file, path);
    } else if (entry.isDirectory && entry.createReader) {
      const reader = entry.createReader();
      let hasChildren = false;
      for (;;) {
        const entries = await new Promise<DroppedEntry[]>((resolve, reject) =>
          reader.readEntries(resolve, reject),
        );
        if (!entries.length) break;
        hasChildren = true;
        for (const child of entries) await walk(child, path + "/", depth + 1);
      }
      if (!hasChildren) emptyDirectories.push(path);
    }
  };
  const items = Array.from(data.items || []).filter((item) => item.kind === "file");
  const entries = items.map((item) => item.webkitGetAsEntry?.() as DroppedEntry | null);
  if (entries.some(Boolean)) {
    for (let index = 0; index < items.length; index += 1) {
      const entry = entries[index];
      if (entry) await walk(entry);
      else {
        const file = items[index].getAsFile();
        if (file) add(file, file.webkitRelativePath || file.name);
      }
    }
  } else {
    for (const file of Array.from(data.files || []))
      add(file, file.webkitRelativePath || file.name);
  }
  return { files: result, emptyDirectories };
}

export async function droppedFiles(data: DataTransfer): Promise<IncomingFile[]> {
  return (await droppedItems(data)).files;
}
