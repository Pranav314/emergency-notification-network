#!/usr/bin/env python3
# topology.py
# Mininet network for the Emergency Notification Network.
#
#        h1 (server 10.0.0.1)                       h2 h3 h4 h5 (subscribers)
#              \                                     |  |  |  /
#               s1 ============ 10 Mbps ============ s2
#              /         (bottleneck link)            \
#        h6 (traffic generator 10.0.0.6)               h7 (traffic sink 10.0.0.7)
#
# Every switch port gets two OVS queues (linux-htb):
#   queue 0 : normal traffic     (min 1 Mbps,  max 10 Mbps)
#   queue 1 : emergency traffic  (min 8 Mbps,  max 10 Mbps, served first)
# The Ryu controller decides which queue each packet goes to.
#
# Usage (start the controller first in another terminal):
#   sudo python3 topology.py                       -> Mininet CLI for a manual demo
#   sudo python3 topology.py --experiment --tag prio
#        -> runs all congestion scenarios automatically and saves logs in logs/

import argparse
import os
import time

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

LINK_RATE = 10_000_000          # 10 Mbps
SCENARIOS = [("none", 0), ("medium", 8), ("heavy", 15)]   # name, flood Mbps
SUBSCRIBERS = ["h2", "h3", "h4", "h5"]


def build():
    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)

    s1 = net.addSwitch("s1", protocols="OpenFlow13")
    s2 = net.addSwitch("s2", protocols="OpenFlow13")

    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h6 = net.addHost("h6", ip="10.0.0.6/24")
    net.addLink(h1, s1)
    net.addLink(h6, s1)

    for i in range(2, 6):
        h = net.addHost("h%d" % i, ip="10.0.0.%d/24" % i)
        net.addLink(h, s2)
    h7 = net.addHost("h7", ip="10.0.0.7/24")
    net.addLink(h7, s2)

    net.addLink(s1, s2)          # bottleneck
    return net


def setup_queues(net):
    """Attach two HTB queues to every switch port using ovs-vsctl."""
    for sw in net.switches:
        for intf in sw.intfList():
            if intf.name == "lo":
                continue
            cmd = ("ovs-vsctl -- set port {p} qos=@q -- "
                   "--id=@q create qos type=linux-htb other-config:max-rate={rate} "
                   "queues:0=@q0 queues:1=@q1 -- "
                   "--id=@q0 create queue other-config:min-rate=1000000 "
                   "other-config:max-rate={rate} other-config:priority=1 -- "
                   "--id=@q1 create queue other-config:min-rate=8000000 "
                   "other-config:max-rate={rate} other-config:priority=0"
                   ).format(p=intf.name, rate=LINK_RATE)
            sw.cmd(cmd)
    info("*** QoS queues configured on all switch ports\n")


def clear_queues():
    os.system("ovs-vsctl --all destroy qos -- --all destroy queue > /dev/null 2>&1")


def run_experiment(net, tag, alerts, gap):
    h1, h6, h7 = net.get("h1"), net.get("h6"), net.get("h7")
    subs = [net.get(n) for n in SUBSCRIBERS]
    here = os.path.dirname(os.path.abspath(__file__))

    info("*** warming up (ARP + flow rules)\n")
    net.pingAll()

    for name, rate in SCENARIOS:
        out = os.path.join(here, "logs", "%s_%s" % (tag, name))
        os.makedirs(out, exist_ok=True)
        info("\n*** scenario '%s' (background %d Mbps) -> %s\n" % (name, rate, out))

        for h in subs:
            h.cmd("cd %s && python3 subscriber.py --server 10.0.0.1 --name %s "
                  "--log %s/sub_%s.csv > %s/sub_%s.out 2>&1 &"
                  % (here, h.name, out, h.name, out, h.name))

        if rate:
            h7.cmd("cd %s && python3 congestion.py --sink > %s/sink.out 2>&1 &" % (here, out))
            duration = int(alerts * gap) + 15
            h6.cmd("cd %s && python3 congestion.py --dst 10.0.0.7 --rate %d --time %d "
                   "> %s/flood.out 2>&1 &" % (here, rate, duration, out))
            time.sleep(3)          # let the queue fill up first

        result = h1.cmd("cd %s && python3 server.py --auto %d --gap %s --subs %d --log %s/server_log.csv"
                        % (here, alerts, gap, len(subs), out))
        with open(os.path.join(out, "server.out"), "w") as f:
            f.write(result)

        # the [s] / [c] trick stops pkill from matching this shell command itself
        os.system("pkill -INT -f '[s]ubscriber.py'; pkill -INT -f '[c]ongestion.py'")
        time.sleep(2)
        info("*** scenario '%s' done\n" % name)

    info("\n*** all scenarios finished. Now run:  python3 analyze.py\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", action="store_true")
    ap.add_argument("--tag", default="prio", help="name for this run, e.g. prio / noprio")
    ap.add_argument("--alerts", type=int, default=60)
    ap.add_argument("--gap", type=float, default=0.2)
    args = ap.parse_args()

    setLogLevel("info")
    clear_queues()
    net = build()
    net.start()
    setup_queues(net)
    try:
        if args.experiment:
            run_experiment(net, args.tag, args.alerts, args.gap)
        else:
            info("*** Try:  h2 python3 subscriber.py --name h2 &   then   h1 python3 server.py\n")
            CLI(net)
    finally:
        net.stop()
        clear_queues()
