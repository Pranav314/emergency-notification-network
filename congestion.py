# congestion.py
# Background traffic generator used to create congestion scenarios.
# It floods normal (DSCP 0) UDP packets at a fixed rate so the bottleneck link fills up.
#
#   sender:  python3 congestion.py --dst 10.0.0.7 --rate 15 --time 30
#   sink:    python3 congestion.py --sink
#
# Scenarios used in the report:
#   none   : no background traffic
#   medium : 8 Mbps   (link is 10 Mbps, close to full)
#   heavy  : 15 Mbps  (more than the link can carry -> queues build up)

import argparse
import socket
import time

FLOOD_PORT = 9999
PAYLOAD = 1400            # bytes per packet


def sink(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    s.settimeout(1.0)
    total, start = 0, None
    print("sink listening on UDP", port)
    try:
        while True:
            try:
                data, _ = s.recvfrom(65535)
            except socket.timeout:
                continue
            if start is None:
                start = time.time()
            total += len(data)
    except KeyboardInterrupt:
        pass
    if start:
        secs = time.time() - start
        print("received %.2f MB in %.1f s  (%.2f Mbps)" % (total / 1e6, secs, total * 8 / secs / 1e6))


def flood(dst, port, rate_mbps, duration):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data = b"x" * PAYLOAD
    pkts_per_sec = rate_mbps * 1e6 / 8 / PAYLOAD
    gap = 1.0 / pkts_per_sec
    sent = 0
    start = time.time()
    next_send = start
    print("flooding %s at %.1f Mbps for %d s" % (dst, rate_mbps, duration))
    while time.time() - start < duration:
        s.sendto(data, (dst, port))
        sent += 1
        next_send += gap
        delay = next_send - time.time()
        if delay > 0:
            time.sleep(delay)
    secs = time.time() - start
    print("sent %d packets (%.2f Mbps average)" % (sent, sent * PAYLOAD * 8 / secs / 1e6))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sink", action="store_true")
    ap.add_argument("--dst", default="10.0.0.7")
    ap.add_argument("--port", type=int, default=FLOOD_PORT)
    ap.add_argument("--rate", type=float, default=15.0, help="Mbps")
    ap.add_argument("--time", type=int, default=30, help="seconds")
    args = ap.parse_args()

    if args.sink:
        sink(args.port)
    else:
        flood(args.dst, args.port, args.rate, args.time)
