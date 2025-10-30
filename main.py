import os
import json
import re
import subprocess
import sys
from typing import List, Dict, Any, Optional, Tuple
import requests
from pathlib import Path

# Define types
Candidate = Dict[str, int]
SpotifySecrets = List[Dict[str, Any]]
SpotifySecretDict = Dict[str, List[int]]
SpotifySecretBytes = List[Dict[str, Any]]

# Constants
HOOK = """(() => {
  if (globalThis.__secretHookInstalled) return;
  globalThis.__secretHookInstalled = true;
  globalThis.__captures = [];
  globalThis.__validUntil = null;
  Object.defineProperty(Object.prototype, "secret", {
    configurable: true,
    set: function (v) {
      __captures.push(this);
      Object.defineProperty(this, "secret", {
        value: v,
        writable: true,
        configurable: true,
        enumerable: true,
      });
    },
  });
})();
"""

MOD_LOADER = """
const modEnv = {};
let currentlyImporting = null;

function n(id) {
  if (modEnv[id]) {
    return modEnv[id];
  }
  if (__webpack_modules__[id]) {
    modEnv[id] = {};
    currentlyImporting = id
    __webpack_modules__[id]({id}, modEnv[id], n);
    console.error("imported", id)
    currentlyImporting = null;
    return modEnv[id];
  }
  console.error(`failed to import ${id} (during import of ${currentlyImporting})`);
  return {};
}
n.d = () => {};

"""

READOUT = """
globalThis.__captures.filter((c) => c.secret && c.version)
"""

HTTP_OPTIONS = {
    "headers": {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    }
}

def is_ok(status: int) -> None:
    if status < 200 or status > 299:
        raise Exception(f"HTTP status code {status}")

def fetch_player_js_url() -> str:
    response = requests.get("https://open.spotify.com/", headers=HTTP_OPTIONS["headers"])
    is_ok(response.status_code)
    html = response.text
    re_player_js = r'"(https://[^" ]+/web-player\.[0-9a-f]+\.js)"'
    match = re.search(re_player_js, html)
    if not match:
        raise Exception("Player JS URL not found")
    return match.group(1)

def fetch_player_js(player_js_url: str) -> str:
    response = requests.get(player_js_url, headers=HTTP_OPTIONS["headers"])
    is_ok(response.status_code)
    ct = response.headers.get("content-type", "")
    if not re.search(r"text/javascript\b", ct):
        raise Exception(f"Invalid content type: {ct}")
    return response.text

def _find_matching_brace(js: str, start_idx: int) -> int:
    """
    Given js string and index of an opening '{', return index of matching closing '}'.
    This function is careful to skip braces inside single/double/backtick strings and comments.
    """
    i = start_idx
    n = len(js)
    if i >= n or js[i] != "{":
        raise ValueError("start_idx must point to '{'")
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    in_regex = False
    escape = False
    i += 0
    while i < n:
        ch = js[i]
        # Handle escape inside strings
        if escape:
            escape = False
            i += 1
            continue

        # Handle string toggling
        if in_single:
            if ch == "\\":
                escape = True
            elif ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_double = False
            i += 1
            continue
        if in_backtick:
            if ch == "\\":
                escape = True
            elif ch == "`":
                in_backtick = False
            i += 1
            continue
        # handle comments
        if ch == "/":
            # lookahead
            nxt = js[i+1] if i+1 < n else ""
            if nxt == "/":
                # skip single-line comment
                i += 2
                while i < n and js[i] not in "\r\n":
                    i += 1
                continue
            elif nxt == "*":
                # skip multi-line comment
                i += 2
                while i+1 < n and not (js[i] == "*" and js[i+1] == "/"):
                    i += 1
                i += 2
                continue
            else:
                # Could be a regex literal — naive handling: if previous non-space char suggests start of expression, skip until next '/'
                # This is heuristic; we primarily care about strings/comments/braces, not perfect regex parsing.
                # To avoid false positives we will not try to parse regex here.
                pass

        # Now handle normal chars
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "`":
            in_backtick = True

        i += 1

    raise ValueError("No matching closing brace found")

def _extract_object_at(js: str, brace_open_idx: int) -> str:
    end_idx = _find_matching_brace(js, brace_open_idx)
    return js[brace_open_idx:end_idx+1]

def extract_webpack_modules(player_js: str) -> Dict[str, Any]:
    """
    Robust extraction of the webpack modules object (or close equivalent) from Spotify's player.js.

    This function returns a dict with keys:
      - 'wpmString': the exact JavaScript source text representing the modules object
      - 'start': starting index in the original player_js string (int)
      - 'end': ending index in the original player_js string (int)
    """
    # Candidate regex patterns (ordered)
    # 1) Explicit __webpack_modules__ assignment
    patterns = [
        r"__webpack_modules__\s*=\s*\{",             # __webpack_modules__ = {
        r"var\s+[a-zA-Z0-9_$]+\s*=\s*\{",           # var a = {
        r"let\s+[a-zA-Z0-9_$]+\s*=\s*\{",           # let a = {
        r"[a-zA-Z0-9_$]+\s*=\s*\{",                 # a = {
        # webpackBootstrap style: /******/ (() => { var __webpack_modules__ = ({ ... })
        r"\/\*{6,}\s*\(\/\*{0,}\)\s*=>\s*\{\s*var\s+[a-zA-Z0-9_$]+\s*=\s*\(\{",
        # look for a large object literal that looks like numeric keys: 0:{...},1:{...},...
        r"\{\s*(?:\d+\s*:\s*function\b)"
    ]

    # Try to find using patterns and then extract balanced braces
    for pat in patterns:
        for m in re.finditer(pat, player_js):
            # find the position of the first '{' after the match start
            span_start = m.start()
            # search for the next '{' from m.start()
            open_brace_idx = player_js.find("{", m.start())
            if open_brace_idx == -1:
                continue
            try:
                obj_str = _extract_object_at(player_js, open_brace_idx)
                return {"wpmString": obj_str, "start": open_brace_idx, "end": open_brace_idx + len(obj_str)}
            except Exception:
                # if matching fails, keep searching
                continue

    # Secondary heuristic: sometimes modules are wrapped like (t={123:function...})
    # search for patterns like "=({<digits>:function"
    heur = re.search(r"=\s*\(\s*\{[0-9]+\s*:\s*function", player_js)
    if heur:
        open_brace_idx = player_js.find("{", heur.start())
        try:
            obj_str = _extract_object_at(player_js, open_brace_idx)
            return {"wpmString": obj_str, "start": open_brace_idx, "end": open_brace_idx + len(obj_str)}
        except Exception:
            pass

    # Last resort: try to find the largest object-like literal in the file that contains "function"
    # This is a fallback and not perfect but often finds the modules object when minified/unusual
    largest_obj = None
    for m in re.finditer(r"\{", player_js):
        try:
            obj = _extract_object_at(player_js, m.start())
            # quick heuristic: object should be fairly large and contain "function" and numeric keys
            if len(obj) > 2000 and "function" in obj:
                if re.search(r"\d+\s*:", obj):
                    largest_obj = (m.start(), obj)
                    break
                # accept if it contains many occurrences of "function"
                if obj.count("function") >= 3:
                    largest_obj = (m.start(), obj)
                    break
        except Exception:
            continue

    if largest_obj:
        start_idx, obj_str = largest_obj
        return {"wpmString": obj_str, "start": start_idx, "end": start_idx + len(obj_str)}

    # If none worked, raise a clear exception with a short diagnostic
    # include a short snippet around likely areas to help debugging
    snippet = player_js[:2000] if len(player_js) > 2000 else player_js
    raise Exception("could not find __webpack_modules__ (tried multiple heuristics). Sample start of file:\n" + snippet[:1000])

def find_otp_module(player_js: str, wpm_info: Dict[str, Any]) -> List[Candidate]:
    # Use Node.js to find OTP modules
    script = """
    const fs = require("fs");
    const { parse } = require("meriyah");
    
    const playerJs = fs.readFileSync(0, "utf-8");
    const wpmInfo = JSON.parse(process.argv[2]);
    
    const searchPatterns = [
      "Hash#digest()",
      ".validUntil",
      ".secrets",
      '"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/="',
    ];
    
    const candidates = [];
    
    const wpmString = wpmInfo.wpmString;
    const ast = parse("const __webpack_modules__ = " + wpmString, { ranges: true });
    
    const wpmNode = ast.body[0].declarations[0].init;
    
    wpmNode.properties.forEach((p) => {
      if (
        p.type === "Property" &&
        p.key.type === "Literal" &&
        typeof p.key.value === "number" &&
        (p.value.type === "ArrowFunctionExpression" ||
          p.value.type === "FunctionExpression") &&
        p.value.body
      ) {
        const body_js = playerJs.substring(p.value.body.start, p.value.body.end);
        const prio = searchPatterns.findIndex((x) => body_js.includes(x));
        if (prio !== -1) {
          candidates.push({ key: p.key.value, prio });
        }
      }
    });
    
    if (candidates.length === 0) throw new Error("could not find OTP module");
    candidates.sort((a, b) => b.prio - a.prio);
    
    console.log(JSON.stringify(candidates));
    """
    
    try:
        result = subprocess.run(
            ["node", "-e", script, "-", json.dumps(wpm_info)],
            input=player_js,
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print(f"Error finding OTP module: {e.stderr}")
        raise Exception("could not find OTP module")
    except json.JSONDecodeError as e:
        print(f"Error parsing OTP module output: {e}")
        raise Exception("could not parse OTP module")

def build_eval_script(wpm_str: str, otp_candidates: List[Candidate]) -> str:
    otp_code = "\n\n"
    for c in otp_candidates:
        otp_code += f"n({c['key']});\n"
    
    return HOOK + MOD_LOADER + wpm_str + otp_code + READOUT

def run_eval_script(eval_script: str) -> SpotifySecrets:
    # Use Node.js to run the evaluation script
    script = """
    const fs = require("fs");
    const evalScript = fs.readFileSync(0, "utf-8");
    const result = eval(evalScript);
    console.log(JSON.stringify(result));
    """
    
    try:
        result = subprocess.run(
            ["node", "-e", script],
            input=eval_script,
            capture_output=True,
            text=True,
            check=True
        )
        secrets = json.loads(result.stdout.strip())
        
        if not secrets or not isinstance(secrets, list):
            raise ValueError("Invalid secrets format")
        
        for item in secrets:
            if not isinstance(item, dict) or "secret" not in item or "version" not in item:
                raise ValueError("Invalid secret item format")
            if not isinstance(item["secret"], str) or not item["secret"]:
                raise ValueError("Invalid secret value")
            if not isinstance(item["version"], int) or item["version"] <= 0:
                raise ValueError("Invalid version value")
        
        secrets.sort(key=lambda x: x["version"])
        return secrets
    except subprocess.CalledProcessError as e:
        print(f"Error running eval script: {e.stderr}")
        raise Exception("could not run eval script")
    except json.JSONDecodeError as e:
        print(f"Error parsing eval script output: {e}")
        raise Exception("could not parse eval script output")

def secrets_to_bytes(secrets: SpotifySecrets) -> SpotifySecretBytes:
    return [
        {"version": item["version"], "secret": [ord(c) for c in item["secret"]]}
        for item in secrets
    ]

def secrets_to_dict(secrets: SpotifySecrets) -> SpotifySecretDict:
    return {
        str(item["version"]): [ord(c) for c in item["secret"]]
        for item in secrets
    }

def main():
    os.makedirs("tmp", exist_ok=True)
    os.makedirs("secrets", exist_ok=True)
    
    player_js = None
    
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r") as f:
            player_js = f.read()
    else:
        player_js_url = fetch_player_js_url()
        print(f"Player JS URL: {player_js_url}", file=sys.stderr)
        
        last_player_js_url = ""
        try:
            with open("tmp/playerUrl.txt", "r") as f:
                last_player_js_url = f.read().strip()
        except FileNotFoundError:
            pass
        
        if player_js_url == last_player_js_url:
            print("no player updates", file=sys.stderr)
            return
        
        player_js = fetch_player_js(player_js_url)
        with open("tmp/playerUrl.txt", "w") as f:
            f.write(player_js_url)
    
    # Extract webpack modules
    wpm_info = extract_webpack_modules(player_js)
    wpm_str = f"const __webpack_modules__ = {wpm_info['wpmString']}"
    
    # Find OTP modules
    otp_candidates = find_otp_module(player_js, wpm_info)
    
    # Build and run evaluation script
    eval_script = build_eval_script(wpm_str, otp_candidates)
    spotify_secrets = run_eval_script(eval_script)
    
    # Convert to different formats
    spotify_secret_bytes = secrets_to_bytes(spotify_secrets)
    spotify_secret_dict = secrets_to_dict(spotify_secrets)
    
    # Print and save results
    print(json.dumps(spotify_secrets, indent=2))
    
    with open("secrets/secrets.json", "w") as f:
        json.dump(spotify_secrets, f, indent=2)
    
    with open("secrets/secretBytes.json", "w") as f:
        json.dump(spotify_secret_bytes, f)
    
    with open("secrets/secretDict.json", "w") as f:
        json.dump(spotify_secret_dict, f)

if __name__ == "__main__":
    main()
