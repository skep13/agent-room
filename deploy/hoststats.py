#!/usr/bin/env python3
"""Runs on lab1 (the Proxmox host) every 2 minutes: writes RAM, disk, load, containers and
backups into the room's container as /var/lib/agent-room/host.json for the admin dashboard.
Install: /usr/local/sbin/agent-room-hoststats + agent-room-hoststats.timer (see deploy/README)."""
import json, os, shutil, subprocess, time

CT = os.environ.get("ROOM_CT", "115")

def meminfo():
    m = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    return m

def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""

m = meminfo()
du = shutil.disk_usage("/")
out = {
    "time": time.time(), "host": os.uname().nodename, "cpus": os.cpu_count(),
    "load": list(os.getloadavg()), "uptime": float(open("/proc/uptime").read().split()[0]),
    "mem": {"total": m["MemTotal"], "available": m["MemAvailable"]},
    "swap": {"total": m["SwapTotal"], "used": m["SwapTotal"] - m["SwapFree"]},
    "disk": {"total": du.total, "used": du.used},
    "containers": [], "backups": [],
}
for line in run("pvesm", "status").splitlines()[1:]:
    f = line.split()
    if len(f) >= 6 and f[0] == "local-lvm":
        out["thin"] = {"total": int(f[3]) * 1024, "used": int(f[4]) * 1024}
try:
    cts = json.loads(run("pvesh", "get", f"/nodes/{os.uname().nodename}/lxc", "--output-format", "json") or "[]")
    out["containers"] = sorted(({"id": int(c["vmid"]), "name": c.get("name", ""), "status": c.get("status", ""),
                                 "mem": c.get("mem"), "maxmem": c.get("maxmem")} for c in cts), key=lambda c: c["id"])
except Exception:
    pass
out["backups"] = sorted(b for b in run("pct", "exec", CT, "--", "sh", "-c", "ls -1d /root/backup-* 2>/dev/null").split() if b)
data = json.dumps(out)
subprocess.run(["pct", "exec", CT, "--", "sh", "-c",
                "cat > /var/lib/agent-room/.host.json && chown agentroom: /var/lib/agent-room/.host.json && "
                "mv /var/lib/agent-room/.host.json /var/lib/agent-room/host.json"], input=data, text=True, timeout=30)
