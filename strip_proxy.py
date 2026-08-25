"""Compatibility shim: Claude Code -> this -> LiteLLM -> Bedrock.

Claude Code sends `anthropic-beta: ...,advanced-tool-use-2025-11-20,
effort-2025-11-24,...`. Bedrock rejects those two with
`400 {"message":"invalid beta flag"}`.

Verified NOT to fix it (litellm v1.97.0, Bedrock apac Claude 3.7):
  - CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1  (anthropics/claude-code#30926)
  - nulling the flags in litellm's anthropic_beta_headers_config.json
    (for Bedrock they also ride in additionalModelRequestFields.anthropic_beta,
    which that mapping does not govern)
  - bedrock/invoke/ instead of bedrock/converse/

So the header has to be removed before litellm parses it.

`output_config` and `thinking` are stripped too: when the model name is one
Claude Code recognises it enables extended thinking, and Bedrock then rejects
either `thinking.enabled.display` or the assistant turn of a tool loop.

Everything else passes through untouched.
"""
import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.getenv("UPSTREAM_HOST", "litellm")
UPSTREAM_PORT = int(os.getenv("UPSTREAM_PORT", "4000"))
PORT = int(os.getenv("PORT", "4001"))

STRIP_HEADERS = {h.strip().lower() for h in os.getenv(
    "STRIP_HEADERS", "anthropic-beta,x-anthropic-beta").split(",") if h.strip()}
STRIP_BODY_KEYS = {k.strip() for k in os.getenv(
    "STRIP_BODY_KEYS", "output_config,thinking").split(",") if k.strip()}

HOP_BY_HOP = {"content-length", "host", "connection", "accept-encoding",
              "transfer-encoding", "keep-alive"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _proxy(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        if body and STRIP_BODY_KEYS:
            try:
                obj = json.loads(body)
                if isinstance(obj, dict) and any(k in obj for k in STRIP_BODY_KEYS):
                    for k in STRIP_BODY_KEYS:
                        obj.pop(k, None)
                    body = json.dumps(obj).encode()
            except Exception:
                pass  # not JSON: forward untouched

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in STRIP_HEADERS and k.lower() not in HOP_BY_HOP}
        headers["content-length"] = str(len(body))
        headers["accept-encoding"] = "identity"

        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=900)
        try:
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            msg = json.dumps({"error": {"message": f"strip-proxy: {exc}"}}).encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = _proxy


if __name__ == "__main__":
    print(f"strip-proxy :{PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT} | "
          f"headers={sorted(STRIP_HEADERS)} body={sorted(STRIP_BODY_KEYS)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
