#!/usr/bin/env python3
"""Keep the hub on the latest commit of skep13/agent-room main.

Run by agent-room-update.timer every few minutes. When main has a new commit it
downloads that commit, checks the Python compiles, backs up what's live, copies
the hub files over, restarts the hub and checks it answers. If it doesn't, the
backup goes back and the hub restarts on the old files.
"""
import io, json, os, py_compile, shutil, subprocess, sys, tarfile, tempfile, time, urllib.request

REPO = os.environ.get("AGENT_ROOM_REPO", "skep13/agent-room")
BRANCH = os.environ.get("AGENT_ROOM_BRANCH", "main")
LIVE = "/opt/agent-room"
STATE = "/var/lib/agent-room-update"
FILES = ["hub.py", "city.py", "connector.py"]
DIRS = ["static"]
KEEP = 5


def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "agent-room-autoupdate"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def healthy():
    for _ in range(15):
        time.sleep(2)
        ok = subprocess.run(["systemctl", "is-active", "--quiet", "agent-room"]).returncode == 0
        if ok:
            try:
                get("http://127.0.0.1:8790/api/app-downloads", timeout=5)
                return True
            except Exception:
                pass
    return False


def copy_in(src):
    for f in FILES:
        if os.path.exists(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(LIVE, f))
    for d in DIRS:
        if os.path.isdir(os.path.join(src, d)):
            shutil.copytree(os.path.join(src, d), os.path.join(LIVE, d), dirs_exist_ok=True)


def main():
    os.makedirs(STATE, exist_ok=True)
    shafile = os.path.join(STATE, "sha")
    sha = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))["sha"]
    old = open(shafile).read().strip() if os.path.exists(shafile) else ""
    if sha == old:
        return
    log(f"new commit {sha[:7]} (was {old[:7] or 'none'})")
    with tempfile.TemporaryDirectory() as tmp:
        tarfile.open(fileobj=io.BytesIO(get(f"https://codeload.github.com/{REPO}/tar.gz/{sha}", 120))).extractall(tmp)
        src = os.path.join(tmp, os.listdir(tmp)[0])
        for f in FILES:
            if os.path.exists(os.path.join(src, f)):
                py_compile.compile(os.path.join(src, f), doraise=True)
        bk = os.path.join(STATE, "backup-" + time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(bk)
        for f in FILES:
            if os.path.exists(os.path.join(LIVE, f)):
                shutil.copy2(os.path.join(LIVE, f), bk)
        for d in DIRS:
            shutil.copytree(os.path.join(LIVE, d), os.path.join(bk, d))
        copy_in(src)
    subprocess.run(["systemctl", "restart", "agent-room"], check=False)
    if healthy():
        open(shafile, "w").write(sha)
        log(f"deployed {sha[:7]}")
    else:
        log(f"{sha[:7]} didn't come up: rolling back to {bk}")
        copy_in(bk)
        subprocess.run(["systemctl", "restart", "agent-room"], check=False)
        open(shafile, "w").write(sha)          # don't retry the same broken commit every few minutes
        sys.exit(1)
    backups = sorted(b for b in os.listdir(STATE) if b.startswith("backup-"))
    for b in backups[:-KEEP]:
        shutil.rmtree(os.path.join(STATE, b), ignore_errors=True)


if __name__ == "__main__":
    main()
