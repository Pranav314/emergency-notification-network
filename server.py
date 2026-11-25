# server.py
# Emergency Notification Server (UDP)
#
#  - keeps a list of registered subscribers
#  - sends alerts to every subscriber (CRITICAL / HIGH / INFO)
#  - marks emergency alerts with DSCP 46 so the SDN switch can prioritise them
#  - waits for ACKs for CRITICAL/HIGH alerts and resends if an ACK is missing
#  - writes every delivery to logs/server_log.csv
#
# Usage:
#   python3 server.py                      (type alerts by hand)
#   python3 server.py --auto 50 --gap 0.2  (send 50 test alerts automatically)

import argparse
import csv
import os
import random
import socket
import threading
import time

from protocol import (SERVER_PORT, TOS_EMERGENCY, LEVELS, ACK_TIMEOUT,
                      MAX_RETRIES, BUF_SIZE, now_ms, encode, decode)


class NotificationServer:
    def __init__(self, bind_ip="0.0.0.0", log_file="logs/server_log.csv"):
        # control socket: REGISTER, ACK, UNREGISTER arrive here
        self.ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ctrl.bind((bind_ip, SERVER_PORT))

        # two sending sockets: one marked as emergency, one normal
        self.emerg_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.emerg_sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TOS_EMERGENCY)
        self.normal_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.subscribers = {}     # name -> (ip, port)
        self.pending = {}         # (seq, name) -> info about an un-ACKed alert
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True

        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        self.log_f = open(log_file, "w", newline="")
        self.log = csv.writer(self.log_f)
        self.log.writerow(["seq", "subscriber", "level", "sent_ms",
                           "ack_ms", "rtt_ms", "retries", "status"])

    # ---------------- receiving side ----------------
    def listen(self):
        self.ctrl.settimeout(0.5)
        while self.running:
            try:
                data, addr = self.ctrl.recvfrom(BUF_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            msg = decode(data)
            if not msg:
                continue
            kind = msg.get("type")
            if kind == "REGISTER":
                self.handle_register(msg, addr)
            elif kind == "ACK":
                self.handle_ack(msg)
            elif kind == "UNREGISTER":
                with self.lock:
                    self.subscribers.pop(msg.get("name"), None)
                print("[-] %s left" % msg.get("name"))

    def handle_register(self, msg, addr):
        name = msg.get("name", "%s:%d" % addr)
        with self.lock:
            new = name not in self.subscribers
            self.subscribers[name] = addr
            count = len(self.subscribers)
        self.ctrl.sendto(encode({"type": "REG_OK", "name": name}), addr)
        if new:
            print("[+] registered %s at %s:%d  (total %d)" % (name, addr[0], addr[1], count))

    def handle_ack(self, msg):
        key = (msg.get("seq"), msg.get("name"))
        ack_time = now_ms()
        with self.lock:
            item = self.pending.pop(key, None)
        if item:
            rtt = ack_time - item["first_sent"]
            self.log.writerow([key[0], key[1], item["level"], "%.3f" % item["first_sent"],
                               "%.3f" % ack_time, "%.3f" % rtt, item["retries"], "DELIVERED"])
            self.log_f.flush()

    # ---------------- sending side ----------------
    def send_alert(self, level, text):
        level = level.upper()
        if level not in LEVELS:
            print("unknown level, use CRITICAL / HIGH / INFO")
            return
        rule = LEVELS[level]
        with self.lock:
            self.seq += 1
            seq = self.seq
            targets = list(self.subscribers.items())

        if not targets:
            print("no subscribers registered yet")
            return

        sock = self.emerg_sock if rule["emergency"] else self.normal_sock
        for name, addr in targets:
            msg = {"type": "ALERT", "seq": seq, "level": level, "text": text,
                   "need_ack": rule["need_ack"], "sent_ms": now_ms()}
            if rule["need_ack"]:
                # record it BEFORE sending, otherwise a very fast ACK could
                # arrive before we start waiting for it
                with self.lock:
                    self.pending[(seq, name)] = {
                        "msg": msg, "addr": addr, "level": level,
                        "first_sent": msg["sent_ms"], "last_sent": time.time(),
                        "retries": 0}
            sock.sendto(encode(msg), addr)
            if not rule["need_ack"]:
                self.log.writerow([seq, name, level, "%.3f" % msg["sent_ms"],
                                   "", "", 0, "SENT_NO_ACK"])
        self.log_f.flush()
        print("[>] alert #%d (%s) sent to %d subscriber(s)" % (seq, level, len(targets)))

    def retransmit_loop(self):
        # checks every 100 ms for alerts whose ACK has not come back
        while self.running:
            time.sleep(0.1)
            now = time.time()
            resend, failed = [], []
            with self.lock:
                for key, item in list(self.pending.items()):
                    if now - item["last_sent"] < ACK_TIMEOUT:
                        continue
                    if item["retries"] >= MAX_RETRIES:
                        failed.append((key, self.pending.pop(key)))
                    else:
                        item["retries"] += 1
                        item["last_sent"] = now
                        resend.append(item)
            for item in resend:
                self.emerg_sock.sendto(encode(item["msg"]), item["addr"])
            for key, item in failed:
                print("[!] alert #%d to %s FAILED after %d retries" % (key[0], key[1], MAX_RETRIES))
                self.log.writerow([key[0], key[1], item["level"], "%.3f" % item["first_sent"],
                                   "", "", item["retries"], "FAILED"])
            if failed:
                self.log_f.flush()

    def stop(self):
        self.running = False
        time.sleep(0.3)
        self.ctrl.close()
        self.log_f.close()


def manual_mode(server):
    print("Type alerts as:  LEVEL message     e.g.  CRITICAL Fire in Block A")
    print("Commands: list, quit")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "quit":
            break
        if line == "list":
            for name, addr in server.subscribers.items():
                print("   %s -> %s:%d" % (name, addr[0], addr[1]))
            continue
        parts = line.split(" ", 1)
        server.send_alert(parts[0], parts[1] if len(parts) > 1 else "")


def auto_mode(server, count, gap, wait_subs):
    print("waiting for %d subscriber(s)..." % wait_subs)
    while len(server.subscribers) < wait_subs:
        time.sleep(0.2)
    samples = ["Fire alarm in Block A", "Earthquake warning", "Gas leak in lab 3",
               "Flood alert near gate 2", "Power shutdown at 6 PM", "Exam timetable updated"]
    for i in range(count):
        level = random.choice(["CRITICAL", "HIGH", "INFO"])
        server.send_alert(level, "%s (test %d)" % (random.choice(samples), i + 1))
        time.sleep(gap)
    time.sleep(ACK_TIMEOUT * (MAX_RETRIES + 1) + 0.5)   # let last ACKs / retries finish


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", type=int, default=0, help="send N test alerts automatically")
    ap.add_argument("--gap", type=float, default=0.2, help="seconds between auto alerts")
    ap.add_argument("--subs", type=int, default=1, help="auto mode: wait for this many subscribers")
    ap.add_argument("--log", default="logs/server_log.csv")
    args = ap.parse_args()

    srv = NotificationServer(log_file=args.log)
    threading.Thread(target=srv.listen, daemon=True).start()
    threading.Thread(target=srv.retransmit_loop, daemon=True).start()
    print("Emergency Notification Server on UDP port %d" % SERVER_PORT)

    try:
        if args.auto:
            auto_mode(srv, args.auto, args.gap, args.subs)
        else:
            manual_mode(srv)
    finally:
        srv.stop()
        print("server stopped, log saved to", args.log)
