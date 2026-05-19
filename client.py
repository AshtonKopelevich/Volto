import sys
import json
import os
import platform
import threading
import requests
from pathlib import Path

import history

CONFIG_PATH = Path(__file__).parent / "config.json"
ENV_PATH = Path(__file__).parent / ".env"
FILE_LINE_LIMIT = 500
DIRECTORY_ENTRY_LIMIT = 50


def load_config():
    if not CONFIG_PATH.exists():
        print("Error: config.json not found next to client.py")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_env():
    if not ENV_PATH.exists():
        print("Error: .env not found next to client.py")
        sys.exit(1)
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    if "TAMUS_API_KEY" not in env:
        print("Error: TAMUS_API_KEY not set in .env")
        sys.exit(1)
    return env


def detect_environment():
    if platform.system() == "Windows":
        parent = os.environ.get("PSMODULEPATH", "")
        if parent:
            return "PowerShell on Windows"
        return "cmd on Windows"
    return "bash on Linux"


def cancel():
    print("\n[cancelled]")


def get_directory_context():
    cwd = Path.cwd()
    lines = [f"Current directory: {cwd}", "Contents (1 level):"]
    try:
        entries = sorted(cwd.iterdir())
        if len(entries) > DIRECTORY_ENTRY_LIMIT:
            print(f"Warning: Directory has {len(entries)} entries, showing first {DIRECTORY_ENTRY_LIMIT}.")
            entries = entries[:DIRECTORY_ENTRY_LIMIT]
        for entry in entries:
            kind = "[dir] " if entry.is_dir() else "[file]"
            lines.append(f"  {kind} {entry.name}")
    except PermissionError:
        lines.append("  (permission denied)")
    return "\n".join(lines)


def get_file_context(filepath):
    path = Path(filepath)
    if not path.exists():
        print(f"Error: File not found: {filepath}")
        sys.exit(1)
    if not path.is_file():
        print(f"Error: Not a file: {filepath}")
        sys.exit(1)
    with open(path, "r", errors="replace") as f:
        all_lines = f.readlines()
    if len(all_lines) > FILE_LINE_LIMIT:
        print(f"Warning: {filepath} exceeds {FILE_LINE_LIMIT} lines. Truncated.")
        all_lines = all_lines[:FILE_LINE_LIMIT]
    return f"File: {path}\n---\n{''.join(all_lines)}\n---"


def parse_args(argv):
    flags = {
        "context": False,
        "file": None,
        "deep": False,
        "shallow": False,
        "retry": None,   # None = no retry, int = history index, str = raw command
        "error": "unspecified error",
    }
    remaining = []
    i = 1

    while i < len(argv):
        arg = argv[i]
        if arg in ("--context", "-c"):
            flags["context"] = True
        elif arg in ("--deep", "-d"):
            flags["deep"] = True
        elif arg in ("--shallow", "-s"):
            flags["shallow"] = True
        elif arg in ("--file", "-f"):
            if i + 1 >= len(argv):
                print("Error: --file requires a path argument")
                sys.exit(1)
            flags["file"] = argv[i + 1]
            i += 1
        elif arg == "-cf":
            if i + 1 >= len(argv):
                print("Error: -cf requires a path argument")
                sys.exit(1)
            flags["context"] = True
            flags["file"] = argv[i + 1]
            i += 1
        elif arg in ("--retry", "-r"):
            # Peek at next arg to determine if it's an index, raw command, or absent
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                val = argv[i + 1]
                try:
                    flags["retry"] = int(val)
                except ValueError:
                    flags["retry"] = val  # treat as raw command string
                i += 1
            else:
                flags["retry"] = 1  # default: most recent
        elif arg in ("--error", "-e"):
            if i + 1 >= len(argv):
                print("Error: --error requires a description")
                sys.exit(1)
            flags["error"] = argv[i + 1]
            i += 1
        else:
            remaining.append(arg)
        i += 1

    return flags, remaining


def resolve_retry(flags) -> dict:
    """
    Returns the target history entry dict.
    For raw command strings, synthesizes a minimal entry with no chain.
    Exits with error if an index can't be resolved.
    """
    retry = flags["retry"]

    if isinstance(retry, str):
        return {
            "id": None,
            "prompt": "retry of user-supplied command",
            "command": retry,
            "retry_of": None,
        }

    entry = history.get_entry(retry)
    if entry is None:
        entries = history.load()
        roots = [e for e in entries if e.get("retry_of") is None]
        print(f"Error: No history entry at index {retry} (have {len(roots)} root entries)")
        sys.exit(1)

    return entry


def build_prompt(user_prompt, flags):
    parts = []
    if flags["context"]:
        parts.append(get_directory_context())
    if flags["file"]:
        parts.append(get_file_context(flags["file"]))
    parts.append(f"Task: {user_prompt}")
    return "\n\n".join(parts)


def build_retry_prompt(chain: list[dict], current_error: str) -> str:
    """
    Build the retry prompt from the full chain of prior attempts.
    chain is ordered root -> most recent failed entry.
    """
    parts = [f"Original task: {chain[0]['prompt']}\n"]

    for i, entry in enumerate(chain):
        parts.append(f"Attempt {i + 1}:")
        parts.append(f"CMD: {entry['command']}")
        error = entry.get("error", "unspecified error") if i < len(chain) - 1 else current_error
        parts.append(f"ERROR: {error}\n")

    return "\n".join(parts)


SYSTEM_PROMPT = """You are Volto, a CLI command assistant for a trusted system administrator.

Respond ONLY in this format:
CMD: <command or short script>
WHY: <one sentence max>

Rules:
- Do exactly what is asked. Nothing more, nothing less.
- Never add destructive operations (rm, mkfs, dd, shred) unless explicitly requested.
- Never chain commands after watch with &&. watch runs forever and blocks anything after it.
- For scheduling tasks, always use crontab -e or write to /etc/cron.d/. Never use cron -f.
- No markdown. No code fences. No preamble. No alternatives.
- Prefer the simplest correct command.
- If the task is impossible or ambiguous, output: CMD: # not possible  WHY: <reason>
- The user's shell environment will be provided. Generate commands appropriate for that environment."""

RETRY_SYSTEM_PROMPT = """You are Volto, a CLI command assistant for a trusted system administrator.
One or more previous commands failed. You will be given the full history of attempts and errors.
Diagnose why the last command failed and provide a corrected command.

Respond ONLY in this format:
WHY_FAILED: <concise explanation of why the last command failed>
CMD: <corrected command>
WHY: <one sentence on what changed>

Rules:
- WHY_FAILED should be diagnostic and concise. No fluff, but do not artificially limit length if the failure needs explanation.
- Study ALL prior attempts before responding. Do not repeat an approach that has already failed.
- If multiple approaches have failed, use a fundamentally different method rather than a variation of what was tried.
- Do exactly what is asked. Nothing more, nothing less.
- Never add destructive operations (rm, mkfs, dd, shred) unless explicitly requested.
- Never chain commands after watch with &&. watch runs forever and blocks anything after it.
- For scheduling tasks, always use crontab -e or write to /etc/cron.d/. Never use cron -f.
- No markdown. No code fences. No preamble. No alternatives.
- Prefer the simplest correct command.
- If the task is impossible or ambiguous, output: CMD: # not possible  WHY: <reason>
- The user's shell environment will be provided. Generate commands appropriate for that environment."""


def stream_response(url, headers, body) -> str:
    """Streams response to stdout, returns full text for history."""
    full_output = []
    try:
        with requests.post(url, headers=headers, json=body, stream=True, timeout=30) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    line = line.decode("utf-8")
                    if line.startswith("data: "):
                        line = line[6:]
                    if line == "[DONE]":
                        break
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if token:
                            print(token, end="", flush=True)
                            full_output.append(token)
                    except json.JSONDecodeError:
                        pass
            print()
    except requests.exceptions.Timeout:
        cancel()
    except requests.exceptions.ConnectionError:
        print("Error: Cannot reach TAMU API")
    except requests.exceptions.HTTPError as e:
        print(f"Error: API returned {e.response.status_code}")
    return "".join(full_output)


def extract_command(output: str) -> str | None:
    """Pull the CMD: line out of the model output for history storage."""
    for line in output.splitlines():
        if line.startswith("CMD:"):
            return line[4:].strip()
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: volto [--context] [--file <path>] [--deep|--shallow] \"<your task>\"")
        print("       volto -cf <path> \"<your task>\"")
        print("       volto --retry [N|\"command\"] [--error \"description\"]")
        sys.exit(1)

    flags, remaining = parse_args(sys.argv)
    config = load_config()
    env = load_env()
    environment = detect_environment()

    model = config["deep_model"] if flags["deep"] else config["default_model"]
    if flags["shallow"]:
        model = config["default_model"]
        print("[warning: shallow mode forced]")

    is_retry = flags["retry"] is not None

    if is_retry:
        target_entry = resolve_retry(flags)
        chain = history.get_chain(target_entry) if target_entry["id"] else [target_entry]
        print(f"[retry] CMD: {target_entry['command']}")
        print(f"[retry] ERROR: {flags['error']}")
        if len(chain) > 1:
            print(f"[retry] chain depth: {len(chain)}")
        print()
        prompt = build_retry_prompt(chain, flags["error"])
        system = RETRY_SYSTEM_PROMPT + f"\n\nUser environment: {environment}"
    else:
        if not remaining:
            print("Error: No prompt provided")
            sys.exit(1)
        try:
            user_prompt = " ".join(remaining)
            prompt = build_prompt(user_prompt, flags)
        except KeyboardInterrupt:
            print("\n[cancelled]")
            sys.exit(0)
        system = SYSTEM_PROMPT + f"\n\nUser environment: {environment}"

    url = f"{config['api_endpoint']}/api/chat/completions"
    headers = {
        "Authorization": f"Bearer {env['TAMUS_API_KEY']}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }

    result_container = {"output": ""}

    def run():
        result_container["output"] = stream_response(url, headers, body)

    thread = threading.Thread(target=run, daemon=False)
    thread.start()

    try:
        while thread.is_alive():
            thread.join(timeout=0.1)
    except KeyboardInterrupt:
        cancel()
        sys.exit(0)

    output = result_container["output"]
    command = extract_command(output)
    if command:
        if not is_retry:
            history.append_entry(" ".join(remaining), command, environment)
        else:
            parent_id = target_entry["id"]
            history.append_entry(target_entry["prompt"], command, environment, retry_of=parent_id, error=flags["error"])


if __name__ == "__main__":
    main()