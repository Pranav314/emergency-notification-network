# subscriber.py
# Emergency Notification Subscriber (UDP client)
#
#  - registers itself with the server
#  - receives alerts, prints them, and records the one-way latency
#  - sends an ACK back for alerts that need delivery confirmation
#  - ignores duplicate copies caused by retransmission
#
# Usage:
#   python3 subscriber.py --server 10.0.0.1 --name h2
#   python3 subscriber.py --server 127.0.0.1 --name s1 --port 6001   (local test)
#   --drop 0.3  -> pretend 30% of alerts are lost (to show retransmission)

import argparse
import csv
import os
import random
import socket
import time

from protocol import SERVER_PORT, ALERT_PORT, BUF_SIZE, now_ms, encode, decode


def register(sock, server, name):
    sock.settimeout(1.0)
    while True:
        sock.sendto(encode({"type": "REGISTER", "name": name}), server)
        try:
            data, _ = sock.recvfrom(BUF_SIZE)
        except socket.timeout:
            print("server not answering, retrying registration...")
            continue
        msg = decode(data)
        if msg and msg.get("type") == "REG_OK":
            print("registered with server as", name)
            sock.settimeout(None)
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="10.0.0.1")
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--port", type=int, default=ALERT_PORT)
    ap.add_argument("--drop", type=float, default=0.0, help="simulated loss probability")
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    log_file = args.log or "logs/sub_%s.csv" % args.name
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    f = open(log_file, "w", newline="")
    log = csv.writer(f)
    log.writerow(["seq", "level", "sent_ms", "recv_ms", "latency_ms", "duplicate"])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    server = (args.server, SERVER_PORT)
    register(sock, server, args.name)

    seen = set()
    try:
        while True:
            data, _ = sock.recvfrom(BUF_SIZE)
            recv = now_ms()
            msg = decode(data)
            if not msg or msg.get("type") != "ALERT":
                continue

            if random.random() < args.drop:
                continue          # act as if this packet never arrived

            seq = msg["seq"]
            dup = seq in seen
            latency = recv - msg["sent_ms"]
            log.writerow([seq, msg["level"], "%.3f" % msg["sent_ms"],
                          "%.3f" % recv, "%.3f" % latency, int(dup)])
            f.flush()

            if msg.get("need_ack"):
                sock.sendto(encode({"type": "ACK", "seq": seq, "name": args.name}), server)

            if not dup:
                seen.add(seq)
                stamp = time.strftime("%H:%M:%S")
                print("[%s] #%d %-8s %s  (%.2f ms)" % (stamp, seq, msg["level"], msg["text"], latency))
    except KeyboardInterrupt:
        pass
    finally:
        sock.sendto(encode({"type": "UNREGISTER", "name": args.name}), server)
        sock.close()
        f.close()
        print("subscriber stopped, log saved to", log_file)


if __name__ == "__main__":
    main()
