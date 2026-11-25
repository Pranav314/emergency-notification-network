# sdn_controller.py
# Ryu SDN controller (OpenFlow 1.3) for the Emergency Notification Network.
#
# What it does:
#   1. Works as a learning switch (learns which MAC is on which port).
#   2. Classifies every new flow:
#        EMERGENCY : UDP packets with DSCP 46 (CRITICAL/HIGH alerts from the server)
#                    or UDP packets to port 5000 (registrations and ACKs to the server)
#        NORMAL    : everything else (INFO alerts, background / flood traffic)
#   3. Installs flow rules:
#        EMERGENCY -> priority 200 rule, sent to queue 1 (high-priority queue)
#        NORMAL    -> priority 10 rule,  sent to queue 0 (normal queue)
#   4. Every 5 s prints how many emergency vs normal packets the switches forwarded.
#
# Run:   ryu-manager sdn_controller.py
# To compare WITHOUT prioritisation:   ENN_PRIORITY=0 ryu-manager sdn_controller.py

import os

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, udp
from ryu.ofproto import ofproto_v1_3, inet

SERVER_PORT = 5000
DSCP_EMERGENCY = 46

EMERGENCY_QUEUE = 1
NORMAL_QUEUE = 0

PRIO_EMERGENCY = 200
PRIO_CONTROL = 150
PRIO_NORMAL = 10

COOKIE_EMERGENCY = 0x1
COOKIE_NORMAL = 0x2


class EmergencyController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(EmergencyController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = {}
        self.installed = set()
        self.priority_on = os.environ.get("ENN_PRIORITY", "1") != "0"
        self.logger.info("Emergency prioritisation is %s", "ON" if self.priority_on else "OFF")
        self.monitor_thread = hub.spawn(self._monitor)

    # ---------- switch connects: install table-miss rule ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)
        self.logger.info("switch %016x connected", dp.id)

    def add_flow(self, dp, priority, match, actions, cookie=0, idle=0, buffer_id=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        kwargs = dict(datapath=dp, priority=priority, match=match, instructions=inst,
                      cookie=cookie, idle_timeout=idle)
        if buffer_id is not None and buffer_id != ofp.OFP_NO_BUFFER:
            kwargs["buffer_id"] = buffer_id
        dp.send_msg(parser.OFPFlowMod(**kwargs))

    # ---------- classification ----------
    def classify(self, pkt):
        """Return 'EMERGENCY', 'CONTROL' or 'NORMAL' for a packet."""
        ip = pkt.get_protocol(ipv4.ipv4)
        u = pkt.get_protocol(udp.udp)
        if ip is None or u is None:
            return "NORMAL"
        dscp = ip.tos >> 2
        if dscp == DSCP_EMERGENCY:
            return "EMERGENCY"
        if u.dst_port == SERVER_PORT:
            return "CONTROL"          # REGISTER / ACK going to the server
        return "NORMAL"

    # ---------- new packet arrives at controller ----------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst, src = eth.dst, eth.src
        table = self.mac_to_port.setdefault(dp.id, {})
        table[src] = in_port
        out_port = table.get(dst, ofp.OFPP_FLOOD)

        kind = self.classify(pkt)
        if not self.priority_on:
            kind = "NORMAL"
        queue = EMERGENCY_QUEUE if kind in ("EMERGENCY", "CONTROL") else NORMAL_QUEUE
        actions = [parser.OFPActionSetQueue(queue), parser.OFPActionOutput(out_port)]

        if kind != "NORMAL":
            self.logger.info("[s%d] %s packet %s -> %s", dp.id, kind, src, dst)

        if out_port != ofp.OFPP_FLOOD:
            self.install_rules(dp, dst, out_port)

        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        dp.send_msg(out)

    def install_rules(self, dp, dst, out_port):
        """Once we know where a host is, install 3 rules for it in one go.
        The switch itself then classifies every later packet without asking us:
          prio 200: UDP + DSCP 46   -> high-priority queue
          prio 150: UDP to port 5000 (ACK/REGISTER) -> high-priority queue
          prio 10 : anything else   -> normal queue
        """
        key = (dp.id, dst, out_port)
        if key in self.installed:
            return
        self.installed.add(key)

        parser = dp.ofproto_parser
        high = EMERGENCY_QUEUE if self.priority_on else NORMAL_QUEUE

        m_emerg = parser.OFPMatch(eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP,
                                  ip_proto=inet.IPPROTO_UDP, ip_dscp=DSCP_EMERGENCY)
        m_ctrl = parser.OFPMatch(eth_dst=dst, eth_type=ether_types.ETH_TYPE_IP,
                                 ip_proto=inet.IPPROTO_UDP, udp_dst=SERVER_PORT)
        m_norm = parser.OFPMatch(eth_dst=dst)

        def acts(q):
            return [parser.OFPActionSetQueue(q), parser.OFPActionOutput(out_port)]

        self.add_flow(dp, PRIO_EMERGENCY, m_emerg, acts(high), COOKIE_EMERGENCY)
        self.add_flow(dp, PRIO_CONTROL, m_ctrl, acts(high), COOKIE_EMERGENCY)
        self.add_flow(dp, PRIO_NORMAL, m_norm, acts(NORMAL_QUEUE), COOKIE_NORMAL)
        self.logger.info("[s%d] rules for %s on port %d (emergency queue %d, normal queue %d)",
                         dp.id, dst, out_port, high, NORMAL_QUEUE)

    # ---------- statistics ----------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    def _monitor(self):
        while True:
            for dp in list(self.datapaths.values()):
                dp.send_msg(dp.ofproto_parser.OFPFlowStatsRequest(dp))
            hub.sleep(5)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply_handler(self, ev):
        emerg_pkts = emerg_bytes = norm_pkts = norm_bytes = 0
        for stat in ev.msg.body:
            if stat.cookie == COOKIE_EMERGENCY:
                emerg_pkts += stat.packet_count
                emerg_bytes += stat.byte_count
            elif stat.cookie == COOKIE_NORMAL:
                norm_pkts += stat.packet_count
                norm_bytes += stat.byte_count
        self.logger.info("[s%d stats] emergency: %d pkts / %d bytes | normal: %d pkts / %d bytes",
                         ev.msg.datapath.id, emerg_pkts, emerg_bytes, norm_pkts, norm_bytes)
