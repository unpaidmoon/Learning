// Readonly project method probe.
type SourceJump=any;
export class JumpProbe {file:any;application:any;pendingJump:any=null;disposed=false;highlight:any=null;highlightTimer=0;constructor(file:any,application:any){this.file=file;this.application=application;}
jump(value: SourceJump): void {
    this.pendingJump = value;
    this.applyJump();
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
}