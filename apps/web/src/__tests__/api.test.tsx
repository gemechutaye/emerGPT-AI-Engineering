import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";

afterEach(() => vi.unstubAllGlobals());

it("identifies an unavailable proxy/backend as a retryable connection problem", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("Proxy error", { status: 500 })),
  );
  await expect(api("/session")).rejects.toMatchObject({
    code: "service_unavailable",
    retryable: true,
    message: expect.stringContaining("temporarily unavailable"),
  });
});

it("preserves the server's actionable error and request identity", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json(
        {
          error: {
            code: "VOICE_CAPACITY",
            message: "A voice session is still closing.",
            retryable: false,
          },
          request_id: "request-1",
        },
        { status: 409 },
      ),
    ),
  );
  await expect(api("/voice/sessions")).rejects.toMatchObject({
    code: "VOICE_CAPACITY",
    message: "A voice session is still closing.",
    retryable: false,
    requestId: "request-1",
  });
});

it("does not turn a successful HTML fallback into a successful API response", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("<html>Not the API</html>")),
  );
  await expect(api("/session")).rejects.toBeInstanceOf(ApiError);
  await expect(api("/session")).rejects.toMatchObject({
    code: "invalid_response",
    retryable: true,
  });
});
