/** Browser-only originals are never part of an attachment or submission request. */
export interface CropRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface CaptureSource {
  sourceId: string;
  dataUrl: string;
  width: number;
  height: number;
  source?: {
    asset_id: string;
    source_version: string;
    time_ms: number;
  };
  initialCrop?: CropRect;
}

export interface CaptureResult {
  source: CaptureSource;
  crop: CropRect;
  dataUrl: string;
  width: number;
  height: number;
}

export class CaptureError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "CaptureError";
  }
}

const MAX_PIXELS = 40_000_000;
const MAX_BYTES = 10_000_000;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export function checkImageDimensions(width: number, height: number) {
  if (
    !Number.isSafeInteger(width) ||
    !Number.isSafeInteger(height) ||
    width < 1 ||
    height < 1 ||
    width * height > MAX_PIXELS
  ) {
    throw new CaptureError("IMAGE_SIZE_INVALID", "图片尺寸无效或超过 4000 万像素");
  }
}

/** Round inward: a fractional edge must never add pixels outside the selected rectangle. */
export function normalizeCrop(rect: CropRect, width: number, height: number): CropRect {
  checkImageDimensions(width, height);
  if (![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite)) {
    throw new CaptureError("CROP_INVALID", "选区坐标无效");
  }
  const left = Math.max(0, Math.min(width, Math.ceil(rect.x)));
  const top = Math.max(0, Math.min(height, Math.ceil(rect.y)));
  const right = Math.max(0, Math.min(width, Math.floor(rect.x + rect.width)));
  const bottom = Math.max(0, Math.min(height, Math.floor(rect.y + rect.height)));
  if (right <= left || bottom <= top) {
    throw new CaptureError("CROP_EMPTY", "请框选至少 1 × 1 像素的图片区域");
  }
  return { x: left, y: top, width: right - left, height: bottom - top };
}

function imageElement(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new CaptureError("IMAGE_INVALID", "无法读取所选图片"));
    image.src = url;
  });
}

export async function readImageFile(file: File): Promise<CaptureSource> {
  if (!IMAGE_TYPES.has(file.type) || !file.size || file.size > MAX_BYTES) {
    throw new CaptureError("IMAGE_SIZE_INVALID", "请选择不超过 10MB 的 PNG、JPEG 或 WebP 图片");
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new CaptureError("IMAGE_INVALID", "无法读取所选图片"));
    reader.readAsDataURL(file);
  });
  const image = await imageElement(dataUrl);
  checkImageDimensions(image.naturalWidth, image.naturalHeight);
  return {
    sourceId: crypto.randomUUID(),
    dataUrl,
    width: image.naturalWidth,
    height: image.naturalHeight,
  };
}

/** Call only from the user's screenshot action, never during playback or background work. */
export function captureVideoFrame(
  video: HTMLVideoElement,
  metadata?: { asset_id: string; source_version: string; time_ms?: number },
): CaptureSource {
  video.pause();
  if (video.readyState < 2 || !Number.isFinite(video.currentTime)) {
    throw new CaptureError("VIDEO_FRAME_UNAVAILABLE", "视频画面尚未就绪，请播放后重试截图");
  }
  const width = video.videoWidth;
  const height = video.videoHeight;
  checkImageDimensions(width, height);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new CaptureError("CAPTURE_UNAVAILABLE", "当前浏览器无法创建截图");
  try {
    context.drawImage(video, 0, 0, width, height);
    return {
      sourceId: crypto.randomUUID(),
      dataUrl: canvas.toDataURL("image/png"),
      width,
      height,
      source: metadata
        ? {
            asset_id: metadata.asset_id,
            source_version: metadata.source_version,
            time_ms: Math.round(video.currentTime * 1000),
          }
        : undefined,
    };
  } catch {
    throw new CaptureError("CAPTURE_UNAVAILABLE", "无法读取当前视频画面，请检查视频后重试");
  }
}

/** Export exactly the confirmed source-pixel rectangle, without scaling or other frame access. */
export async function exportCrop(
  source: CaptureSource,
  selection: CropRect,
): Promise<CaptureResult> {
  const crop = normalizeCrop(selection, source.width, source.height);
  const image = await imageElement(source.dataUrl);
  if (image.naturalWidth !== source.width || image.naturalHeight !== source.height) {
    throw new CaptureError("IMAGE_VERSION_CHANGED", "原图尺寸与编辑记录不一致，请重新选择图片");
  }
  const canvas = document.createElement("canvas");
  canvas.width = crop.width;
  canvas.height = crop.height;
  const context = canvas.getContext("2d");
  if (!context) throw new CaptureError("CAPTURE_UNAVAILABLE", "当前浏览器无法创建截图");
  context.drawImage(image, crop.x, crop.y, crop.width, crop.height, 0, 0, crop.width, crop.height);
  const dataUrl = canvas.toDataURL("image/png");
  // Check decoded bytes rather than the larger base64 representation.
  if (Math.ceil(((dataUrl.length - dataUrl.indexOf(",") - 1) * 3) / 4) > MAX_BYTES) {
    throw new CaptureError("IMAGE_SIZE_INVALID", "选区图片超过 10MB，请缩小选区");
  }
  return { source, crop, dataUrl, width: crop.width, height: crop.height };
}
