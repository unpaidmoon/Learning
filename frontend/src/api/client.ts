import type { components } from "../../../contracts/generated/api";

export type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name];

export class ApiError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class ApiClient {
  private csrf: string | null = null;

  async connect(signal?: AbortSignal) {
    const session = await this.read<Schema<"SessionResponse">>("/session", signal);
    this.csrf = session.csrf_token;
  }

  read<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>(path, { signal });
  }

  write<T>(
    path: string,
    body?: unknown,
    options?: { keepalive?: boolean; timeoutMs?: number },
  ): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Learning-CSRF": this.csrf || "",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      keepalive: options?.keepalive,
      signal: options?.timeoutMs ? AbortSignal.timeout(options.timeoutMs) : undefined,
    });
  }

  upload<T>(path: string, file: Blob, options?: { timeoutMs?: number }): Promise<T> {
    return this.request<T>(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Learning-CSRF": this.csrf || "",
      },
      body: file,
      signal: AbortSignal.timeout(options?.timeoutMs ?? 300_000),
    });
  }

  async download(path: string, body: unknown): Promise<Blob> {
    let response: Response;
    try {
      response = await fetch(`/api/v1${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Learning-CSRF": this.csrf || "" },
        body: JSON.stringify(body),
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.timeout(180_000),
      });
    } catch {
      throw new ApiError("OFFLINE_DOWNLOAD_UNAVAILABLE", "导出连接中断，请核对后重新导出本版本");
    }
    if (!response.ok) {
      const detail = (await response.json().catch(() => null)) as Partial<
        Schema<"ErrorResponse">
      > | null;
      throw new ApiError(
        detail?.code || "OFFLINE_EXPORT_FAILED",
        detail?.message || "此版本未能导出",
        response.status,
      );
    }
    if (response.headers.get("content-type")?.split(";")[0] !== "application/zip") {
      throw new ApiError("OFFLINE_EXPORT_TYPE_INVALID", "导出内容类型不符，未保存文件");
    }
    return response.blob();
  }

  private async request<T>(path: string, options: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`/api/v1${path}`, {
        ...options,
        credentials: "same-origin",
        cache: "no-store",
        signal: options.signal || AbortSignal.timeout(20_000),
      });
    } catch (error) {
      if (options.signal?.aborted) throw error;
      throw new ApiError("BACKEND_UNREACHABLE", "无法连接后台，请检查服务后重新连接");
    }
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = data as Partial<Schema<"ErrorResponse">> | null;
      throw new ApiError(
        detail?.code || "BACKEND_NOT_READY",
        detail?.message || "后台尚未就绪，请稍后重新连接",
        response.status,
      );
    }
    return data as T;
  }
}

export const api = new ApiClient();

export function errorMessage(error: unknown): string {
  return error instanceof ApiError
    ? `${error.message}（${error.code}）`
    : "操作未完成，请重新连接后核对";
}
