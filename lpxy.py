#!/usr/bin/env python3
"""
litellm proxy — simple Flask app.
config.yaml → routing,  keys.tab → api keys,  costs.tab → pricing,
api_base.tab → provider endpoints,  trace.log → audit,
errors.log → exceptions,  debug.log → lookups (DEBUG=1)
"""

from flask import Flask, request, Response
import yaml, requests, random, time, os, copy, threading, json, traceback
from collections import defaultdict
from urllib.parse import urlparse

app = Flask(__name__)

DEBUG = os.environ.get("DEBUG", "0") == "1"

model_map:  dict[str, list[dict]] = defaultdict(list)
router_cfg: dict = {}
keys:       dict[str, str] = {}
costs:      dict[tuple[str, str], tuple[float | None, float | None]] = {}
bases:      dict[str, str] = {}
err_lock   = threading.Lock()
trace_lock = threading.Lock()
debug_lock = threading.Lock()


# ── debug ─────────────────────────────────────────────────────────────────────
def debug(msg):
    if not DEBUG:
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with debug_lock:
        with open("debug.log", "a") as f:
            f.write(f"{ts} {msg}\n")


# ── load ──────────────────────────────────────────────────────────────────────
def load_config(path="config.yaml"):
    global model_map, router_cfg
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for e in cfg.get("model_list", []):
        name = e["model_name"]
        params = e["litellm_params"]
        model_map[name].append(params)
        debug(f"config  model={name}  params={params}")
    router_cfg = cfg.get("router_settings", {})
    debug(f"config  router_settings={router_cfg}")


def load_keys(path="keys.tab"):
    """provider key"""
    if not os.path.exists(path):
        debug(f"keys    {path} not found")
        return
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                keys[parts[0]] = parts[1]
                debug(f"keys    provider={parts[0]}  key={parts[1][:8]}...")
    debug(f"keys    loaded {len(keys)} entries")


def load_costs(path="costs.tab"):
    """name provider in/token out/token $in/M $out/M"""
    if not os.path.exists(path):
        debug(f"costs   {path} not found")
        return
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 6:
                try:
                    tin  = float(parts[2])
                    tout = float(parts[3])
                except ValueError:
                    tin, tout = None, None
                costs[(parts[0], parts[1])] = (tin, tout)
                debug(f"costs   name={parts[0]}  provider={parts[1]}  "
                      f"in/token={parts[2]}  out/token={parts[3]}")
    debug(f"costs   loaded {len(costs)} entries")


def load_bases(path="api_base.tab"):
    """provider http://api_base/"""
    if not os.path.exists(path):
        debug(f"bases   {path} not found")
        return
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                bases[parts[0]] = parts[1]
                debug(f"bases   provider={parts[0]}  api_base={parts[1]}")
    debug(f"bases   loaded {len(bases)} entries")


# ── errors.log ────────────────────────────────────────────────────────────────
def log_error(exc):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    with err_lock:
        with open("errors.log", "a") as f:
            f.write(f"{ts} {''.join(tb)}\n")


# ── resolve api_base ──────────────────────────────────────────────────────────
def provider_for_model(model_name):
    """Get the provider from costs.tab for a given model_name."""
    for (n, prov) in costs:
        if n == model_name:
            debug(f"provider  model={model_name}  → {prov}  (from costs.tab)")
            return prov
    return None


def resolve_base(dep, model_name):
    """Resolve api_base: costs.tab provider → api_base.tab → spec prefix → dep fallback."""
    prov = provider_for_model(model_name)
    if prov and prov in bases:
        debug(f"base    model={model_name}  costs_provider={prov}  → {bases[prov]}")
        return bases[prov]

    spec = dep.get("model", "")
    spec_prov = spec.split("/", 1)[0] if "/" in spec else spec
    if spec_prov in bases:
        debug(f"base    model={model_name}  spec_provider={spec_prov}  → {bases[spec_prov]}")
        return bases[spec_prov]

    explicit = dep.get("api_base", "")
    if explicit:
        debug(f"base    model={model_name}  → explicit {explicit}")
        return explicit

    debug(f"base    model={model_name}  → None (no match)")
    return None


# ── cost lookup ───────────────────────────────────────────────────────────────
def rates_for(dep, model_name):
    spec = dep.get("model", "")
    spec_prov = spec.split("/", 1)[0] if "/" in spec else spec
    costs_prov = provider_for_model(model_name) or ""

    for key in [(model_name, costs_prov), (model_name, spec_prov)]:
        if key in costs:
            debug(f"rates   match={key}  rates={costs[key]}")
            return costs[key]

    for (n, _), v in costs.items():
        if n == model_name:
            debug(f"rates   name-only match=({n},*)  rates={v}")
            return v

    debug(f"rates   no match for model={model_name}")
    return (None, None)


def calc_cost(usage, dep, model_name):
    rin, rout = rates_for(dep, model_name)
    if rin is None or rout is None:
        return ("-", "-")
    tin  = usage.get("prompt_tokens", 0) or 0
    tout = usage.get("completion_tokens", 0) or 0
    cin  = tin * rin
    cout = tout * rout
    debug(f"cost    prompt_tokens={tin}  completion_tokens={tout}  "
          f"rate_in={rin}  rate_out={rout}  $in={cin:.6f}  $out={cout:.6f}")
    return (f"${cin:.6f}", f"${cout:.6f}")


def usage_from_response(r):
    try:
        return r.json().get("usage") or {}
    except Exception:
        return {}


# ── trace ─────────────────────────────────────────────────────────────────────
def trace(provider, model, name, bin, bout, code, secs, cin, cout):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with trace_lock:
        with open("trace.log", "a") as f:
            f.write(f"{ts} {provider} {model} {name} {bin} {bout} {code} {secs:.3f} {cin} {cout}\n")


# ── routing ───────────────────────────────────────────────────────────────────
def pick(model_name):
    deps = model_map.get(model_name)
    if not deps:
        debug(f"pick    model={model_name}  → None (not in model_map)")
        return None
    dep = random.choice(deps)
    debug(f"pick    model={model_name}  → model={dep.get('model')}")
    return dep


def fallbacks(model_name):
    for fb in router_cfg.get("fallbacks", []):
        if model_name in fb:
            result = fb[model_name]
            debug(f"fallback  model={model_name}  → {result}")
            return result
        if "*" in fb:
            result = fb["*"]
            debug(f"fallback  model={model_name}  via '*'  → {result}")
            return result
    debug(f"fallback  model={model_name}  → []")
    return []


# ── upstream ──────────────────────────────────────────────────────────────────
def api_key_for(dep, model_name):
    if dep.get("api_key"):
        debug(f"apikey  → dep.api_key={dep['api_key'][:8]}...")
        return dep["api_key"]
    for prov in (provider_for_model(model_name),
                 dep.get("model", "").split("/", 1)[0]):
        if prov and prov in keys:
            debug(f"apikey  provider={prov}  → keys.tab by provider")
            return keys[prov]
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if env_key:
        debug(f"apikey  → OPENAI_API_KEY env")
        return env_key
    debug(f"apikey  → None (no key found)")
    return ""


def upstream(dep, payload, model_name, suffix, stream=False):
    base = resolve_base(dep, model_name)
    if not base:
        raise ValueError(f"no api_base for model '{model_name}'")
    base = base.rstrip("/")

    spec = dep.get("model", "")
    mid  = spec.split("/", 1)[1] if "/" in spec else spec

    body = copy.deepcopy(payload)
    body["model"] = mid

    url = f"{base}/{suffix}"

    hdrs = {"Content-Type": "application/json"}
    auth = request.headers.get("Authorization", "")
    if not auth:
        k = api_key_for(dep, model_name)
        if k:
            auth = f"Bearer {k}"
    if auth:
        hdrs["Authorization"] = auth

    debug(f"upstream  url={url}  model={mid}  stream={stream}  "
          f"auth={'yes' if hdrs.get('Authorization') else 'NO'}")

    kw = dict(json=body, headers=hdrs, timeout=dep.get("timeout", 120))
    if stream:
        kw["stream"] = True
    return requests.post(url, **kw)


# ── handler ───────────────────────────────────────────────────────────────────
def handle(suffix):
    payload = request.get_json(silent=True)
    if not payload:
        return Response('{"error":"bad json"}', 400, content_type="application/json")

    req_model = payload.get("model", "")
    stream    = payload.get("stream", False)
    retries   = router_cfg.get("num_retries", 0)
    bin       = len(request.get_data())

    candidates = [req_model] + fallbacks(req_model)
    debug(f"request model={req_model}  stream={stream}  retries={retries}  "
          f"bytes_in={bin}  candidates={candidates}")

    last_resp = None
    last_code = 502

    for m in candidates:
        dep = pick(m)
        if not dep:
            debug(f"skip    model={m}  no deployment in model_map")
            continue

        prov = provider_for_model(m) or dep.get("model", "").split("/", 1)[0]

        for attempt in range(retries + 1):
            t0 = time.time()
            try:
                r = upstream(dep, payload, m, suffix, stream)
                el   = time.time() - t0
                code = r.status_code
                debug(f"upstream  status={code}  elapsed={el:.3f}s  attempt={attempt}")

                if code == 200:
                    if stream:
                        def _gen(r=r, p=prov, m_=m, rm=req_model,
                                 t0_=t0, d=dep):
                            bout = 0
                            usage = {}
                            for chunk in r.iter_content(chunk_size=None):
                                bout += len(chunk)
                                yield chunk
                            try:
                                lines = chunk.decode().strip().split("\n")
                                for line in reversed(lines):
                                    if line.startswith("data: "):
                                        obj = json.loads(line[6:])
                                        if "usage" in obj and obj["usage"]:
                                            usage = obj["usage"]
                                            break
                            except Exception:
                                pass
                            cin, cout = calc_cost(usage, d, m_) if usage else ("-", "-")
                            trace(p, m_, rm, bin, bout, 200,
                                  time.time() - t0_, cin, cout)
                        return Response(_gen(), 200,
                                        content_type=r.headers.get(
                                            "Content-Type", "text/event-stream"))

                    bout = len(r.content)
                    usage = usage_from_response(r)
                    cin, cout = calc_cost(usage, dep, m)
                    trace(prov, m, req_model, bin, bout, 200, el, cin, cout)
                    return Response(r.content, 200,
                                    content_type=r.headers.get(
                                        "Content-Type", "application/json"))

                # non-200: save it, trace it
                bout = len(r.content) if r.content else 0
                trace(prov, m, req_model, bin, bout, code, el, "-", "-")
                last_resp = r
                last_code = code
                if code < 500:
                    debug(f"abort   model={m}  status={code} is 4xx, no retry")
                    break

            except Exception as exc:
                log_error(exc)
                trace(prov, m, req_model, bin, 0, 0,
                      time.time() - t0, "-", "-")
                debug(f"abort   model={m}  exception={exc}, no retry")
                break

    # all attempts failed — return the last upstream error to the client
    if last_resp is not None:
        debug(f"fail    returning upstream {last_code} to client")
        return Response(last_resp.content, last_code,
                        content_type=last_resp.headers.get(
                            "Content-Type", "application/json"))

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
    debug("starting")
    load_config(os.environ.get("CONFIG", "config.yaml"))
    load_keys("keys.tab")
    load_costs("costs.tab")
    load_bases("api_base.tab")
    debug(f"model_map keys={list(model_map.keys())}")
    debug(f"bases     keys={list(bases.keys())}")
    port = int(os.environ.get("PORT", 4001))
    debug(f"listening on port {port}")
    app.run(host="0.0.0.0", port=port)

