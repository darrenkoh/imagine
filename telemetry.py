"""Host and hardware telemetry for Imagine status polling."""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

_cpu_prev: tuple[float, float] | None = None  # (idle, total)


def _cpu_percent() -> Optional[float]:
    """Non-blocking CPU % from /proc/stat deltas (Linux) or loadavg (macOS)."""
    global _cpu_prev
    try:
        proc_stat = Path("/proc/stat")
        if proc_stat.exists():
            line = proc_stat.read_text().splitlines()[0]
            parts = line.split()
            # cpu user nice system idle iowait irq softirq steal ...
            nums = [float(x) for x in parts[1:8]]
            idle = nums[3] + nums[4]  # idle + iowait
            total = sum(nums)
            if _cpu_prev is None:
                _cpu_prev = (idle, total)
                return None
            idle0, total0 = _cpu_prev
            _cpu_prev = (idle, total)
            dt = total - total0
            if dt <= 0:
                return None
            return round(max(0.0, min(100.0, 100.0 * (1.0 - (idle - idle0) / dt))), 1)
        if os.uname().sysname == "Darwin":
            cpus = os.cpu_count() or 1
            load1 = os.getloadavg()[0]
            return round(max(0.0, min(100.0, (load1 / cpus) * 100.0)), 1)
        return None
    except Exception:
        return None


_gpu_cache: tuple[float, tuple[Optional[float], Optional[float]]] = (0.0, (None, None))


def _gpu_stats() -> tuple[Optional[float], Optional[float]]:
    """Return (util %, temp °C) from one nvidia-smi call with 0.8s TTL cache."""
    global _gpu_cache
    now = time.monotonic()
    if now - _gpu_cache[0] < 0.8:
        return _gpu_cache[1]
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            _gpu_cache = (now, (None, None))
            return None, None
        line = (r.stdout or "").strip().splitlines()[0].strip()
        parts = [p.strip() for p in line.split(",")]
        util = temp = None
        if parts and parts[0] and "N/A" not in parts[0].upper():
            util = round(float(parts[0]), 1)
        if len(parts) > 1 and parts[1] and "N/A" not in parts[1].upper():
            temp = round(float(parts[1]), 1)
        res = (util, temp)
        _gpu_cache = (now, res)
        return res
    except Exception:
        _gpu_cache = (now, (None, None))
        return None, None


def _cpu_temp_c() -> Optional[float]:
    """Best-effort CPU/package temp °C from thermal zones."""
    try:
        temps: list[float] = []
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                raw = (zone / "temp").read_text().strip()
                t = float(raw) / 1000.0  # millidegree C
                if 0 < t < 150:
                    temps.append(t)
            except Exception:
                continue
        if not temps:
            return None
        return round(max(temps), 1)
    except Exception:
        return None


def _ram_gb() -> Optional[float]:
    """Physical RAM in GiB. Read once, without forking, so the event loop never blocks on sysctl."""
    try:
        if os.uname().sysname == "Darwin":
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            size = ctypes.c_uint64()
            length = ctypes.c_size_t(ctypes.sizeof(size))
            if libc.sysctlbyname(b"hw.memsize", ctypes.byref(size), ctypes.byref(length), None, 0) != 0:
                return None
            return size.value / (1024 ** 3)
        info = Path("/proc/meminfo").read_text()
        for line in info.splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except Exception:
        return None
    return None


_RAM_GB = _ram_gb()


def _meminfo() -> dict:
    out: dict[str, Any] = {"mem_total_gb": None, "mem_available_gb": None, "mem_free_gb": None}
    try:
        proc_mem = Path("/proc/meminfo")
        if proc_mem.exists():
            info: dict[str, int] = {}
            for line in proc_mem.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    info[parts[0][:-1]] = int(parts[1])  # kB
            out["mem_total_gb"] = round(info.get("MemTotal", 0) / 1024 / 1024, 1)
            out["mem_available_gb"] = round(info.get("MemAvailable", 0) / 1024 / 1024, 1)
            out["mem_free_gb"] = round(info.get("MemFree", 0) / 1024 / 1024, 1)
            return out
        if os.uname().sysname == "Darwin":
            if _RAM_GB is not None:
                out["mem_total_gb"] = round(_RAM_GB, 1)
            r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                lines = r.stdout.splitlines()
                page_size = 4096
                m = re.search(r"page size of (\d+) bytes", lines[0] if lines else "")
                if m:
                    page_size = int(m.group(1))
                stats: dict[str, int] = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        val = v.strip().rstrip(".")
                        if val.isdigit():
                            stats[k.strip()] = int(val)
                free_pages = stats.get("Pages free", 0)
                inactive_pages = stats.get("Pages inactive", 0)
                purgeable_pages = stats.get("Pages purgeable", 0)
                speculative_pages = stats.get("Pages speculative", 0)
                free_bytes = free_pages * page_size
                avail_bytes = (free_pages + inactive_pages + purgeable_pages + speculative_pages) * page_size
                out["mem_free_gb"] = round(free_bytes / (1024 ** 3), 1)
                out["mem_available_gb"] = round(avail_bytes / (1024 ** 3), 1)
    except Exception as e:
        out["error"] = str(e)
    return out


# (monotonic time, rx bytes, tx bytes) from the previous status sample.
_net_prev: tuple[float, int, int] | None = None


def _physical_iface(name: str) -> bool:
    """Real NICs only, so docker veth and loopback are not counted twice."""
    n = name.strip().rstrip("*")
    return n.startswith(("en", "eth", "wl", "wlan", "bond", "ib"))


def _parse_proc_net_dev(text: str) -> list[tuple[str, int, int]]:
    """(iface, rx bytes, tx bytes) from /proc/net/dev."""
    rows: list[tuple[str, int, int]] = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            rows.append((name.strip(), int(cols[0]), int(cols[8])))
        except ValueError:
            continue
    return rows


def _parse_netstat_ib(text: str) -> list[tuple[str, int, int]]:
    """Link-layer rows from `netstat -ibn` (macOS). Ibytes/Obytes counted from the right."""
    rows: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "<Link" not in line:
            continue
        cols = line.split()
        if len(cols) < 8:
            continue
        name = cols[0].rstrip("*")
        if name in seen:
            continue
        try:
            rx = int(cols[-5])
            tx = int(cols[-2])
        except ValueError:
            continue
        seen.add(name)
        rows.append((name, rx, tx))
    return rows


def _select_net_rows(rows: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    phys = [row for row in rows if _physical_iface(row[0])]
    if phys:
        return phys
    return [row for row in rows if row[0] not in ("lo", "lo0")]


def _read_net_rows() -> list[tuple[str, int, int]] | None:
    proc = Path("/proc/net/dev")
    try:
        if proc.exists():
            return _parse_proc_net_dev(proc.read_text())
    except Exception:
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ibn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        return _parse_netstat_ib(result.stdout or "")
    except Exception:
        return None


def _net_bandwidth() -> dict:
    """Receive/transmit bytes per second since the previous /api/status sample.

    The first call returns null rates; the footer fills in on the next poll.
    """
    empty: dict[str, Any] = {"rx_bps": None, "tx_bps": None, "ifaces": []}
    global _net_prev
    try:
        rows = _read_net_rows()
        if not rows:
            return empty
        chosen = _select_net_rows(rows)
        if not chosen:
            return empty
        rx = sum(r for _, r, _ in chosen)
        tx = sum(t for _, _, t in chosen)
        active = [name for name, r, t in chosen if r + t > 0]
        ifaces = active or [name for name, _, _ in chosen]
        now = time.monotonic()
        prev = _net_prev
        _net_prev = (now, rx, tx)
        if prev is None:
            return {"rx_bps": None, "tx_bps": None, "ifaces": ifaces}
        t0, rx0, tx0 = prev
        dt = now - t0
        if dt <= 0:
            return {"rx_bps": None, "tx_bps": None, "ifaces": ifaces}
        drx = rx - rx0 if rx >= rx0 else rx
        dtx = tx - tx0 if tx >= tx0 else tx
        return {
            "rx_bps": round(drx / dt, 1),
            "tx_bps": round(dtx / dt, 1),
            "ifaces": ifaces,
        }
    except Exception:
        return empty


_docker_cache: dict[str, tuple[float, Optional[bool]]] = {}


def _docker_running(name: str) -> Optional[bool]:
    global _docker_cache
    now = time.monotonic()
    if name in _docker_cache and (now - _docker_cache[name][0] < 2.0):
        return _docker_cache[name][1]
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode != 0:
            res = False
        else:
            res = r.stdout.strip().lower() == "true"
        _docker_cache[name] = (now, res)
        return res
    except Exception:
        _docker_cache[name] = (now, None)
        return None
