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

def extract_webpack_modules(player_js: str) -> Dict[str, Any]:
    # Use Node.js to extract webpack modules
    script = """
    const fs = require("fs");
    const { parse } = require("meriyah");
    const { traverse } = require("estraverse");
    
    const playerJs = fs.readFileSync(0, "utf-8");
    const ast = parse(playerJs, { ranges: true });
    
    let wpm = null;
    
    traverse(ast, {
      enter: (node) => {
        if (node.type === "VariableDeclaration") {
          const found = node.declarations.find(
            (d) => d.id.type === "Identifier" && d.id.name === "__webpack_modules__"
          );
          if (found?.init && found.init.type === "ObjectExpression") {
            wpm = found.init;
            console.error("__webpack_modules__ found at", found.init.range);
            return traverse.VisitorOption.Break;
          }
        }
      },
    });
    
    if (!wpm) throw new Error("could not find __webpack_modules__");
    
    const wpmString = playerJs.substring(wpm.start, wpm.end);
    console.log(JSON.stringify({ wpmString, start: wpm.start, end: wpm.end }));
    """
    
    try:
        result = subprocess.run(
            ["node", "-e", script],
            input=player_js,
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print(f"Error extracting webpack modules: {e.stderr}")
        raise Exception("could not find __webpack_modules__")
    except json.JSONDecodeError as e:
        print(f"Error parsing webpack modules output: {e}")
        raise Exception("could not parse __webpack_modules__")

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
    
    wpm_info = extract_webpack_modules(player_js)
    wpm_str = f"const __webpack_modules__ = {wpm_info['wpmString']}"
    
    otp_candidates = find_otp_module(player_js, wpm_info)
    
    eval_script = build_eval_script(wpm_str, otp_candidates)
    spotify_secrets = run_eval_script(eval_script)
    
    spotify_secret_bytes = secrets_to_bytes(spotify_secrets)
    spotify_secret_dict = secrets_to_dict(spotify_secrets)
    
    print(json.dumps(spotify_secrets, indent=2))
    
    with open("secrets/secrets.json", "w") as f:
        json.dump(spotify_secrets, f, indent=2)
    
    with open("secrets/secretBytes.json", "w") as f:
        json.dump(spotify_secret_bytes, f)
    
    with open("secrets/secretDict.json", "w") as f:
        json.dump(spotify_secret_dict, f)

if __name__ == "__main__":
    main()
