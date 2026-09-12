import type { components } from "./generated/api";

export class ApiError extends Error {
  code: string;
  retryable: boolean;
  requestId?: string;
  constructor(
    message: string,
    code = "request_failed",
    retryable = false,
    requestId?: string,
  ) {
    super(message);
    this.code = code;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      credentials: "same-origin",
      signal: AbortSignal.timeout(60000),
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch {
    throw new ApiError(
      "The connection was interrupted. Reconnect and try again.",
      "connection_lost",
      true,
    );
  }
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = value?.error ?? value?.detail ?? value;
    const unavailable = response.status >= 500;
    if (response.status === 401 && detail?.code === "SESSION_EXPIRED")
      window.dispatchEvent(new Event("emer:session-expired"));
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : (detail?.message ??
            (unavailable
              ? "emer-GPT is temporarily unavailable. Try connecting again."
              : "This request could not be completed. Please try again.")),
      detail?.code ?? (unavailable ? "service_unavailable" : "request_failed"),
      detail?.retryable ?? unavailable,
      detail?.request_id ??
        value?.request_id ??
        response.headers.get("x-request-id") ??
        undefined,
    );
  }
  if (value === null && response.status !== 204)
    throw new ApiError(
      "emer-GPT returned an unreadable response. Try again.",
      "invalid_response",
      true,
    );
  return value as T;
}
export function post<T>(path: string, body: unknown = {}): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}
export function patch<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}
export const idempotencyKey = () => crypto.randomUUID();

export type Session = components["schemas"]["SessionView"];
export type SavedTranscript = components["schemas"]["SavedTranscript"];
export type TranscriptPage = components["schemas"]["TranscriptPage"];
export type SharedSnapshot = components["schemas"]["SharedSnapshot"];
export type ShareCreated = components["schemas"]["ShareCreated"];
export type Conversation = components["schemas"]["ConversationView"] & {
  runs?: Run[];
  next_cursor?: string | null;
  transcripts?: SavedTranscript[];
  transcripts_next_cursor?: number | null;
};
export type Source = {
  doc_id: string;
  title: string;
  category: string;
  version: string;
  effective_date: string;
  authority: string;
  text: string;
  index_id?: string;
};
export type Citation = components["schemas"]["Citation"];
export type Statement = components["schemas"]["PublishedStatement"];
export type Answer = components["schemas"]["PublishedAnswer"];
export type Run = Omit<components["schemas"]["RunView"], "error"> & {
  error?: { message?: string; code?: string } | string | null;
  provider_attempts?: {
    id: string;
    operation: string;
    model: string;
    status: string;
    usage?: Record<string, unknown> | null;
    error_code?: string | null;
    created_at: string;
    completed_at?: string | null;
  }[];
  events?: {
    id?: string;
    sequence?: number;
    type?: string;
    stage?: string;
    data?: Record<string, unknown>;
    created_at?: string;
  }[];
};
export type Draft = Omit<components["schemas"]["DraftView"], "check"> & {
  check?: {
    status?: string;
    supported?: boolean;
    assessment?: string;
    version?: number;
    checked_version?: number;
    issues?: (string | { text?: string; message?: string })[];
  } | null;
};
export const activeStatus = (status: string) =>
  [
    "queued",
    "running",
    "retrieving",
    "generating",
    "validating",
    "cancelling",
  ].includes(status);
