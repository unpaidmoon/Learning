/** Only a live, bound product Viewer may contribute visible pixels to a capture. */
const readers = new WeakMap<HTMLIFrameElement, () => boolean>();

export function registerReadingCapture(frame: HTMLIFrameElement, isBound: () => boolean) {
  readers.set(frame, isBound);
  return () => readers.delete(frame);
}

export function readingCaptureDocument(frame: HTMLIFrameElement): Document | null {
  if (!readers.get(frame)?.()) return null;
  try {
    return frame.contentDocument;
  } catch {
    return null;
  }
}
