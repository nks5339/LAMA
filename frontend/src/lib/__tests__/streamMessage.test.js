/**
 * streamMessage — the client half of POST /api/chat/stream.
 *
 * This is the contract the backend's `test_iter20_chat_stream.py` pins from
 * the other side. The two suites describe the same wire format; if either
 * drifts, one of them fails.
 *
 * The cases that matter are the awkward ones: a provider that cannot stream
 * (one giant chunk), an SSE frame split across two network reads, and a
 * user pressing Stop mid-answer.
 */
import { streamMessage } from "@/lib/api";

/** Build a fetch Response whose body streams the given byte chunks. */
function sseResponse(chunks, { ok = true, status = 200 } = {}) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok,
    status,
    statusText: "OK",
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { value: encoder.encode(chunks[i++]), done: false }
            : { value: undefined, done: true },
        cancel: async () => {},
      }),
    },
    json: async () => ({ detail: "boom" }),
  };
}

const frame = (obj) => `data: ${JSON.stringify(obj)}\n\n`;

const COMPLETE = {
  type: "complete",
  conversation_id: "conv-1",
  message: { content: "Billing is handled by Invoice.php.", role: "assistant" },
  intent: "srs.gap_question",
  srs_triggered: false,
  srs_error: "",
  session_id: "",
  tokens: 9,
};

beforeEach(() => {
  global.fetch = jest.fn();
  window.localStorage.clear();
});

afterEach(() => {
  jest.resetAllMocks();
});

describe("streamMessage", () => {
  it("parses phase, citation, token and complete in order", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([
        frame({ type: "phase", phase: "retrieving" }),
        frame({ type: "citation", filename: "Invoice.php", filetype: "php", score: 0.91 }),
        frame({ type: "phase", phase: "generating" }),
        frame({ type: "token", text: "Billing " }),
        frame({ type: "token", text: "is handled." }),
        frame(COMPLETE),
      ])
    );

    const seen = [];
    const final = await streamMessage({ project_id: "p1", message: "hi" }, (e) =>
      seen.push(e)
    );

    expect(seen.map((e) => e.type)).toEqual([
      "phase", "citation", "phase", "token", "token", "complete",
    ]);
    expect(final.conversation_id).toBe("conv-1");
  });

  it("concatenates deltas rather than counting them", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([
        frame({ type: "token", text: "a" }),
        frame({ type: "token", text: "b" }),
        frame({ type: "token", text: "c" }),
        frame(COMPLETE),
      ])
    );
    let text = "";
    await streamMessage({}, (e) => {
      if (e.type === "token") text += e.text;
    });
    expect(text).toBe("abc");
  });

  it("handles a non-streaming provider's single buffered chunk", async () => {
    // fabric_call_stream degrades to one yield on Anthropic-native and
    // Factory. The client must treat that as a valid stream.
    global.fetch.mockResolvedValue(
      sseResponse([
        frame({ type: "phase", phase: "generating" }),
        frame({ type: "token", text: "the entire answer at once" }),
        frame(COMPLETE),
      ])
    );
    const tokens = [];
    const final = await streamMessage({}, (e) => {
      if (e.type === "token") tokens.push(e.text);
    });
    expect(tokens).toHaveLength(1);
    expect(final.type).toBe("complete");
  });

  it("reassembles an SSE frame split across two network reads", async () => {
    // TCP does not respect \n\n boundaries. The buffer must carry the
    // partial frame into the next read.
    const whole = frame({ type: "token", text: "split-me" }) + frame(COMPLETE);
    const cut = Math.floor(whole.length / 3);
    global.fetch.mockResolvedValue(
      sseResponse([whole.slice(0, cut), whole.slice(cut)])
    );
    let text = "";
    await streamMessage({}, (e) => {
      if (e.type === "token") text += e.text;
    });
    expect(text).toBe("split-me");
  });

  it("ignores ping keep-alives", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([
        frame({ type: "ping", ts: "now" }),
        frame({ type: "token", text: "x" }),
        frame(COMPLETE),
      ])
    );
    const seen = [];
    await streamMessage({}, (e) => seen.push(e.type));
    expect(seen).not.toContain("ping");
  });

  it("skips a malformed frame instead of aborting the stream", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([
        "data: {not json\n\n",
        frame({ type: "token", text: "survived" }),
        frame(COMPLETE),
      ])
    );
    let text = "";
    const final = await streamMessage({}, (e) => {
      if (e.type === "token") text += e.text;
    });
    expect(text).toBe("survived");
    expect(final.type).toBe("complete");
  });

  it("does not let a listener throw kill the stream", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([frame({ type: "token", text: "x" }), frame(COMPLETE)])
    );
    const final = await streamMessage({}, () => {
      throw new Error("listener exploded");
    });
    expect(final.type).toBe("complete");
  });

  describe("failures", () => {
    it("rejects with the server's message on an error event", async () => {
      global.fetch.mockResolvedValue(
        sseResponse([frame({ type: "error", message: "LLM call failed: 429" })])
      );
      await expect(streamMessage({})).rejects.toThrow("LLM call failed: 429");
    });

    it("rejects with the detail on a non-2xx response", async () => {
      global.fetch.mockResolvedValue(sseResponse([], { ok: false, status: 404 }));
      await expect(streamMessage({})).rejects.toThrow("boom");
    });

    it("rejects when the stream ends without completing", async () => {
      global.fetch.mockResolvedValue(
        sseResponse([frame({ type: "token", text: "truncated" })])
      );
      await expect(streamMessage({})).rejects.toThrow(/without a complete event/i);
    });
  });

  describe("request shape", () => {
    it("posts to /chat/stream asking for an event stream", async () => {
      global.fetch.mockResolvedValue(sseResponse([frame(COMPLETE)]));
      await streamMessage({ project_id: "p1", message: "hi" });
      const [url, init] = global.fetch.mock.calls[0];
      expect(url).toMatch(/\/chat\/stream$/);
      expect(init.method).toBe("POST");
      expect(init.headers.Accept).toBe("text/event-stream");
      expect(JSON.parse(init.body)).toMatchObject({ project_id: "p1", message: "hi" });
    });

    it("attaches the bearer token when one is stored", async () => {
      window.localStorage.setItem("lama:auth:token", "jwt-abc");
      global.fetch.mockResolvedValue(sseResponse([frame(COMPLETE)]));
      await streamMessage({});
      expect(global.fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer jwt-abc");
    });

    it("omits Authorization when no token is stored", async () => {
      global.fetch.mockResolvedValue(sseResponse([frame(COMPLETE)]));
      await streamMessage({});
      expect(global.fetch.mock.calls[0][1].headers.Authorization).toBeUndefined();
    });

    it("forwards the AbortSignal that backs the Stop button", async () => {
      const ctrl = new AbortController();
      global.fetch.mockResolvedValue(sseResponse([frame(COMPLETE)]));
      await streamMessage({}, () => {}, ctrl.signal);
      expect(global.fetch.mock.calls[0][1].signal).toBe(ctrl.signal);
    });
  });

  it("surfaces srs_error so a failed auto-trigger is not swallowed", async () => {
    global.fetch.mockResolvedValue(
      sseResponse([
        frame({ ...COMPLETE, srs_triggered: false, srs_error: "Discovery is not frozen" }),
      ])
    );
    const final = await streamMessage({});
    expect(final.srs_triggered).toBe(false);
    expect(final.srs_error).toBe("Discovery is not frozen");
  });
});
