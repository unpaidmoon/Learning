import type { CaptureResult } from "../media/capture";

// Original screenshots are a browser-local editing asset, never an attachment upload.
// Keeping this store separate prevents the execution API from acquiring full frames.
const databaseName = "learning-local-capture-editing";
const storeName = "originals";

async function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(databaseName, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(storeName);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("无法打开本机截图编辑缓存"));
  });
}

export async function saveCapture(attachmentId: string, result: CaptureResult) {
  const db = await database();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(storeName, "readwrite");
      transaction.objectStore(storeName).put(result, attachmentId);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new Error("本机截图编辑缓存未保存"));
      transaction.onabort = () => reject(new Error("本机截图编辑缓存被中断"));
    });
  } finally {
    db.close();
  }
}

export async function loadCapture(attachmentId: string): Promise<CaptureResult | undefined> {
  const db = await database();
  try {
    return await new Promise((resolve, reject) => {
      const request = db.transaction(storeName).objectStore(storeName).get(attachmentId);
      request.onsuccess = () => resolve(request.result as CaptureResult | undefined);
      request.onerror = () => reject(new Error("无法读取原截图"));
    });
  } finally {
    db.close();
  }
}
