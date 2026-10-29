#!/usr/bin/env python3
"""Prometheus exporter for SKS3200-series web-managed switches (no SNMP).

The switch's web UI is backed by a small JSON API; this exporter logs in the
way the UI does and turns those JSON pages into metrics. One instance serves
every switch: Prometheus passes the switch as ?target=<address>, credentials
come from the config file keyed by that address.

Managed by Ansible (roles/monitoring) — do not edit on the host.
"""

import argparse
import hashlib
import http.cookiejar
import json
import re
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TIMEOUT = 10

# Link_Status / Spd_Duplex_Actual look like "100MbpsFull", "10GbpsFull", "Link Down".
LINK_RE = re.compile(r"^(\d+(?:\.\d+)?)([MG])bps(Full|Half)$")


class LoginError(Exception):
    pass


class Switch:
    """One switch: a cookie session that is reused until the switch drops it."""

    def __init__(self, address, username, password):
        self.address = address
        self.username = username
        self.password = password
        self.lock = threading.Lock()
        self._new_session()

    def _new_session(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _get(self, path, params=None):
        url = f"http://{self.address}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")

    def _login(self):
        self._new_session()
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731 - what the UI sends
        body = self._get("authorize", {"loginusr": md5(self.username), "loginpwd": md5(self.password)})
        # Success redirects into the UI, failure back to login.html.
        if "login.html" in body:
            raise LoginError(f"login to {self.address} rejected")

    def fetch(self, page):
        """GET a JSON page, logging in again once if the session has expired."""
        with self.lock:
            for attempt in range(2):
                # The "not logged in" answer is followed by a second, duplicated
                # HTTP response in the same body: read only the first JSON value.
                data, _ = json.JSONDecoder().raw_decode(self._get(page).lstrip())
                if isinstance(data, dict) and "redirect" in data:
                    if attempt:
                        raise LoginError(f"{self.address} keeps redirecting {page} to login")
                    self._login()
                    continue
                return data
        raise AssertionError("unreachable")


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


def ports(data):
    """Port_<n> entries of a JSON page, in port order."""
    for n in range(1, int(data["PortNum"]) + 1):
        yield str(n), data[f"Port_{n}"]


def collect(switch):
    m = Metrics()

    status = switch.fetch("status.json")
    m.add("sks_info", "gauge", "Switch model and firmware.", 1,
          model=status.get("des", "").strip(), firmware=status.get("fw_ver", ""),
          hardware=status.get("hw_ver", ""), mac=status.get("sys_macaddr", ""))
    m.add("sks_temperature_celsius", "gauge", "Switch board temperature.", float(status["temperature"]))

    for port, p in ports(switch.fetch("port_statistics.json")):
        m.add("sks_port_enabled", "gauge", "Port administratively enabled.",
              int(p["Port_Status"] == "Enabled"), port=port)
        link = LINK_RE.match(p["Link_Status"])
        m.add("sks_port_up", "gauge", "Port has a link.", int(bool(link)), port=port)
        speed = 0
        if link:
            speed = float(link.group(1)) * (1e9 if link.group(2) == "G" else 1e6)
        m.add("sks_port_speed_bits", "gauge", "Negotiated link speed, 0 when down.", int(speed), port=port)
        m.add("sks_port_full_duplex", "gauge", "Link is full duplex.",
              int(bool(link) and link.group(3) == "Full"), port=port)
        # The switch counts packets only, no octets.
        for key, metric, help_text in (
            ("TxGoodPkt", "sks_port_tx_packets_total", "Packets sent."),
            ("RxGoodPkt", "sks_port_rx_packets_total", "Packets received."),
            ("TxBadPkt", "sks_port_tx_errors_total", "Bad packets sent."),
            ("RxBadPkt", "sks_port_rx_errors_total", "Bad packets received."),
        ):
            m.add(metric, "counter", help_text, int(p[key]), port=port)

    # Link aggregation: portTypeId 0 = static, 2 = LACP; grpInd 0 = no group;
    # state 1 = the port carries traffic (for a LAG member: selected, not standby).
    for port, p in ports(switch.fetch("port_trunk_cfg.json")):
        group = p[f"Port_{port}_grpInd"]
        if not group:
            continue
        mode = {0: "static", 2: "lacp"}.get(p[f"portTypeId_{port}"], str(p[f"portTypeId_{port}"]))
        m.add("sks_lag_member_active", "gauge", "LAG member port is selected (1) or standby/down (0).",
              int(p[f"Port_{port}_state"] == 1), port=port, group=str(group), mode=mode)

    return m.render()


class Handler(BaseHTTPRequestHandler):
    switches = {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/metrics":
            self._reply(200, "SKS switch exporter: /metrics?target=<address>\n")
            return
        target = urllib.parse.parse_qs(url.query).get("target", [""])[0]
        switch = self.switches.get(target)
        if switch is None:
            self._reply(400, f"unknown target {target!r}\n")
            return
        try:
            body = collect(switch)
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config.file", dest="config", required=True,
                    help='JSON: {"<address>": {"username": ..., "password": ...}}')
    ap.add_argument("--web.listen-address", dest="listen", default="127.0.0.1:9117")
    args = ap.parse_args()

    with open(args.config) as f:
        Handler.switches = {addr: Switch(addr, c["username"], c["password"]) for addr, c in json.load(f).items()}

    host, port = args.listen.rsplit(":", 1)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()


if __name__ == "__main__":
    main()
