#!/usr/bin/env python3
"""System Inspector - data collectors and command-line interface.

Usage (after installing):
  system-inspector-cli                    show everything
  system-inspector-cli -s storage users   show only some sections
  system-inspector-cli --json > out.json  machine-readable output
  system-inspector-cli --list             list available sections

Uses only the Python standard library. Run with sudo for extra details
(full hardware info, disk SMART health, other users' processes).
"""
import argparse
import glob
import json
import os
import platform
import pwd
import grp
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


# ---------- helpers ----------
def read(path, default=""):
    try:
        return Path(path).read_text(errors="replace").strip()
    except Exception:
        return default


def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def human(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def duration(seconds):
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}d {h}h {m}m"


# ---------- sections ----------
def sec_system():
    osr = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip('"')
    up = float(read("/proc/uptime", "0 0").split()[0])
    return {
        "Hostname": socket.gethostname(),
        "OS": osr.get("PRETTY_NAME", platform.system()),
        "OS ID / Version": f"{osr.get('ID', '?')} {osr.get('VERSION_ID', '')}".strip(),
        "Kernel": platform.release(),
        "Kernel build": platform.version(),
        "Architecture": platform.machine(),
        "Uptime": duration(up),
        "Boot time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - up)),
        "Init system": read("/proc/1/comm", "?"),
        "Desktop": os.environ.get("XDG_CURRENT_DESKTOP", "none/unknown"),
        "Session type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "Shell": os.environ.get("SHELL", "?"),
        "Timezone": read("/etc/timezone") or time.tzname[0],
        "Locale": os.environ.get("LANG", "?"),
        "Virtualization": run(["systemd-detect-virt"]) or "unknown",
        "Python": platform.python_version(),
    }


def sec_cpu():
    models, phys, mhz, cache, flags = [], set(), [], "", ""
    cur_phys = None
    for line in read("/proc/cpuinfo").splitlines():
        if ":" not in line:
            continue
        k, v = [x.strip() for x in line.split(":", 1)]
        if k == "model name":
            models.append(v)
        elif k == "physical id":
            cur_phys = v
        elif k == "core id":
            phys.add((cur_phys, v))
        elif k == "cpu MHz":
            mhz.append(float(v))
        elif k == "cache size":
            cache = v
        elif k == "flags" and not flags:
            flags = v
    la = os.getloadavg()
    info = {
        "Model": models[0] if models else platform.processor() or "unknown",
        "Logical CPUs": len(models) or os.cpu_count(),
        "Physical cores": len(phys) or "unknown",
        "Current MHz (avg)": f"{sum(mhz) / len(mhz):.0f}" if mhz else "unknown",
        "Cache": cache or "unknown",
        "Load average (1/5/15m)": f"{la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f}",
        "Virtualization flag": "yes" if (" vmx" in f" {flags}" or " svm" in f" {flags}") else "no",
    }
    for name in ("min", "max"):
        v = read(f"/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_{name}_freq")
        if v:
            info[f"{name.capitalize()} MHz"] = int(v) // 1000
    gov = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov:
        info["Governor"] = gov
    return info


def sec_memory():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    st, sf = m.get("SwapTotal", 0), m.get("SwapFree", 0)
    return {
        "RAM total": human(total),
        "RAM used": f"{human(total - avail)} ({(total - avail) * 100 // max(total, 1)}%)",
        "RAM available": human(avail),
        "Buffers + cached": human(m.get("Buffers", 0) + m.get("Cached", 0)),
        "Swap total": human(st),
        "Swap used": human(st - sf),
    }


def sec_storage():
    out = {"Block devices": [], "Mounted filesystems": []}
    raw = run(["lsblk", "-J", "-b", "-o",
               "NAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,ROTA,RO"])
    try:
        def walk(devs, depth=0):
            for d in devs:
                kind = d.get("type")
                if kind == "loop" and not d.get("mountpoint"):
                    continue
                rota = d.get("rota")
                media = ""
                if kind == "disk":
                    media = "HDD" if rota in (True, "1", 1) else "SSD/NVMe"
                out["Block devices"].append({
                    "name": ("  " * depth) + d["name"],
                    "type": kind,
                    "size": human(d.get("size") or 0),
                    "fs": d.get("fstype") or "",
                    "label": d.get("label") or "",
                    "uuid": d.get("uuid") or "",
                    "mount": d.get("mountpoint") or "",
                    "model": (d.get("model") or "").strip(),
                    "media": media,
                })
                walk(d.get("children", []), depth + 1)
        walk(json.loads(raw)["blockdevices"])
    except Exception:
        # fallback when lsblk is missing
        for line in read("/proc/partitions").splitlines()[2:]:
            p = line.split()
            if len(p) == 4:
                out["Block devices"].append({"name": p[3], "size": human(int(p[2]) * 1024)})

    if shutil.which("smartctl"):
        health = []
        for d in out["Block devices"]:
            if d.get("type") != "disk" or d["name"].startswith(("zram", "loop", "ram")):
                continue
            if os.geteuid() != 0:
                break
            try:
                j = json.loads(run(["smartctl", "-H", "-A", "-j", "/dev/" + d["name"]], 15) or "{}")
                ok = j.get("smart_status", {}).get("passed")
                if ok is None:
                    continue
                temp = j.get("temperature", {}).get("current")
                hours = j.get("power_on_time", {}).get("hours")
                health.append({"disk": d["name"], "health": "PASSED" if ok else "FAILING",
                               "temp": f"{temp} C" if temp is not None else "",
                               "power-on": f"{hours} h" if hours is not None else ""})
            except Exception:
                continue
        if health:
            out["Disk health (SMART)"] = health
        elif os.geteuid() != 0:
            out["Disk health (SMART)"] = ["Run with sudo to read SMART health data"]

    seen = set()
    for line in read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        if fs in ("proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "securityfs",
                  "pstore", "bpf", "tracefs", "debugfs", "mqueue", "hugetlbfs", "configfs",
                  "fusectl", "autofs", "binfmt_misc", "overlay", "squashfs", "nsfs"):
            continue
        if mnt in seen or mnt.startswith(("/sys", "/proc", "/dev/", "/run/user")):
            continue
        seen.add(mnt)
        try:
            s = os.statvfs(mnt)
        except OSError:
            continue
        total = s.f_blocks * s.f_frsize
        if total == 0:
            continue
        free = s.f_bavail * s.f_frsize
        used = total - s.f_bfree * s.f_frsize
        out["Mounted filesystems"].append({
            "device": dev, "mount": mnt, "fs": fs, "size": human(total),
            "used": human(used), "free": human(free),
            "use%": f"{used * 100 // max(used + free, 1)}%",
        })
    return out


def sec_network():
    out = {"Interfaces": []}
    addrs = {}
    raw = run(["ip", "-j", "addr"])
    try:
        for i in json.loads(raw):
            addrs[i["ifname"]] = [f"{a['local']}/{a['prefixlen']}" for a in i.get("addr_info", [])]
    except Exception:
        pass
    for path in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(path)
        out["Interfaces"].append({
            "name": n,
            "state": read(f"{path}/operstate"),
            "mac": read(f"{path}/address"),
            "mtu": read(f"{path}/mtu"),
            "speed": (read(f"{path}/speed") + " Mbps") if read(f"{path}/speed").lstrip("-").isdigit() and int(read(f"{path}/speed")) > 0 else "",
            "addresses": ", ".join(addrs.get(n, [])),
        })
    out["Default route"] = run(["ip", "route", "show", "default"]) or "none"
    out["DNS servers"] = ", ".join(
        l.split()[1] for l in read("/etc/resolv.conf").splitlines() if l.startswith("nameserver")
    ) or "unknown"
    listening = run(["ss", "-tulnH"])
    out["Listening sockets"] = len(listening.splitlines()) if listening else "unknown"
    return out


def sec_users():
    out = {"Human users": [], "System accounts": 0, "Groups": len(grp.getall()) if hasattr(grp, "getall") else len(grp.getgrall())}
    sudoers = set()
    for g in ("sudo", "wheel", "admin"):
        try:
            sudoers.update(grp.getgrnam(g).gr_mem)
        except KeyError:
            pass
    for u in pwd.getpwall():
        human_user = (u.pw_uid == 0 or u.pw_uid >= 1000) and u.pw_uid != 65534
        if human_user:
            groups = [g.gr_name for g in grp.getgrall() if u.pw_name in g.gr_mem]
            out["Human users"].append({
                "name": u.pw_name, "uid": u.pw_uid, "gid": u.pw_gid,
                "home": u.pw_dir, "shell": u.pw_shell,
                "admin": "yes" if (u.pw_uid == 0 or u.pw_name in sudoers) else "no",
                "groups": ", ".join(groups),
            })
        else:
            out["System accounts"] += 1
    out["Currently logged in"] = run(["who"]).splitlines() or ["nobody"]
    out["Recent logins"] = run(["last", "-n", "5", "-w"]).splitlines()[:5]
    out["Running as"] = f"{pwd.getpwuid(os.getuid()).pw_name} (uid {os.getuid()})"
    return out


def sec_hardware():
    out = {}
    dmi = "/sys/class/dmi/id"
    for label, f in (("Vendor", "sys_vendor"), ("Product", "product_name"),
                     ("Product version", "product_version"), ("Board", "board_name"),
                     ("BIOS vendor", "bios_vendor"), ("BIOS version", "bios_version"),
                     ("BIOS date", "bios_date"), ("Chassis type", "chassis_type")):
        v = read(f"{dmi}/{f}")
        if v:
            out[label] = v
    gpu = [l for l in run(["lspci"]).splitlines() if any(k in l for k in ("VGA", "3D", "Display"))]
    if gpu:
        out["GPU"] = gpu
    usb = run(["lsusb"]).splitlines()
    if usb:
        out["USB devices"] = usb
    bats = []
    for b in glob.glob("/sys/class/power_supply/BAT*"):
        bats.append(f"{os.path.basename(b)}: {read(b + '/capacity')}% ({read(b + '/status')})")
    if bats:
        out["Battery"] = bats
    temps = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = read(z + "/temp")
        if t.lstrip("-").isdigit():
            temps.append(f"{read(z + '/type')}: {int(t) / 1000:.1f} C")
    if temps:
        out["Temperatures"] = temps
    return out


def sec_processes():
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    top_mem = run(["ps", "-eo", "pid,user,%cpu,%mem,comm", "--sort=-%mem"]).splitlines()[:6]
    top_cpu = run(["ps", "-eo", "pid,user,%cpu,%mem,comm", "--sort=-%cpu"]).splitlines()[:6]
    return {"Total processes": len(pids), "Top by memory": top_mem, "Top by CPU": top_cpu}


def sec_software():
    out = {}
    counts = (("dpkg", ["dpkg-query", "-f", ".\n", "-W"]), ("rpm", ["rpm", "-qa"]),
              ("pacman", ["pacman", "-Q"]), ("apk", ["apk", "info"]))
    for name, cmd in counts:
        if shutil.which(cmd[0]):
            out["Package manager"] = name
            out["Installed packages"] = len(run(cmd, 30).splitlines())
            break
    for tool, args in (("GCC", ["gcc", "--version"]), ("Git", ["git", "--version"]),
                       ("Docker", ["docker", "--version"]), ("Node", ["node", "--version"]),
                       ("systemd", ["systemctl", "--version"])):
        if shutil.which(args[0]):
            first = run(args).splitlines()
            if first:
                out[tool] = first[0]
    if shutil.which("systemctl"):
        failed = run(["systemctl", "--failed", "--no-legend", "--plain"]).splitlines()
        out["Failed services"] = failed or ["none"]
        out["Running services"] = len(run(["systemctl", "list-units", "--type=service",
                                           "--state=running", "--no-legend", "--plain"]).splitlines())
    return out


SECTIONS = {
    "system": sec_system, "cpu": sec_cpu, "memory": sec_memory, "storage": sec_storage,
    "network": sec_network, "users": sec_users, "hardware": sec_hardware,
    "processes": sec_processes, "software": sec_software,
}


# ---------- output ----------
def render(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        w = max((len(str(k)) for k in value), default=0)
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}:")
                render(v, indent + 1)
            else:
                print(f"{pad}{str(k).ljust(w)} : {v}")
    elif isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value):
            keys = list(value[0].keys())
            for x in value:
                for k in x:
                    if k not in keys:
                        keys.append(k)
            widths = {k: max(len(k), *(len(str(x.get(k, ""))) for x in value)) for k in keys}
            print(pad + "  ".join(k.upper().ljust(widths[k]) for k in keys))
            for x in value:
                print(pad + "  ".join(str(x.get(k, "")).ljust(widths[k]) for k in keys).rstrip())
        else:
            for x in value:
                print(f"{pad}{x}")


def _main():
    ap = argparse.ArgumentParser(description="Show detailed Linux system information.")
    ap.add_argument("-s", "--section", nargs="+", choices=SECTIONS, help="sections to show")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--list", action="store_true", help="list sections")
    a = ap.parse_args()

    if a.list:
        print("\n".join(SECTIONS))
        return
    if platform.system() != "Linux":
        print("This tool is built for Linux.")
        return

    names = a.section or list(SECTIONS)
    data = {n: SECTIONS[n]() for n in names}
    if a.json:
        print(json.dumps(data, indent=2))
        return
    for n, d in data.items():
        print(f"\n{'=' * 8} {n.upper()} {'=' * (60 - len(n))}")
        render(d)


def main():
    try:
        _main()
    except BrokenPipeError:  # e.g. piped into `head`
        sys.stderr.close()


if __name__ == "__main__":
    main()
