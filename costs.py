#!/usr/bin/env python3
"""
costs.py — Build a cost table from config.yaml + keys.tab + litellm pricing JSON.

  1. Downloads litellm model_prices_and_context_window.json -> costs.json
  2. Reads config.yaml model_list entries
  3. Reads keys.tab  (format per line: "<provider> <api_key>")
  4. For each entry: model_info overrides first, then falls back to costs.json
  5. Prints SPACE separated columns:
        name provider in/token out/token in/M out/M
     (missing values are printed as "-")
  6. Anything not resolvable from config.yaml, keys.tab or costs.json is an
     ERROR printed to STDERR (unknown provider, unknown model, missing or
     zero cost, ...). Exit code is 1 if any error occurred.

Provider rules:
  - api_base is an IP / localhost  -> local route, provider = host:port
    (the model prefix such as 'openai/' is only the wire protocol)
  - api_base is another hostname   -> provider = that host[:port]
  - no api_base                    -> cloud, provider = model string prefix

Pricing rules:
  - model_info in config.yaml always wins.
  - cloud: full model path, then bare name (prefix stripped).
  - local: the upstream model is still priced from costs.json, using the bare
    name (dot/dash spellings tried), then a '<vendor>/<name>' suffix match
    (first-party vendor preferred; ambiguous matches are an error).
"""

import json, sys, os, argparse, ipaddress, urllib.request
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required:  pip install pyyaml")

COSTS_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "refs/heads/main/model_prices_and_context_window.json"
)
COSTS_FILE = "costs.json"
CONFIG_FILE = "config.yaml"
KEYS_FILE = "keys.tab"

FIRST_PARTY = ("openai", "anthropic", "gemini", "moonshot", "deepseek",
               "mistral", "cohere", "xai")

ERRORS = 0


def err(msg):
    global ERRORS
    ERRORS += 1
    print(f"ERROR: {msg}", file=sys.stderr)


# ── helpers ──────────────────────────────────────────────────────────────

def download_costs(force=False):
    if force or not os.path.exists(COSTS_FILE):
        print(f"downloading {COSTS_URL}", file=sys.stderr)
        urllib.request.urlretrieve(COSTS_URL, COSTS_FILE)
        print(f"saved {COSTS_FILE}", file=sys.stderr)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_keys(path):
    """keys.tab: '<provider> <api_key>' per line (split on first whitespace)."""
    keys = {}
    if not os.path.exists(path):
        err(f"{path} not found - no provider can be resolved to an api key")
        return keys
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                err(f"{path}:{n} malformed line (expected '<provider> <api_key>')")
                continue
            keys[parts[0]] = parts[1].strip()
    return keys


def is_local_base(api_base):
    """True if api_base points at a raw IP / localhost (self-hosted endpoint)."""
    if not api_base:
        return False
    host = urlparse(api_base).hostname
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def derive_provider(litellm_params):
    """Local (api_base is an IP): host:port. Otherwise cloud: model prefix.
    The prefix (e.g. 'openai/') is only the wire protocol when api_base is local."""
    api_base = litellm_params.get("api_base")
    if is_local_base(api_base):
        p = urlparse(api_base)
        return p.hostname + (f":{p.port}" if p.port else "")
    if api_base:  # custom hostname endpoint, treat host as provider
        p = urlparse(api_base)
        if p.hostname:
            return p.hostname + (f":{p.port}" if p.port else "")
    model = litellm_params.get("model", "") or ""
    return model.split("/", 1)[0] if "/" in model else model


def _variants(name):
    """claude-haiku-4.5 <-> claude-haiku-4-5 (litellm is inconsistent)."""
    out = [name]
    for alt in (name.replace(".", "-"), name.replace("-", ".")):
        if alt not in out:
            out.append(alt)
    return out


def _price(entry):
    return (entry.get("input_cost_per_token"), entry.get("output_cost_per_token"))


def lookup_model(costs, model_path, local=False):
    """Returns (entry, problem). At most one of them is not None.
    Cloud: full path, then bare name. Local (api_base is an IP): the prefix is
    only the wire protocol, so resolve the bare upstream model name."""
    if not local:
        if model_path in costs:
            return costs[model_path], None
        if "/" in model_path and model_path.split("/", 1)[1] in costs:
            return costs[model_path.split("/", 1)[1]], None
        return None, None

    bare = model_path.split("/", 1)[1] if "/" in model_path else model_path
    variants = _variants(bare)

    for v in variants:                      # 1. exact bare key
        if v in costs:
            return costs[v], None

    hits = sorted({k for v in variants for k in costs if k.endswith("/" + v)})
    if not hits:
        return None, None

    for k in hits:                          # 2. first-party vendor wins
        if k.split("/", 1)[0] in FIRST_PARTY:
            return costs[k], None

    if len({_price(costs[k]) for k in hits}) == 1:   # 3. all hits agree
        return costs[hits[0]], None

    return None, (f"ambiguous local model '{model_path}': {len(hits)} entries in "
                  f"{COSTS_FILE} with different prices ({', '.join(hits[:5])}); "
                  f"set model_info in {CONFIG_FILE}")


def fmt_per_m(val):
    if val is None:
        return "-"
    per_m = val * 1_000_000
    if per_m == 0:
        return "$0"
    if per_m >= 1:
        return f"${per_m:.2f}"
    return f"${per_m:.4f}"


def fmt_token(val):
    if val is None:
        return "-"
    if val == 0:
        return "0"
    return f"{val:.2e}"


def cell(s):
    """Keep output strictly space separated: no embedded whitespace in a cell."""
    s = str(s).strip()
    return "_".join(s.split()) if s else "-"


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Cost table from config.yaml + keys.tab + litellm pricing")
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download pricing JSON")
    args = ap.parse_args()

    try:
        download_costs(force=args.refresh)
        costs = load_json(COSTS_FILE)
    except Exception as e:
        err(f"cannot obtain {COSTS_FILE}: {e}")
        costs = {}

    try:
        config = load_config(CONFIG_FILE) or {}
    except Exception as e:
        sys.exit(f"ERROR: cannot read {CONFIG_FILE}: {e}")

    keys = load_keys(KEYS_FILE)
    models = config.get("model_list") or []
    if not models:
        err(f"{CONFIG_FILE} has no model_list entries")

    print("name provider in/token out/token in/M out/M")

    for i, entry in enumerate(models, 1):
        name = entry.get("model_name") or ""
        params = entry.get("litellm_params") or {}
        model_path = params.get("model") or ""
        info = entry.get("model_info") or {}
        label = name or f"entry#{i}"
        local = is_local_base(params.get("api_base"))

        if not name:
            err(f"entry#{i}: model_name missing in {CONFIG_FILE}")
        if not model_path:
            err(f"{label}: litellm_params.model missing in {CONFIG_FILE}")

        provider = derive_provider(params)
        if not provider:
            err(f"{label}: provider cannot be derived (no api_base, no model)")
        elif provider not in keys:
            err(f"{label}: provider '{provider}' is unknown - not listed in "
                f"{KEYS_FILE}, cannot resolve it")

        in_cost = info.get("input_cost_per_token")
        out_cost = info.get("output_cost_per_token")

        if in_cost is None or out_cost is None:
            hit, problem = (lookup_model(costs, model_path, local)
                            if model_path else (None, None))
            if hit is None:
                if problem:
                    err(f"{label}: {problem}")
                elif in_cost is None and out_cost is None:
                    err(f"{label}: model '{model_path}' not found in "
                        f"{COSTS_FILE} and no model_info override in {CONFIG_FILE}")
            else:
                if in_cost is None:
                    in_cost = hit.get("input_cost_per_token")
                if out_cost is None:
                    out_cost = hit.get("output_cost_per_token")

        for what, val in (("input", in_cost), ("output", out_cost)):
            if val is None:
                err(f"{label}: {what} cost unknown (not in model_info or {COSTS_FILE})")
            elif val == 0:
                err(f"{label}: {what} cost is zero")

        print(" ".join(cell(x) for x in (
            name or "-", provider or "-",
            fmt_token(in_cost), fmt_token(out_cost),
            fmt_per_m(in_cost), fmt_per_m(out_cost))))

    if ERRORS:
        print(f"{ERRORS} error(s) found", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

