# analyze.py
# Reads the logs of every run in logs/ and measures notification latency.
#
# For each run folder (e.g. logs/prio_heavy) it reports, per alert level:
#   - average, 95th percentile and maximum one-way latency (ms)
#   - delivery ratio (how many ACK-required alerts were confirmed)
#   - number of retransmissions
# Output: printed table + results/summary.csv + results/latency.png (if matplotlib exists)
#
# Usage:  python3 analyze.py            (looks inside ./logs)
#         python3 analyze.py my_logs

import csv
import glob
import os
import sys

LEVELS = ["CRITICAL", "HIGH", "INFO"]


def percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def analyze_run(folder):
    latencies = {lvl: [] for lvl in LEVELS}
    for path in glob.glob(os.path.join(folder, "sub_*.csv")):
        seen = set()
        for row in read_csv(path):
            if row["duplicate"] == "1" or row["seq"] in seen:
                continue
            seen.add(row["seq"])
            latencies[row["level"]].append(float(row["latency_ms"]))

    sent = {lvl: 0 for lvl in LEVELS}
    delivered = {lvl: 0 for lvl in LEVELS}
    retries = {lvl: 0 for lvl in LEVELS}
    server_log = os.path.join(folder, "server_log.csv")
    if os.path.exists(server_log):
        for row in read_csv(server_log):
            lvl = row["level"]
            sent[lvl] += 1
            retries[lvl] += int(row["retries"] or 0)
            if row["status"] == "DELIVERED":
                delivered[lvl] += 1
    # INFO alerts have no ACK, so count what the subscribers actually received
    delivered["INFO"] = len(latencies["INFO"])

    rows = []
    for lvl in LEVELS:
        lat = latencies[lvl]
        rows.append({
            "run": os.path.basename(folder),
            "level": lvl,
            "received": len(lat),
            "avg_ms": round(sum(lat) / len(lat), 3) if lat else 0,
            "p95_ms": round(percentile(lat, 95), 3),
            "max_ms": round(max(lat), 3) if lat else 0,
            "delivery_%": round(100.0 * delivered[lvl] / sent[lvl], 1) if sent[lvl] else 0,
            "retries": retries[lvl],
        })
    return rows


def make_chart(all_rows, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not installed, skipping chart)")
        return

    runs = sorted({r["run"] for r in all_rows})
    width = 0.25
    fig, ax = plt.subplots(figsize=(max(6, len(runs) * 1.6), 4.5))
    for i, lvl in enumerate(LEVELS):
        vals = [next((r["avg_ms"] for r in all_rows if r["run"] == run and r["level"] == lvl), 0)
                for run in runs]
        xs = [x + (i - 1) * width for x in range(len(runs))]
        ax.bar(xs, vals, width, label=lvl)
    ax.set_xticks(range(len(runs)))
    ax.set_xticklabels(runs, rotation=20)
    ax.set_ylabel("Average latency (ms)")
    ax.set_title("Notification latency per scenario")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    print("chart saved to", path)


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "logs"
    folders = sorted(d for d in glob.glob(os.path.join(base, "*")) if os.path.isdir(d))
    if not folders:
        print("no run folders found in", base)
        return

    all_rows = []
    for folder in folders:
        all_rows.extend(analyze_run(folder))

    header = ["run", "level", "received", "avg_ms", "p95_ms", "max_ms", "delivery_%", "retries"]
    print("%-18s %-9s %8s %9s %9s %9s %10s %8s" % tuple(header))
    for r in all_rows:
        print("%-18s %-9s %8d %9.3f %9.3f %9.3f %10.1f %8d" % tuple(r[h] for h in header))

    os.makedirs("results", exist_ok=True)
    with open("results/summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(all_rows)
    print("\nsummary saved to results/summary.csv")
    make_chart(all_rows, "results/latency.png")


if __name__ == "__main__":
    main()
