from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import json
import threading
import subprocess
import time

app = Flask(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_DEFAULT = "qwen2.5:3b"
MODEL_DEEP = "qwen2.5:7b"

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
- Chain multiple commands with && or use a heredoc only when necessary.
- Prefer the simplest correct command. Do not add error handling, echo statements, or extra operations unless asked.
- If the task is impossible or ambiguous, output: CMD: # not possible  WHY: <reason>
- The user's shell environment will be provided. Generate commands appropriate for that environment."""

# Patterns that trigger automatic 7B routing
DEEP_ROUTING_RULES = [
    ("deletion", [
        # Linux
        "rm ", "rm -", "rmdir", "shred", "wipe", "dd if", "mkfs", "fdisk", "parted",
        "truncate", "unlink",
        # Windows
        "del ", "rd ", "format ", "diskpart", "cipher /w", "remove-item", "clear-content",
    ]),
    ("cleanup", [
        # Linux
        "cleanup", "clean up", "free up", "purge", "autoremove", "vacuum",
        "rotate log", "clear cache", "clear tmp", "clear /tmp",
        # Windows
        "disk cleanup", "cleanmgr", "clear-recyclebin", "temp files",
        "prefetch", "clear eventlog",
    ]),
    ("scheduling", [
        # Linux
        "cron", "crontab", "schedule", "every day", "every night", "every hour",
        "every minute", "at 6", "at midnight", "systemd timer", "anacron",
        # Windows
        "task scheduler", "schtasks", "scheduled task", "new-scheduledtask",
    ]),
    ("system-wide", [
        # Linux
        "find /", "chmod -r /", "chown -r /", "full system", "entire disk",
        "root partition", "/etc/", "/sys/", "/proc/",
        # Windows
        "c:\\", "entire drive", "system32", "registry", "regedit",
        "hklm", "hkcu", "set-itemproperty",
    ]),
    ("network", [
        # Linux
        "iptables", "ufw", "firewalld", "nftables", "ip route", "ip addr",
        "netplan", "nmcli", "hostnamectl",
        # Windows
        "netsh", "new-netfirewallrule", "set-netadapter", "ipconfig /release",
        "route add", "set-dnsclientserveraddress",
    ]),
    ("user-permissions", [
        # Linux
        "useradd", "userdel", "usermod", "passwd", "chmod", "chown",
        "visudo", "sudoers", "groupadd", "groupdel",
        # Windows
        "net user", "net localgroup", "add-localuser", "set-acl", "icacls", "runas",
    ]),
    ("package-management", [
        # Linux
        "apt ", "apt-get", "dpkg", "yum ", "dnf ", "pacman", "snap install",
        "flatpak install", "pip install", "npm install -g",
        # Windows
        "winget", "choco ", "scoop ", "msiexec", "install-package",
    ]),
    ("complex-logic", [
        "alert when", "monitor and", "if cpu", "if disk", "exceeds", "threshold",
        "notify when", "watch and", "detect when", "trigger if",
    ]),
]

active_request = None
active_request_lock = threading.Lock()


def classify_prompt(prompt):
    """Returns (model, reason) or (None, None) if no match."""
    lower = prompt.lower()
    for category, keywords in DEEP_ROUTING_RULES:
        if any(kw in lower for kw in keywords):
            return MODEL_DEEP, category
    return MODEL_DEFAULT, None


def warmup():
    try:
        requests.post(OLLAMA_URL, json={
            "model": MODEL_DEFAULT,
            "prompt": "respond with the word ready",
            "stream": False
        })
        print(f"[Volto] Model {MODEL_DEFAULT} loaded and ready.")
    except requests.exceptions.ConnectionError:
        print("[Volto] Warning: Could not reach Ollama during warmup. Is it running?")


def restart_and_warmup():
    try:
        subprocess.run(["sudo", "systemctl", "restart", "ollama"], check=True)
        print("[Volto] Ollama restarted. Waiting for it to come back up...")
        time.sleep(3)
        warmup()
    except subprocess.CalledProcessError as e:
        print(f"[Volto] Failed to restart Ollama: {e}")


@app.route("/command", methods=["POST"])
def command():
    global active_request

    data = request.get_json()
    if not data or "prompt" not in data:
        return jsonify({"error": "Missing 'prompt' in request body"}), 400

    prompt = data["prompt"]
    environment = data.get("environment", "")
    mode = data.get("mode", "auto")  # "auto", "deep", "shallow"

    # Model selection
    if mode == "deep":
        model = MODEL_DEEP
        routing_msg = None
    elif mode == "shallow":
        model = MODEL_DEFAULT
        routing_msg = "[warning: shallow mode forced — safety routing bypassed]\n"
    else:
        model, reason = classify_prompt(prompt)
        routing_msg = f"[switching to deep model: detected {reason} task]\n" if reason else None

    system = SYSTEM_PROMPT
    if environment:
        system += f"\n\nUser environment: {environment}"

    def generate():
        global active_request
        try:
            # Send routing message first if needed
            if routing_msg:
                yield routing_msg

            sess = requests.Session()
            with active_request_lock:
                active_request = sess

            with sess.post(OLLAMA_URL, json={
                "model": model,
                "prompt": prompt,
                "system": system,
                "stream": True
            }, stream=True, timeout=120) as ollama_response:
                ollama_response.raise_for_status()
                for line in ollama_response.iter_lines():
                    if line:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        if token:
                            yield token
        except requests.exceptions.ConnectionError:
            yield "[Error: Cannot reach Ollama. Is it running?]"
        except requests.exceptions.HTTPError as e:
            yield f"[Error: Ollama returned {e.response.status_code}]"
        except Exception:
            pass
        finally:
            with active_request_lock:
                active_request = None

    return Response(stream_with_context(generate()), mimetype="text/plain")


@app.route("/cancel", methods=["POST"])
def cancel():
    global active_request
    with active_request_lock:
        if active_request:
            active_request.close()
            active_request = None

    thread = threading.Thread(target=restart_and_warmup)
    thread.start()
    return jsonify({"status": "cancelled"}), 200


if __name__ == "__main__":
    warmup()
    app.run(host="0.0.0.0", port=5000)