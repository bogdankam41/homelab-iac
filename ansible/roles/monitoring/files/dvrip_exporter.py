#!/usr/bin/env python3
"""Prometheus exporter for XiongMai-based video recorders (NETSurveillance / XMeye).

These NVRs/DVRs have no SNMP; their own clients talk DVRIP, a JSON-over-TCP
protocol on port 34567. This exporter logs in the same way, keeps the session
and turns the recorder's status replies into metrics: camera connection,
per-channel recording and bitrate, disks, how fresh and how deep the archive is.
One instance serves every recorder: Prometheus passes it as ?target=<address>,
credentials come from the config file keyed by that address.

Managed by Ansible (roles/monitoring) — do not edit on the host.
"""

import argparse
import datetime
import hashlib
import json
import socket
import struct
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 34567
TIMEOUT = 10

# head 0xff, version, 2 reserved, session, sequence, 2 reserved, message id, payload length.
HEADER = struct.Struct("<BB2xII2xHI")
MSG_LOGIN = 1000
MSG_KEEPALIVE = 1006
MSG_INFO = 1020        # SystemInfo, StorageInfo, WorkState
MSG_CONFIG = 1042      # NetWork.ChnStatus
MSG_CHANNEL_TITLE = 1048
MSG_TIME = 1452        # OPTimeQuery: the recorder's clock
RET_OK = 100
RET_NOT_LOGGED_IN = 105

# Record times are the recorder's local wall clock; an unused slot reads all zeros.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MIB = 1024 * 1024


class DvripError(Exception):
    pass


class SessionLost(DvripError):
    pass


def sofia_hash(password):
    """The password digest DVRIP expects: MD5 folded into 8 alphanumerics."""
    digest = hashlib.md5(password.encode()).digest()
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(alphabet[(digest[i] + digest[i + 1]) % 62] for i in range(0, 16, 2))


def parse_time(value):
    if not value or value.startswith("0000"):
        return None
    return datetime.datetime.strptime(value, TIME_FORMAT)


class Recorder:
    """One recorder: a DVRIP session that is reused until the recorder drops it."""

    def __init__(self, address, username, password):
        self.address = address
        self.username = username
        self.password = password
        self.lock = threading.Lock()
        self.sock = None
        self.session = 0
        self.seq = 0

    def _close(self):
        if self.sock is not None:
            self.sock.close()
        self.sock = None

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise SessionLost(f"{self.address} closed the connection")
            buf += chunk
        return buf

    def _call(self, msg, body):
        payload = json.dumps(body).encode() + b"\x0a\x00"
        self.sock.sendall(HEADER.pack(0xFF, 0, self.session, self.seq, msg, len(payload)) + payload)
        self.seq += 1
        length = HEADER.unpack(self._recv(HEADER.size))[-1]
        reply = json.loads(self._recv(length).rstrip(b"\x00\x0a").decode("utf-8", "replace"))
        if reply.get("Ret") == RET_NOT_LOGGED_IN:
            raise SessionLost(f"{self.address} dropped the session")
        if reply.get("Ret") != RET_OK:
            raise DvripError(f"{self.address}: message {msg} {body.get('Name', '')!r} returned Ret={reply.get('Ret')}")
        return reply

    def _login(self):
        self._close()
        self.sock = socket.create_connection((self.address, PORT), timeout=TIMEOUT)
        self.session, self.seq = 0, 0
        reply = self._call(MSG_LOGIN, {"EncryptType": "MD5", "LoginType": "DVRIP-Web",
                                       "UserName": self.username, "PassWord": sofia_hash(self.password)})
        self.session = int(reply["SessionID"], 16)

    def _get(self, msg, name):
        return self._call(msg, {"Name": name, "SessionID": f"0x{self.session:08X}"})[name]

    def fetch(self, requests):
        """Run (message, name) requests in one session, logging in again once if it is gone."""
        with self.lock:
            for attempt in range(2):
                try:
                    if self.sock is None:
                        self._login()
                    return [self._get(msg, name) for msg, name in requests]
                except (SessionLost, OSError):
                    self._close()
                    if attempt:
                        raise
                except Exception:
                    self._close()
                    raise
        raise AssertionError("unreachable")

    def keepalive(self):
        """Keep an idle session from expiring between scrapes; a lost one is reopened on the next scrape."""
        with self.lock:
            if self.sock is None:
                return
            try:
                self._call(MSG_KEEPALIVE, {"Name": "KeepAlive", "SessionID": f"0x{self.session:08X}"})
            except (DvripError, OSError, ValueError):
                self._close()


def esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Metrics:
    def __init__(self):
        self.families = {}

    def add(self, name, kind, help_text, value, **labels):
        family = self.families.setdefault(name, (kind, help_text, []))
        family[2].append((labels, value))

    def render(self):
        out = []
        for name, (kind, help_text, samples) in self.families.items():
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                lbl = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")
        return "\n".join(out) + "\n"


def collect(recorder):
    m = Metrics()

    info, storage, work, chn_status, titles, clock = recorder.fetch([
        (MSG_INFO, "SystemInfo"),
        (MSG_INFO, "StorageInfo"),
        (MSG_INFO, "WorkState"),
        (MSG_CONFIG, "NetWork.ChnStatus"),
        (MSG_CHANNEL_TITLE, "ChannelTitle"),
        (MSG_TIME, "OPTimeQuery"),
    ])

    m.add("dvr_info", "gauge", "Recorder model and firmware.", 1,
          model=info.get("HardWare", ""), firmware=info.get("SoftWareVersion", ""),
          build=info.get("BuildTime", ""))

    # Channels: only the IP channels that have a camera configured (NoConfig = empty slot).
    # ChnStatus, WorkState.ChannelState and ChannelTitle are all indexed by channel.
    states = work["ChannelState"]
    loss, blind, motion = (int(work["AlarmState"][k]) for k in ("VideoLoss", "VideoBlind", "VideoMotion"))
    for i, chn in enumerate(chn_status):
        if chn["Status"] in ("", "NoConfig"):
            continue
        labels = {"channel": str(i + 1), "title": titles[i] if i < len(titles) else chn["ChnName"]}
        m.add("dvr_channel_connected", "gauge", "The recorder is receiving the camera's stream.",
              int(chn["Status"] == "Connected"), **labels)
        m.add("dvr_channel_recording", "gauge", "The channel is being recorded right now.",
              int(bool(states[i]["Record"])), **labels)
        # WorkState reports kbit/s.
        m.add("dvr_channel_bitrate_bits", "gauge", "Current stream bitrate, bits per second.",
              int(states[i]["Bitrate"]) * 1000, **labels)
        m.add("dvr_channel_video_loss", "gauge", "Video loss alarm on the channel.", (loss >> i) & 1, **labels)
        m.add("dvr_channel_video_blind", "gauge", "Camera tampering (blind) alarm on the channel.",
              (blind >> i) & 1, **labels)
        m.add("dvr_channel_motion", "gauge", "Motion detected on the channel.", (motion >> i) & 1, **labels)

    # Disks: every physical disk has a fixed set of partition slots, unused ones are all zeros.
    # The archive spans from the oldest start to the newest end over all partitions.
    disks, starts, ends = 0, [], []
    for disk in storage or []:
        for n, part in enumerate(disk["Partition"]):
            size = int(part["TotalSpace"], 16)
            if not size:
                continue
            disks += 1
            labels = {"disk": str(disk["PlysicalNo"]), "partition": str(n)}
            m.add("dvr_disk_size_bytes", "gauge", "Partition size.", size * MIB, **labels)
            # With overwrite on (the default) a full disk is normal: the oldest video gets replaced.
            m.add("dvr_disk_free_bytes", "gauge", "Free space on the partition.", int(part["RemainSpace"], 16) * MIB, **labels)
            m.add("dvr_disk_status", "gauge", "Partition status as the recorder reports it, 0 = normal.",
                  int(part["Status"]), **labels)
            m.add("dvr_disk_current", "gauge", "The partition being recorded to now.", int(bool(part["IsCurrent"])), **labels)
            starts += [t for t in (parse_time(part["OldStartTime"]), parse_time(part["NewStartTime"])) if t]
            ends += [t for t in (parse_time(part["OldEndTime"]), parse_time(part["NewEndTime"])) if t]
    m.add("dvr_disks", "gauge", "Partitions with non-zero capacity (0 = no usable disk).", disks)

    # Both sides come from the recorder's own clock, so its time zone does not matter.
    if ends:
        now = parse_time(clock)
        m.add("dvr_last_record_age_seconds", "gauge", "Time since the newest recorded video, by the recorder's clock.",
              int((now - max(ends)).total_seconds()))
        m.add("dvr_archive_depth_seconds", "gauge", "Span of the archive, from the oldest to the newest video.",
              int((max(ends) - min(starts)).total_seconds()) if starts else 0)

    return m.render()


class Handler(BaseHTTPRequestHandler):
    recorders = {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/metrics":
            self._reply(200, "DVRIP recorder exporter: /metrics?target=<address>\n")
            return
        target = urllib.parse.parse_qs(url.query).get("target", [""])[0]
        recorder = self.recorders.get(target)
        if recorder is None:
            self._reply(400, f"unknown target {target!r}\n")
            return
        try:
            body = collect(recorder)
        except Exception as exc:  # noqa: BLE001 - any failure means the scrape failed
            # A non-2xx answer makes Prometheus set up=0, which InstanceDown watches.
            self._reply(500, f"scrape of {target} failed: {exc}\n")
            return
        self._reply(200, body, "text/plain; version=0.0.4")

    def _reply(self, code, body, ctype="text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Successful scrapes would flood the journal; keep only errors.
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write(fmt % args + "\n")


def keepalive_loop(recorders, interval, stop):
    while not stop.wait(interval):
        for recorder in recorders:
            recorder.keepalive()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config.file", dest="config", required=True,
                    help='JSON: {"<address>": {"username": ..., "password": ...}}')
    ap.add_argument("--web.listen-address", dest="listen", default="127.0.0.1:9118")
    # Recorders announce AliveInterval ~20 s at login and drop sessions that stay silent.
    ap.add_argument("--keepalive-interval", dest="keepalive", type=float, default=15)
    args = ap.parse_args()

    with open(args.config) as f:
        Handler.recorders = {addr: Recorder(addr, c["username"], c["password"]) for addr, c in json.load(f).items()}

    threading.Thread(target=keepalive_loop, args=(Handler.recorders.values(), args.keepalive, threading.Event()),
                     daemon=True).start()

    host, port = args.listen.rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
