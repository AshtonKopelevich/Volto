import sys
import json
import os
import platform
import subprocess
import requests
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"
FILE_LINE_LIMIT = 500


def load_config():
    if not CONFIG_PATH.exists():
        print("Error: config.json not found next to client.py")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def detect_environment():
    if platform.system() == "Windows":
        # Check if running inside PowerShell
        parent = os.environ.get("PSMODULEPATH", "")
        if parent:
            return "PowerShell on Windows"
        return "cmd on Windows"
    return "bash on Linux"


def cancel(base_url):
    try:
        requests.post(f"{base_url}/cancel", timeout=3)
    except Exception:
        pass
    print("\n[cancelled]")


def get_directory_context():
    cwd = Path.cwd()
    lines = [f"Current directory: {cwd}", "Contents (1 level):"]
    try:
        for entry in sorted(cwd.iterdir()):
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

    truncated = len(all_lines) > FILE_LINE_LIMIT
    lines = all_lines[:FILE_LINE_LIMIT]

    if truncated:
        print(f"Warning: {filepath} exceeds {FILE_LINE_LIMIT} lines. Truncated to first {FILE_LINE_LIMIT} lines.")

    content = "".join(lines)
    return f"File: {path}\n---\n{content}\n---"


def parse_args(argv):
    """
    Returns (flags, prompt_parts) where flags is a dict.
    Supported:
      --context / -c
      --file <path> / -f <path>
      -cf <path>  (shorthand for --context --file)
    """
    flags = {"context": False, "file": None, "deep": False, "shallow": False}
    remaining = []
    i = 1  # skip script name

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
        else:
            remaining.append(arg)
        i += 1

    return flags, remaining


def build_prompt(user_prompt, flags):
    parts = []

    if flags["context"]:
        parts.append(get_directory_context())

    if flags["file"]:
        parts.append(get_file_context(flags["file"]))

    parts.append(f"Task: {user_prompt}")
    return "\n\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: volto [--context] [--file <path>] [--deep|--shallow] \"<your task>\"")
        print("       volto -cf <path> \"<your task>\"")
        sys.exit(1)

    flags, remaining = parse_args(sys.argv)

    if not remaining:
        print("Error: No prompt provided")
        sys.exit(1)

    try:
        user_prompt = " ".join(remaining)
        prompt = build_prompt(user_prompt, flags)
    except KeyboardInterrupt:
        print("\n[cancelled]")
        sys.exit(0)

    config = load_config()
    environment = detect_environment()

    if flags["deep"]:
        mode = "deep"
    elif flags["shallow"]:
        mode = "shallow"
    else:
        mode = "auto"

    url = f"{config['server_host']}:{config['server_port']}"
    payload = {"prompt": prompt, "mode": mode, "environment": environment}

    def stream_response():
        try:
            with requests.post(f"{url}/command", json=payload, stream=True, timeout=120) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=None):
                    if chunk:
                        print(chunk.decode("utf-8"), end="", flush=True)
                print()
        except requests.exceptions.Timeout:
            cancel(url)
        except requests.exceptions.ConnectionError:
            print(f"Error: Cannot reach Volto server at {url}")
        except requests.exceptions.HTTPError as e:
            print(f"Error: Server returned {e.response.status_code}")

    thread = threading.Thread(target=stream_response, daemon=False)
    thread.start()

    try:
        while thread.is_alive():
            thread.join(timeout=0.1)
    except KeyboardInterrupt:
        cancel(url)
        sys.exit(0)


if __name__ == "__main__":
    main()