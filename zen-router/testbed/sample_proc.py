#!/usr/bin/env python3
"""Sample one process: CPU%, RSS, threads, fds. Leak check over a long run.

Usage: sample_proc.py <pid> <seconds> [interval]
Prints one CSV line per sample and a summary at the end.
"""
import os
import sys
import time


def read(pid: int):
    with open(f"/proc/{pid}/stat", "r") as f:
        parts = f.read().split()
    utime = int(parts[13]) / os.sysconf("SC_CLK_TCK")
    stime = int(parts[14]) / os.sysconf("SC_CLK_TCK")
    threads = int(parts[19])
    rss = 0
    with open(f"/proc/{pid}/status", "r") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])  # kB
                break
    try:
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        fds = -1
    return utime + stime, rss, threads, fds


def main() -> int:
    pid = int(sys.argv[1])
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    cpu0, rss0, thr0, fd0 = read(pid)
    t0 = time.time()
    print("t,cpu_pct,rss_mb,threads,fds", flush=True)
    peak_rss = rss0
    peak_fd = fd0
    try:
        while time.time() - t0 < secs:
            time.sleep(interval)
            cpu1, rss1, thr1, fd1 = read(pid)
            dt = interval
            cpu = (cpu1 - cpu0) / dt * 100.0
            print(f"{time.time() - t0:.1f},{cpu:.1f},{rss1 / 1024:.1f},{thr1},{fd1}", flush=True)
            peak_rss = max(peak_rss, rss1)
            peak_fd = max(peak_fd, fd1)
            cpu0, rss0 = cpu1, rss1
    except KeyboardInterrupt:
        pass
    cpu1, rss1, thr1, fd1 = read(pid)
    print(
        f"SUMMARY elapsed={time.time() - t0:.1f}s rss_start={rss0 and peak_rss and rss0}kB "
        f"rss_end={rss1}kB peak_rss={peak_rss}kB fds_start={fd0} fds_end={fd1} peak_fds={peak_fd}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
