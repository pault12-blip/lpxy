#!/usr/bin/env python3
"""
litellm proxy — simple Flask app.
config.yaml → routing,  keys.tab → api keys,  trace.log → audit trail
"""

from flask import Flask, request, Response
import yaml, requests, random, time, os, copy, threading
from collections import defaultdict

app = Flask(__name__)

model_map:  dict[str, list[dict]] = defaultdict(list)
router_cfg: dict = {}
keys:       dict[str, str] = {}                 # name → key
trace_lock  = threading.Lock()


# ── load ──────────────────────────────────────────────────────────────────────
def load_config(path="config.yaml"):
    global model_map, router_cfg
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for e in cfg.get("model_list", []):
        model_map[e["model_name"]].append(e["litellm_params"])
    router_cfg = cfg.get("router_settings", {})


def load_keys(path="keys.tab"):
    """name space key"""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3:
                keys[parts[0]] = parts[2]       # name → key


# ── trace ─────────────────────────────────────────────────────────────────────
def trace(provider, model, name, bin, bout, code, secs):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with trace_lock:
        with open("trace.log", "a") as f:
            f.write(f"{ts} {provider} {model} {name} {bin} {bout} {code} {secs:.3f}\n")


# ── routing ───────────────────────────────────────────────────────────────────
def pick(model_name):
    deps = model_map.get(model_name)
    return random.choice(deps) if deps else None


def fallbacks(model_name):
    for fb in router_cfg.get("fallbacks", []):
        if model_name in fb:
            return fb[model_name]
        if "*" in fb:
            return fb["*"]
    return []


# ── upstream ──────────────────────────────────────────────────────────────────
def api_key_for(dep, model_name):
    if dep.get("api_key"):
        return dep["api_key"]
    if model_name in keys:
        return keys[model_name]
    prov = dep.get("model", "").split("/", 1)[0]
    if prov in keys:
        return keys[prov]
    return os.environ.get("OPENAI_API_KEY", "")


def upstream(dep, payload, model_name, suffix, stream=False):
    base = dep.get("api_base", "").rstrip("/")
    spec = dep.get("model", "")
    mid  = spec.split("/", 1)[1] if "/" in spec else spec

    body = copy.deepcopy(payload)
    body["model"] = mid

    hdrs = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization", "")
    if not auth:
        k = api_key_for(dep, model_name)
        if k:
            auth = f"Bearer {k}"
    if auth:
        hdrs["Authorization"] = auth

    kw = dict(json=body, headers=hdrs, timeout=dep.get("timeout", 120))
    if stream:
        kw["stream"] = True
    return requests.post(f"{base}/{suffix}", **kw)


# ── handler ───────────────────────────────────────────────────────────────────
def handle(suffix):
    payload = request.get_json(silent=True)
    if not payload:
        return Response('{"error":"bad json"}', 400, content_type="application/json")

    req_model = payload.get("model", "")
    stream    = payload.get("stream", False)
    retries   = router_cfg.get("num_retries", 0)
    bin       = len(request.get_data())

    for m in [req_model] + fallbacks(req_model):
        dep = pick(m)
        if not dep:
            continue

        prov = dep.get("model", "").split("/", 1)[0]

        for _ in range(retries + 1):
            t0 = time.time()
            try:
                r = upstream(dep, payload, m, suffix, stream)
                el = time.time() - t0
                code = r.status_code

                if code == 200:
                    if stream:
                        def _gen(r=r, p=prov, m_=m, rm=req_model, t0_=t0):
                            n = 0
                            for chunk in r.iter_content(chunk_size=None):
                                n += len(chunk)
                                yield chunk
                            trace(p, m_, rm, bin, n, 200, time.time() - t0_)
                        return Response(_gen(), 200,
                                        content_type=r.headers.get("Content-Type",
                                                                   "text/event-stream"))

                    bout = len(r.content)
                    trace(prov, m, req_model, bin, bout, 200, el)
                    return Response(r.content, 200,
                                    content_type=r.headers.get("Content-Type",
                                                               "application/json"))

                bout = len(r.content) if r.content else 0
                trace(prov, m, req_model, bin, bout, code, el)

            except Exception as exc:
                trace(prov, m, req_model, bin, 0, 0, time.time() - t0)

    return Response('{"error":"all attempts failed"}', 502,
                    content_type="application/json")


# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/chat/completions",    methods=["POST"])
@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    return handle("chat/completions")


@app.route("/completions",         methods=["POST"])
@app.route("/v1/completions",      methods=["POST"])
def completions():
    return handle("completions")


@app.route("/embeddings",          methods=["POST"])
@app.route("/v1/embeddings",       methods=["POST"])
def embeddings():
    return handle("embeddings")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "models": list(model_map.keys())}


# ── main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    load_config(os.environ.get("CONFIG", "config.yaml"))
    load_keys("keys.tab")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 4000)))
