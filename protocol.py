# protocol.py
# Common settings and message format shared by server and subscribers.
# Every message is a small JSON object sent inside one UDP datagram.

import json
import time

SERVER_PORT = 5000        # server listens here for REGISTER / ACK / UNREGISTER
ALERT_PORT = 5005         # subscribers listen here for alerts (SDN matches this port)

# DSCP values written into the IP header so the switch can classify traffic.
# TOS byte = DSCP << 2
DSCP_EMERGENCY = 46       # "Expedited Forwarding" -> TOS 184
DSCP_NORMAL = 0
TOS_EMERGENCY = DSCP_EMERGENCY << 2

# Alert levels. CRITICAL and HIGH need a delivery confirmation (ACK).
LEVELS = {
    "CRITICAL": {"need_ack": True,  "emergency": True},
    "HIGH":     {"need_ack": True,  "emergency": True},
    "INFO":     {"need_ack": False, "emergency": False},
}

ACK_TIMEOUT = 0.5         # seconds to wait before resending an un-ACKed alert
MAX_RETRIES = 3           # resend at most this many times
BUF_SIZE = 4096


def now_ms():
    return time.time() * 1000.0


def encode(msg):
    return json.dumps(msg).encode()


def decode(data):
    try:
        return json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
        return None
