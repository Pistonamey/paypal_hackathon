// AG Studio <-> Claude: an AgLlmAdapter that runs Studio's agents on Anthropic's Claude.
//
// Studio hands the adapter one turn (conversation, instructions, tool schemas). We translate it
// into an Anthropic Messages request, post it to our own backend (/api/studio-ai/messages, which
// holds the key and pins the model), and translate Claude's reply back into Studio's output
// items and AG-UI style events. The API key never reaches the browser.

// ---------------------------------------------------------------- tool schemas

// Studio's shape library marks optional values with the sentinel `{ not: {} }` inside anyOf.
const isUndefinedSentinel = (s) =>
  s && typeof s === "object" && Object.keys(s).length === 1 && s.not && Object.keys(s.not).length === 0;

function cleanSchema(schema) {
  if (Array.isArray(schema)) return schema.map(cleanSchema);
  if (!schema || typeof schema !== "object") return schema;
  const out = {};
  for (const [k, v] of Object.entries(schema)) {
    if (k === "anyOf" && Array.isArray(v)) {
      const branches = v.filter((b) => !isUndefinedSentinel(b)).map(cleanSchema);
      if (branches.length === 1) Object.assign(out, branches[0]);
      else out.anyOf = branches;
    } else {
      out[k] = cleanSchema(v);
    }
  }
  return out;
}

function toAnthropicTool(tool) {
  const schema = cleanSchema(tool.parameters || {});
  const input_schema = schema.type === "object" ? schema : { type: "object", properties: {}, ...schema, type: "object" };
  return { name: tool.name, description: tool.description || tool.name, input_schema };
}

// ---------------------------------------------------------------- conversation

function inputContent(content) {
  return content
    .map((c) => {
      if (c.type === "text") return { type: "text", text: c.text };
      if (c.type === "image" && c.imageUrl) {
        const m = /^data:(image\/[a-z+]+);base64,(.*)$/i.exec(c.imageUrl);
        return m
          ? { type: "image", source: { type: "base64", media_type: m[1], data: m[2] } }
          : { type: "image", source: { type: "url", url: c.imageUrl } };
      }
      return null; // files are not sent
    })
    .filter(Boolean);
}

function parseArgs(json) {
  if (!json) return {};
  try {
    const v = JSON.parse(json);
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

// Anthropic needs alternating user/assistant messages, with every tool_use answered by a
// tool_result in the next user message. Studio's items are flat, so group and merge them.
function encodeConversation(items) {
  const system = [];
  const messages = [];
  const push = (role, blocks) => {
    if (!blocks.length) return;
    const last = messages[messages.length - 1];
    if (last && last.role === role) last.content.push(...blocks);
    else messages.push({ role, content: [...blocks] });
  };
  for (const item of items) {
    if (item.type === "message" && item.kind !== "output") {
      if (item.role === "system") system.push(item.content.map((c) => c.text || "").join("\n"));
      else push("user", inputContent(item.content));
    } else if (item.type === "message" && item.kind === "output") {
      const text = item.content.map((c) => (c.type === "text" ? c.text : c.refusal || "")).join("");
      if (text.trim()) push("assistant", [{ type: "text", text }]);
    } else if (item.type === "function_call") {
      push("assistant", [{ type: "tool_use", id: item.callId, name: item.name, input: parseArgs(item.arguments) }]);
    } else if (item.type === "function_call_output") {
      push("user", [{ type: "tool_result", tool_use_id: item.callId, content: String(item.output ?? "") }]);
    }
    // reasoning items are not replayed
  }
  if (!messages.length || messages[0].role !== "user") {
    messages.unshift({ role: "user", content: [{ type: "text", text: "Continue." }] });
  }
  return { system, messages };
}

function buildRequest(request) {
  const { system, messages } = encodeConversation(request.input || []);
  if (request.instructions) system.unshift(request.instructions);
  const fmt = request.responseFormat;
  if (fmt && fmt.type === "json") {
    system.push(
      `Reply with only a JSON object named "${fmt.name}"${fmt.description ? ` (${fmt.description})` : ""} ` +
        `that matches this JSON Schema, and nothing else:\n${JSON.stringify(cleanSchema(fmt.schema))}`
    );
  }
  const tools = request.toolChoice === "none" ? [] : (request.tools || []).filter((t) => t.kind !== "provided");
  const body = { system: system.filter(Boolean).join("\n\n"), messages, max_tokens: 4096 };
  if (tools.length) {
    body.tools = tools.map(toAnthropicTool);
    if (request.toolChoice === "required" || (request.toolChoice && request.toolChoice.name)) {
      const which = request.toolChoice.name ? `the ${request.toolChoice.name} tool` : "one of your tools";
      body.system += `\n\nIn this turn you must call ${which}.`;
    }
    body.tool_choice = { type: "auto" };
  }
  return body;
}

// ---------------------------------------------------------------- response

function stripJsonFence(text) {
  const m = /```(?:json)?\s*([\s\S]*?)\s*```/.exec(text);
  return m ? m[1] : text;
}

function decode(resp, isJson) {
  const output = [];
  const events = [];
  resp.content.forEach((block, i) => {
    if (block.type === "text" && block.text) {
      const id = `${resp.id}_t${i}`;
      const text = isJson ? stripJsonFence(block.text) : block.text;
      output.push({
        id, kind: "output", type: "message", role: "assistant", status: "completed",
        content: [{ type: "text", text, annotations: [] }],
      });
      events.push(
        { type: "TEXT_MESSAGE_START", messageId: id, role: "assistant" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: id, delta: text },
        { type: "TEXT_MESSAGE_END", messageId: id }
      );
    } else if (block.type === "tool_use") {
      const args = JSON.stringify(block.input ?? {});
      output.push({
        id: block.id, kind: "output", type: "function_call", callId: block.id,
        name: block.name, arguments: args, status: "completed",
      });
      events.push(
        { type: "TOOL_CALL_START", toolCallId: block.id, toolCallName: block.name },
        { type: "TOOL_CALL_ARGS", toolCallId: block.id, delta: args },
        { type: "TOOL_CALL_END", toolCallId: block.id }
      );
    }
  });
  const usage = resp.usage
    ? {
        inputTokens: resp.usage.input_tokens,
        outputTokens: resp.usage.output_tokens,
        totalTokens: (resp.usage.input_tokens || 0) + (resp.usage.output_tokens || 0),
        cachedInputTokens: resp.usage.cache_read_input_tokens || undefined,
        cacheWriteTokens: resp.usage.cache_creation_input_tokens || undefined,
      }
    : undefined;
  const response = {
    id: resp.id,
    createdAt: Date.now(),
    model: resp.model,
    output,
    status: resp.stop_reason === "max_tokens" ? "incomplete" : "completed",
    incompleteDetails: resp.stop_reason === "max_tokens" ? { reason: "max_output_tokens" } : undefined,
    usage,
  };
  return { response, events };
}

// ---------------------------------------------------------------- adapter

export function claudeAdapter({ endpoint }) {
  return {
    executeTurn(request, options = {}) {
      let resolveComplete;
      let rejectComplete;
      const complete = new Promise((resolve, reject) => {
        resolveComplete = resolve;
        rejectComplete = reject;
      });
      // Observed for consumers that stop reading early; awaiting `complete` still sees failures.
      complete.catch(() => {});

      async function* run() {
        try {
          const res = await fetch(`${endpoint}/messages`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(buildRequest(request)),
            signal: options.signal,
          });
          const body = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`);
          const { response, events } = decode(body, request.responseFormat?.type === "json");
          for (const e of events) yield e;
          resolveComplete(response);
        } catch (error) {
          rejectComplete(error instanceof Error ? error : new Error(String(error)));
        }
      }

      const iterator = run();
      return { stream: { [Symbol.asyncIterator]: () => iterator }, complete };
    },
  };
}

// Exposed for tests.
export const _internal = { buildRequest, decode, encodeConversation, cleanSchema };
