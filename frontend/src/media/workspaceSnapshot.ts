import { CaptureError, checkImageDimensions } from "./capture";
import type { CaptureSource } from "./capture";
import { readingCaptureDocument } from "../reading/captureFrame";

export interface WorkspaceBounds {
  left: number;
  top: number;
  width: number;
  height: number;
}

export interface WorkspaceSnapshot {
  source: CaptureSource;
  bounds: WorkspaceBounds;
}

type Rectangle = { left: number; top: number; right: number; bottom: number };
const fontData = new Map<string, Promise<string>>();

// A trusted reader lives in another Window realm; instanceof against the parent
// constructors would fail. Tag checks here never grant an embedded-frame read.
const isVideo = (node: Element): node is HTMLVideoElement => node.tagName === "VIDEO";
const isImage = (node: Element): node is HTMLImageElement => node.tagName === "IMG";
const isCanvas = (node: Element): node is HTMLCanvasElement => node.tagName === "CANVAS";
const styled = (node: Element): node is HTMLElement | SVGElement => "style" in node;

function intersection(left: Rectangle, right: Rectangle): Rectangle {
  return {
    left: Math.max(left.left, right.left),
    top: Math.max(left.top, right.top),
    right: Math.min(left.right, right.right),
    bottom: Math.min(left.bottom, right.bottom),
  };
}

function hasArea(rect: Rectangle) {
  return rect.right > rect.left && rect.bottom > rect.top;
}

function blobData(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () =>
      reject(new CaptureError("CAPTURE_RESOURCE_UNAVAILABLE", "截图资源读取失败"));
    reader.readAsDataURL(blob);
  });
}

async function localResource(
  value: string,
  resources: Map<string, Promise<string>>,
  displayedImage?: HTMLImageElement,
) {
  if (value.startsWith("data:")) return value;
  const url = new URL(value, document.baseURI);
  if (url.origin !== location.origin) {
    throw new CaptureError("CAPTURE_EXTERNAL_RESOURCE", "截图包含无法在本机读取的外部资源");
  }
  let pending = resources.get(url.href);
  if (!pending) {
    // Blob images already belong to this page's local rendering. Reading their
    // displayed pixels uses img-src rather than a connect-src network request;
    // fetching the object URL fails under the desktop's unchanged strict CSP.
    pending = (
      url.protocol === "blob:"
        ? displayedImage
          ? Promise.resolve(imageFromCanvas(displayedImage).src)
          : new Promise<HTMLImageElement>((resolve, reject) => {
              const image = new Image();
              image.onload = () => resolve(image);
              image.onerror = () =>
                reject(new CaptureError("CAPTURE_RESOURCE_UNAVAILABLE", "截图资源尚未就绪"));
              image.src = url.href;
            }).then((image) => imageFromCanvas(image).src)
        : fetch(url.href, { credentials: "same-origin", signal: AbortSignal.timeout(15_000) })
            .then((response) => {
              if (!response.ok)
                throw new CaptureError("CAPTURE_RESOURCE_UNAVAILABLE", "截图资源尚未就绪");
              return response.blob();
            })
            .then(blobData)
    ).catch((reason: unknown) => {
      resources.delete(url.href);
      throw reason;
    });
    resources.set(url.href, pending);
  }
  return pending;
}

function copyStyle(style: CSSStyleDeclaration, destination: HTMLElement | SVGElement) {
  for (let index = 0; index < style.length; index += 1) {
    const property = style.item(index);
    destination.style.setProperty(
      property,
      style.getPropertyValue(property),
      style.getPropertyPriority(property),
    );
  }
  destination.style.setProperty("animation", "none", "important");
  destination.style.setProperty("transition", "none", "important");
  destination.style.setProperty("caret-color", "transparent", "important");
}

function cleanAttributes(node: Element) {
  for (const attribute of [...node.attributes]) {
    if (attribute.name.startsWith("on") || attribute.name === "autofocus")
      node.removeAttribute(attribute.name);
  }
}

function imageFromCanvas(node: HTMLVideoElement | HTMLCanvasElement | HTMLImageElement) {
  if (isVideo(node) && node.readyState < 2) {
    throw new CaptureError("VIDEO_FRAME_UNAVAILABLE", "视频画面尚未就绪，请等待缓冲后重新截图");
  }
  if (isImage(node) && (!node.complete || !node.naturalWidth)) {
    throw new CaptureError("CAPTURE_RESOURCE_UNAVAILABLE", "截图图片尚未就绪，请等待显示后重试");
  }
  const width = isVideo(node) ? node.videoWidth : isImage(node) ? node.naturalWidth : node.width;
  const height = isVideo(node)
    ? node.videoHeight
    : isImage(node)
      ? node.naturalHeight
      : node.height;
  checkImageDimensions(width, height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new CaptureError("CAPTURE_UNAVAILABLE", "当前浏览器无法创建截图");
  try {
    // Only the frame already displayed by this element is read, once, after the user's action.
    context.drawImage(node, 0, 0, width, height);
    const image = document.createElement("img");
    image.src = canvas.toDataURL("image/png");
    return image;
  } catch {
    throw new CaptureError(
      "CAPTURE_RESOURCE_UNAVAILABLE",
      "当前画面无法读取，请检查视频或图片来源",
    );
  }
}

async function embeddedFonts(families: Set<string>) {
  const rules: CSSFontFaceRule[] = [];
  const collect = (values: CSSRuleList) => {
    for (const rule of values) {
      if (rule instanceof CSSFontFaceRule) rules.push(rule);
      else if ("cssRules" in rule) collect((rule as CSSGroupingRule).cssRules);
    }
  };
  for (const sheet of document.styleSheets) {
    try {
      collect(sheet.cssRules);
    } catch {
      // Cross-origin styles are never fetched to extend the screenshot's input scope.
    }
  }
  const embedded = await Promise.all(
    rules.map(async (rule) => {
      const family = rule.style.getPropertyValue("font-family").replaceAll(/["']/g, "").trim();
      if (!families.has(family)) return "";
      const source = rule.style.getPropertyValue("src");
      const urls = [...source.matchAll(/url\(["']?([^"')]+)["']?\)/g)];
      const selected = urls.find((match) => /\.woff2(?:[?#]|$)/.test(match[1])) || urls[0];
      if (!selected) return "";
      const address = new URL(selected[1], rule.parentStyleSheet?.href || document.baseURI).href;
      const data = await localResource(address, fontData);
      return `@font-face{font-family:${rule.style.getPropertyValue("font-family")};src:url("${data}");font-style:${rule.style.getPropertyValue("font-style") || "normal"};font-weight:${rule.style.getPropertyValue("font-weight") || "normal"};font-display:block;}`;
    }),
  );
  return embedded.join("\n");
}

/** Freeze only the visible application DOM; the original never leaves browser memory. */
export async function captureWorkspace(root: HTMLElement): Promise<WorkspaceSnapshot> {
  const rootRect = root.getBoundingClientRect();
  const clip = intersection(rootRect, { left: 0, top: 0, right: innerWidth, bottom: innerHeight });
  if (!hasArea(clip))
    throw new CaptureError("CAPTURE_AREA_UNAVAILABLE", "当前学习界面不可见，请返回软件后截图");
  const bounds = {
    left: clip.left,
    top: clip.top,
    width: clip.right - clip.left,
    height: clip.bottom - clip.top,
  };
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.floor(bounds.width * ratio));
  const height = Math.max(1, Math.floor(bounds.height * ratio));
  checkImageDimensions(width, height);
  const resources = new Map<string, Promise<string>>();
  const pending: Promise<void>[] = [];
  const families = new Set<string>();

  const textCanPaint = (node: Node, visibleClip: Rectangle) => {
    const parent = node.parentElement;
    if (
      !parent ||
      parent.ownerDocument.defaultView?.getComputedStyle(parent).visibility !== "visible"
    )
      return false;
    const range = node.ownerDocument!.createRange();
    range.selectNodeContents(node);
    if (typeof range.getClientRects === "function") {
      return [...range.getClientRects()].some((rect) => hasArea(intersection(rect, visibleClip)));
    }
    // DOM-only test environments do not provide text layout; real browsers use the range above.
    return hasArea(intersection(parent.getBoundingClientRect(), visibleClip));
  };

  // This walk is synchronous, so streaming text and video do not change midway through a freeze.
  const clone = (node: Node, visibleClip: Rectangle): Node | null => {
    if (node.nodeType !== Node.ELEMENT_NODE)
      return node.nodeType === Node.TEXT_NODE && !textCanPaint(node, visibleClip)
        ? document.createTextNode("")
        : node.cloneNode(false);
    const element = node as Element;
    return cloneElement(element, visibleClip);
  };
  const cloneElement = (node: Element, visibleClip: Rectangle): Node | null => {
    if (node.matches("script,style,link,meta,[data-workspace-capture-ignore]")) return null;
    const style = node.ownerDocument.defaultView!.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const hiddenBranch =
      style.display === "none" || style.opacity === "0" || style.contentVisibility === "hidden";
    const visible =
      !hiddenBranch &&
      style.visibility !== "hidden" &&
      style.visibility !== "collapse" &&
      hasArea(intersection(rect, visibleClip));
    let result: Element;
    if (isVideo(node) || isCanvas(node))
      result = visible ? imageFromCanvas(node) : document.createElement("img");
    else if (isImage(node)) result = document.createElement("img");
    else if (
      visible &&
      node.tagName === "IFRAME" &&
      readingCaptureDocument(node as HTMLIFrameElement)
    ) {
      const embedded = readingCaptureDocument(node as HTMLIFrameElement)!;
      const localClip = intersection(
        { left: 0, top: 0, right: rect.width, bottom: rect.height },
        {
          left: visibleClip.left - rect.left,
          top: visibleClip.top - rect.top,
          right: visibleClip.right - rect.left,
          bottom: visibleClip.bottom - rect.top,
        },
      );
      result = document.createElement("div");
      const contents = clone(embedded.body, localClip);
      if (contents) result.append(contents);
      copyStyle(style, result as HTMLElement);
      (result as HTMLElement).style.overflow = "hidden";
      return result;
    } else if (visible && node.matches("iframe,object,embed")) {
      throw new CaptureError(
        "CAPTURE_CONTENT_UNAVAILABLE",
        "当前嵌入内容无法截图，请在软件支持的资料视图中重试",
      );
    } else if (node.matches("iframe,object,embed")) result = document.createElement("div");
    else if (["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName)) {
      result = document.createElement("div");
      if (visible)
        result.textContent =
          node.tagName === "SELECT"
            ? (node as HTMLSelectElement).selectedOptions[0]?.textContent || ""
            : (node as HTMLInputElement).value || node.getAttribute("placeholder") || "";
    } else result = node.cloneNode(false) as Element;
    cleanAttributes(result);
    if (styled(result)) copyStyle(style, result);
    if (visible) {
      for (const family of style.fontFamily.split(","))
        families.add(family.replaceAll(/["']/g, "").trim());
    }
    if (!visible) {
      // Zero-height inline/table ancestors can paint visible descendants (for example KaTeX
      // fractions). Preserve their original geometry instead of resizing or pruning the branch.
      if (styled(result)) {
        for (const property of [
          "background-image",
          "mask-image",
          "-webkit-mask-image",
          "border-image-source",
          "list-style-image",
          "content",
        ])
          result.style.setProperty(property, "none", "important");
        result.style.cursor = "auto";
      }
      if (isImage(result)) result.removeAttribute("src");
    }
    if (visible && isImage(result) && isImage(node)) {
      const source = node.currentSrc || node.src;
      result.removeAttribute("srcset");
      result.removeAttribute("loading");
      if (source)
        pending.push(
          localResource(source, resources, node).then((data) => {
            result.setAttribute("src", data);
          }),
        );
    }
    if (visible && styled(result)) {
      for (const property of ["background-image", "mask-image", "-webkit-mask-image"]) {
        const value = style.getPropertyValue(property);
        const matches = [...value.matchAll(/url\(["']?([^"')]+)["']?\)/g)];
        if (!matches.length) continue;
        pending.push(
          Promise.all(matches.map((match) => localResource(match[1], resources))).then((values) => {
            let position = 0;
            const rewritten = value.replace(
              /url\(["']?([^"')]+)["']?\)/g,
              () => `url("${values[position++]}")`,
            );
            (result as HTMLElement | SVGElement).style.setProperty(property, rewritten);
          }),
        );
      }
    }
    const childClip = { ...visibleClip };
    if (/hidden|clip|auto|scroll/.test(style.overflowX)) {
      childClip.left = Math.max(childClip.left, rect.left);
      childClip.right = Math.min(childClip.right, rect.right);
    }
    if (/hidden|clip|auto|scroll/.test(style.overflowY)) {
      childClip.top = Math.max(childClip.top, rect.top);
      childClip.bottom = Math.min(childClip.bottom, rect.bottom);
    }
    if (hiddenBranch || !hasArea(childClip)) return result;
    let contents: Element = result;
    if (node.scrollTop || node.scrollLeft) {
      const inner = document.createElement("div");
      inner.style.cssText = `display:${style.display};box-sizing:border-box;width:${node.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0")}px;min-height:${node.scrollHeight - parseFloat(style.paddingTop || "0") - parseFloat(style.paddingBottom || "0")}px;transform:translate(${-node.scrollLeft}px,${-node.scrollTop}px);transform-origin:top left;`;
      for (const property of [
        "flex-direction",
        "flex-wrap",
        "align-items",
        "justify-content",
        "gap",
        "grid-template-columns",
      ])
        inner.style.setProperty(property, style.getPropertyValue(property));
      (result as HTMLElement).style.display = "block";
      result.append(inner);
      contents = inner;
    }
    for (const child of node.childNodes) {
      const copied = clone(child, childClip);
      if (copied) contents.append(copied);
    }
    return result;
  };
  const frozen = clone(root, clip) as HTMLElement;
  frozen.style.position = "absolute";
  frozen.style.left = `${rootRect.left - clip.left}px`;
  frozen.style.top = `${rootRect.top - clip.top}px`;
  frozen.style.width = `${rootRect.width}px`;
  frozen.style.height = `${rootRect.height}px`;
  frozen.style.margin = "0";
  frozen.style.transform = "none";
  const fonts = embeddedFonts(families);
  await Promise.all(pending);
  const frame = document.createElement("div");
  frame.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
  frame.style.cssText = `position:relative;width:${bounds.width}px;height:${bounds.height}px;overflow:hidden;background:${getComputedStyle(document.body).backgroundColor};`;
  const fontStyle = document.createElement("style");
  fontStyle.textContent = await fonts;
  frame.append(fontStyle, frozen);
  const serialized = new XMLSerializer().serializeToString(frame);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${bounds.width} ${bounds.height}"><foreignObject x="0" y="0" width="${bounds.width}" height="${bounds.height}">${serialized}</foreignObject></svg>`;
  const image = await new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () =>
      reject(new CaptureError("CAPTURE_RENDER_FAILED", "当前界面截图失败，请取消后重试"));
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  });
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new CaptureError("CAPTURE_UNAVAILABLE", "当前浏览器无法创建截图");
  try {
    context.drawImage(image, 0, 0, width, height);
    return {
      bounds,
      source: {
        sourceId: crypto.randomUUID(),
        origin: "workspace",
        dataUrl: canvas.toDataURL("image/png"),
        width,
        height,
      },
    };
  } catch {
    throw new CaptureError("CAPTURE_RENDER_FAILED", "当前界面截图无法导出，请取消后重试");
  }
}
