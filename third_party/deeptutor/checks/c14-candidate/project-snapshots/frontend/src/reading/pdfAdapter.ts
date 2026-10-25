/** Only the pinned public Viewer surface crosses this module boundary. */
import type { Schema } from "../api/client";
import type { ReadingPosition, SourceJump } from "./actions";
import { registerReadingCapture } from "./captureFrame";
import { attachOcrAdapter } from "./ocrLayer.mjs";
import type { OcrLayerAdapter } from "./ocrLayer.mjs";
import { coalesceSelectionRegions } from "./selectionRegions";

type PdfApplication = {
  initializedPromise: Promise<void>;
  open(options: { url: string }): Promise<void>;
  close(): Promise<void>;
  pdfViewer: {
    currentPageNumber: number;
    currentScaleValue: string;
    pagesRotation: number;
    pagesPromise: Promise<void>;
    getPageView(index: number): {
      div: HTMLElement;
      viewport: {
        width: number;
        height: number;
        viewBox: number[];
        convertToPdfPoint(x: number, y: number): number[];
        convertToViewportPoint(x: number, y: number): number[];
      };
    };
  };
  eventBus: {
    on(name: string, callback: () => void): void;
    off(name: string, callback: () => void): void;
  };
};
type PdfWindow = Window & {
  PDFViewerApplication: PdfApplication;
  PDFViewerApplicationOptions: { set(name: string, value: unknown): void };
};

const VERSION = "5.4.624";

export class PdfReaderAdapter {
  readonly frame = document.createElement("iframe");
  private disposed = false;
  private application: PdfApplication | null = null;
  private removeInit: (() => void) | null = null;
  private removeKeys: (() => void) | null = null;
  private removeReading: (() => void) | null = null;
  private removeCapture: (() => void) | null = null;
  private pendingJump: SourceJump | null = null;
  private highlight: HTMLElement | null = null;
  private highlightTimer = 0;
  private ocr: OcrLayerAdapter | null = null;
  private ocrPending = new Map<number, Schema<"OcrPageLayer">>();
  private applyingOcr = false;
  private selectionBusy = false;
  private dragging = false;

  constructor(
    private host: HTMLElement,
    private file: Schema<"CourseFileOpenResponse">,
    private callbacks?: {
      selection(value: Schema<"PdfSelectionInput"> | null): void;
      position(value: ReadingPosition): void;
      error?(value: unknown): void;
    },
  ) {
    this.frame.className = "pdf-reader-frame";
    this.frame.title = file.entry.name;
    this.frame.dataset.learningPdfReader = "true";
    // The frame runs fixed product code. PDF document scripts/XFA/eval are disabled.
    this.frame.sandbox.add("allow-scripts", "allow-same-origin");
  }

  async open(version: string, signal: AbortSignal, state?: Schema<"ReadingState">): Promise<void> {
    if (version !== VERSION || !this.file.content_url)
      throw new Error("阅读资源版本不符（PDF_VERSION_CONFLICT）");
    const source = new URL(this.file.content_url, window.location.origin);
    if (
      source.origin !== window.location.origin ||
      !/^\/api\/v1\/course-files\/[A-Za-z0-9_-]+\/content$/.test(source.pathname) ||
      source.searchParams.get("source_version") !== this.file.source_version
    ) {
      throw new Error("资料来源不符（PDF_SOURCE_DENIED）");
    }
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(
        () => reject(new Error("PDF 阅读器未能就绪，请重试（PDF_VIEWER_TIMEOUT）")),
        20000,
      );
      const abort = () => reject(new DOMException("Reading closed", "AbortError"));
      const initialize = (event: Event) => {
        const sourceWindow = (event as CustomEvent<{ source?: Window }>).detail?.source;
        if (sourceWindow !== this.frame.contentWindow || this.disposed || signal.aborted) return;
        const pdf = sourceWindow as PdfWindow;
        const options = pdf.PDFViewerApplicationOptions;
        for (const [name, value] of Object.entries({
          defaultUrl: "",
          disablePreferences: true,
          enableScripting: false,
          isEvalSupported: false,
          enableXfa: false,
          annotationEditorMode: -1,
          enableAltText: false,
          enableAltTextModelDownload: false,
          enableGuessAltText: false,
          enableSignatureEditor: false,
          disableHistory: true,
          historyUpdateUrl: false,
          localeProperties: { lang: "zh-CN" },
        }))
          options.set(name, value);
        this.application = pdf.PDFViewerApplication;
        const originalOpen = this.application.open.bind(this.application);
        this.application.open = (request) => {
          if (
            this.disposed ||
            typeof request.url !== "string" ||
            new URL(request.url, window.location.origin).href !== source.href
          )
            return Promise.reject(new Error("当前阅读器只打开已绑定版本（PDF_SOURCE_DENIED）"));
          return originalOpen(request);
        };
        const style = pdf.document.createElement("style");
        style.textContent =
          "#secondaryOpenFile,#openFile,#editorModeButtons,#editorModeSeparator,#signatureEditorToolbar{display:none!important}body,button,select,input{font-family:var(--learning-reader-font);font-size:14px}";
        pdf.document.head.append(style);
        const parentStyle = getComputedStyle(this.host);
        pdf.document.documentElement.style.setProperty(
          "--learning-reader-font",
          parentStyle.fontFamily,
        );
        pdf.document.documentElement.style.setProperty("--main-color", parentStyle.color);
        const background = parentStyle.getPropertyValue("--background").trim();
        if (background) {
          pdf.document.documentElement.style.setProperty("--body-bg-color", background);
          pdf.document.documentElement.style.setProperty("--toolbar-bg-color", background);
        }
        const relayKey = (key: KeyboardEvent) => {
          if (
            !key.isTrusted ||
            !key.ctrlKey ||
            !key.shiftKey ||
            !["KeyS", "KeyA"].includes(key.code)
          )
            return;
          key.preventDefault();
          key.stopImmediatePropagation();
          document.dispatchEvent(
            new KeyboardEvent("keydown", {
              key: key.key,
              code: key.code,
              ctrlKey: key.ctrlKey,
              shiftKey: key.shiftKey,
              altKey: key.altKey,
              metaKey: key.metaKey,
              bubbles: true,
              cancelable: true,
            }),
          );
        };
        pdf.document.addEventListener("keydown", relayKey, true);
        this.removeKeys = () => pdf.document.removeEventListener("keydown", relayKey, true);
        void this.application.initializedPromise
          .then(async () => {
            if (this.disposed || signal.aborted)
              throw new DOMException("Reading closed", "AbortError");
            await this.application?.open({ url: source.href });
            if (!this.application) return;
            await this.application.pdfViewer.pagesPromise;
            if (this.disposed || signal.aborted) return;
            const viewer = this.application.pdfViewer;
            viewer.pagesRotation = state?.rotation || 0;
            viewer.currentScaleValue = state?.scale || "page-width";
            viewer.currentPageNumber = (state?.page_index || 0) + 1;
            const container = pdf.document.getElementById("viewerContainer")!;
            if (state?.scroll_fraction)
              container.scrollTop =
                state.scroll_fraction *
                Math.max(0, container.scrollHeight - container.clientHeight);
            const selectionChanged = () => this.inspectSelection(pdf);
            const pointerDown = () => {
              this.dragging = true;
            };
            const pointerUp = () => {
              this.dragging = false;
              void this.flushOcr().catch(() => undefined);
            };
            const viewChanged = () =>
              this.callbacks?.position({
                page_index: viewer.currentPageNumber - 1,
                scale: viewer.currentScaleValue,
                rotation: ((viewer.pagesRotation + 360) % 360) as 0 | 90 | 180 | 270,
                scroll_fraction: Math.max(
                  0,
                  Math.min(
                    1,
                    container.scrollTop /
                      Math.max(1, container.scrollHeight - container.clientHeight),
                  ),
                ),
              });
            pdf.document.addEventListener("selectionchange", selectionChanged);
            pdf.document.addEventListener("pointerdown", pointerDown, true);
            pdf.document.addEventListener("pointerup", pointerUp, true);
            this.application.eventBus.on("updateviewarea", viewChanged);
            this.removeReading = () => {
              pdf.document.removeEventListener("selectionchange", selectionChanged);
              pdf.document.removeEventListener("pointerdown", pointerDown, true);
              pdf.document.removeEventListener("pointerup", pointerUp, true);
              this.application?.eventBus.off("updateviewarea", viewChanged);
            };
            try {
              const ocr = await attachOcrAdapter(
                this.application,
                this.file.source_version,
                (error) => this.callbacks?.error?.(error),
              );
              if (this.disposed || signal.aborted) {
                ocr.dispose();
                return;
              }
              this.ocr = ocr;
              await this.flushOcr();
            } catch (error) {
              if (this.disposed || signal.aborted) return;
              // A protected/unsupported text-layer seam must not hide the
              // already readable original PDF page.
              this.callbacks?.error?.(error);
            }
            this.removeCapture = registerReadingCapture(
              this.frame,
              () =>
                !this.disposed &&
                this.application === pdf.PDFViewerApplication &&
                this.frame.src ===
                  new URL(
                    `/api/v1/reading/pdfjs/${VERSION}/web/viewer.html?file=`,
                    window.location.origin,
                  ).href,
            );
            viewChanged();
          })
          .then(() => {
            this.applyJump();
            resolve();
          }, reject);
      };
      const cleanup = () => {
        window.clearTimeout(timeout);
        document.removeEventListener("webviewerloaded", initialize);
        signal.removeEventListener("abort", abort);
      };
      this.removeInit = cleanup;
      document.addEventListener("webviewerloaded", initialize);
      signal.addEventListener("abort", abort, { once: true });
      this.host.append(this.frame);
      this.frame.src = `/api/v1/reading/pdfjs/${VERSION}/web/viewer.html?file=`;
    }).finally(() => this.removeInit?.());
  }

  jump(value: SourceJump): void {
    this.pendingJump = value;
    this.applyJump();
  }

  freezeSelection(value: boolean): void {
    this.selectionBusy = value;
    if (!value) void this.flushOcr().catch(() => undefined);
  }

  async applyOcr(value: Schema<"OcrPageLayer">): Promise<void> {
    if (value.asset_id !== this.file.asset_id || value.source_version !== this.file.source_version)
      throw new Error("文字层来源不符（OCR_SOURCE_MISMATCH）");
    // Empty recognition is a saved result, not an instruction to retry or to
    // manufacture selectable text. The original page remains readable.
    if (!value.text.trim() || !value.words.length) return;
    this.ocrPending.set(value.page_number, value);
    await this.flushOcr();
  }

  private async flushOcr(): Promise<void> {
    if (this.applyingOcr || !this.ocr || this.disposed) return;
    this.applyingOcr = true;
    try {
      while (this.ocrPending.size && !this.disposed) {
        if (!this.canReplaceLayer()) return;
        const [page, value] = this.ocrPending.entries().next().value!;
        this.ocrPending.delete(page);
        if (!(await this.ocr.apply(value, () => this.canReplaceLayer()))) {
          if (!this.ocrPending.has(page)) this.ocrPending.set(page, value);
          return;
        }
      }
    } catch (error) {
      this.callbacks?.error?.(error);
      throw error;
    } finally {
      this.applyingOcr = false;
    }
  }

  private canReplaceLayer(): boolean {
    const selection = this.frame.contentWindow?.getSelection();
    return (
      !this.disposed &&
      !this.selectionBusy &&
      !this.dragging &&
      (!selection || selection.isCollapsed)
    );
  }

  private applyJump(): void {
    const value = this.pendingJump;
    if (!this.application || !value || this.disposed) return;
    const reference = value.reference;
    if (
      reference.asset_id !== this.file.asset_id ||
      reference.source_version !== this.file.source_version
    )
      return;
    const part = reference.kind === "document_selection" ? reference.parts[0] : reference;
    const page = this.application.pdfViewer.getPageView(part.page_index);
    if (!page) return;
    this.pendingJump = null;
    this.application.pdfViewer.currentPageNumber = part.page_index + 1;
    this.highlight?.remove();
    window.clearTimeout(this.highlightTimer);
    const overlay = page.div.ownerDocument.createElement("div");
    overlay.setAttribute("aria-label", "原文选区");
    overlay.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:4";
    const [left, bottom, right, top] = page.viewport.viewBox;
    for (const region of part.regions || []) {
      const corners = [
        [region.x0, region.y0],
        [region.x0, region.y1],
        [region.x1, region.y0],
        [region.x1, region.y1],
      ].map(([x, y]) =>
        page.viewport.convertToViewportPoint(left + x * (right - left), top - y * (top - bottom)),
      );
      const xs = corners.map((point) => point[0]),
        ys = corners.map((point) => point[1]);
      const box = page.div.ownerDocument.createElement("div");
      box.style.cssText = `position:absolute;left:${Math.min(...xs)}px;top:${Math.min(...ys)}px;width:${Math.max(...xs) - Math.min(...xs)}px;height:${Math.max(...ys) - Math.min(...ys)}px;background:#eab30855;outline:2px solid #ca8a04`;
      overlay.append(box);
    }
    page.div.append(overlay);
    this.highlight = overlay;
    (overlay.firstElementChild || page.div).scrollIntoView({ block: "center", inline: "nearest" });
    this.highlightTimer = window.setTimeout(() => {
      overlay.remove();
    }, 6000);
  }

  private inspectSelection(pdf: PdfWindow): void {
    const selection = pdf.getSelection();
    const empty = () => this.callbacks?.selection(null);
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed || !this.application) {
      empty();
      void this.flushOcr().catch(() => undefined);
      return;
    }
    const range = selection.getRangeAt(0);
    const start = range.startContainer.parentElement?.closest(".textLayer");
    const end = range.endContainer.parentElement?.closest(".textLayer");
    if (!start || !end) {
      empty();
      return;
    }
    const pages: Schema<"PdfSelectionPage">[] = [];
    for (const layer of pdf.document.querySelectorAll<HTMLElement>(".page .textLayer")) {
      if (!range.intersectsNode(layer)) continue;
      const part = range.cloneRange();
      if (!layer.contains(part.startContainer)) part.setStart(layer, 0);
      if (!layer.contains(part.endContainer)) part.setEnd(layer, layer.childNodes.length);
      const fragment = part.cloneContents();
      for (const br of fragment.querySelectorAll("br"))
        br.replaceWith(pdf.document.createTextNode("\n"));
      const text = fragment.textContent || "";
      if (!text.trim()) continue;
      const index = Number(layer.closest<HTMLElement>(".page")?.dataset.pageNumber) - 1;
      const page = this.application.pdfViewer.getPageView(index);
      const canvas = page.div.querySelector(".canvasWrapper")?.getBoundingClientRect();
      const viewport = page.viewport;
      if (!canvas?.width || !canvas.height) {
        empty();
        return;
      }
      const [left, bottom, right, top] = viewport.viewBox;
      // A whole Range also reports fully selected element boxes, including
      // PDF.js's transient full-page endOfContent selection sentinel. Only
      // selected text leaves can define source geometry.
      const rectangles: DOMRect[] = [];
      const walker = pdf.document.createTreeWalker(layer, 4 /* SHOW_TEXT */);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (!part.intersectsNode(node)) continue;
        const leaf = pdf.document.createRange();
        leaf.selectNodeContents(node);
        if (node === part.startContainer) leaf.setStart(node, part.startOffset);
        if (node === part.endContainer) leaf.setEnd(node, part.endOffset);
        if (!leaf.collapsed && leaf.toString()) rectangles.push(...leaf.getClientRects());
      }
      const glyphRegions = rectangles
        .filter((r) => r.width > 0 && r.height > 0)
        .map((rect) => {
          const corners = [
            [rect.left, rect.top],
            [rect.right, rect.top],
            [rect.left, rect.bottom],
            [rect.right, rect.bottom],
          ].map(([x, y]) =>
            viewport.convertToPdfPoint(
              ((x - canvas.left) * viewport.width) / canvas.width,
              ((y - canvas.top) * viewport.height) / canvas.height,
            ),
          );
          const xs = corners.map((point) => (point[0] - left) / (right - left));
          const ys = corners.map((point) => (top - point[1]) / (top - bottom));
          return {
            x0: Math.max(0, Math.min(...xs)),
            y0: Math.max(0, Math.min(...ys)),
            x1: Math.min(1, Math.max(...xs)),
            y1: Math.min(1, Math.max(...ys)),
          };
        })
        .filter((r) => r.x1 > r.x0 && r.y1 > r.y0);
      const regions = coalesceSelectionRegions(glyphRegions, (right - left) / (top - bottom));
      if (!regions.length || regions.length > 256) {
        empty();
        return;
      }
      pages.push({ page_index: index, text, regions });
    }
    if (!pages.length || pages.length > 128) {
      empty();
      return;
    }
    this.callbacks?.selection({
      asset_id: this.file.asset_id,
      source_version: this.file.source_version,
      text: pages.map((p) => p.text).join(""),
      pages,
    });
  }

  dispose(): void {
    this.disposed = true;
    this.removeInit?.();
    this.removeKeys?.();
    this.removeReading?.();
    this.removeCapture?.();
    this.ocr?.dispose();
    this.ocrPending.clear();
    this.highlight?.remove();
    window.clearTimeout(this.highlightTimer);
    void this.application?.close().catch(() => undefined);
    this.frame.remove();
  }
}
