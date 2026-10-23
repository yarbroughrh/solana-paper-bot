#!/usr/bin/env python3
"""Suspension probe: every 10 s append wall-clock, monotonic, CLOCK_BOOTTIME, boot_id, steal ticks.
If the VM is suspended, wall/boottime jump while monotonic (which excludes suspend) does not."""
import time, os, sys
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticks.csv")
boot = open("/proc/sys/kernel/random/boot_id").read().strip()[:8]
def steal():
    f = open("/proc/stat").readline().split(); return int(f[8])
pw, pm, pb = time.time(), time.monotonic(), time.clock_gettime(time.CLOCK_BOOTTIME)
while True:
    time.sleep(10)
    w, m, b = time.time(), time.monotonic(), time.clock_gettime(time.CLOCK_BOOTTIME)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')},{boot},{os.getpid()},{w-pw:.1f},{m-pm:.1f},{b-pb:.1f},{steal()}\n"
    try:
        with open(OUT, "a") as f: f.write(line)
    except Exception: pass
    pw, pm, pb = w, m, b
