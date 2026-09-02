"""On-site day-1 test: is the Deli E-13750 actually ZK-protocol compatible?

The whole live-network design rests on this one unverified assumption. Run this
FIRST, on the site PC, before anything else. It is standalone — it needs only
`pip install pyzk` and nothing from the rest of this repo.

    python agent/probe_device.py --ip 192.168.1.201
    python agent/probe_device.py --ip 192.168.1.201 --port 4370 --dump 20
    python agent/probe_device.py --ip 192.168.1.201 --force-udp

What it does, in order, stopping at the first hard failure:
    1. TCP reachability on the port (plain socket)
    2. ZK connect / handshake
    3. read device metadata (firmware, serial, platform, user count)
    4. pull the user list (device_user_id -> name)
    5. pull recent attendance logs and print a sample

Exit code 0 = ZK works here, proceed with the live agent.
Exit code 2 = ZK did NOT work — fall back to USB/CSV export ingestion.

Whatever it prints, save the full output — it tells us which firmware quirks
the Phase 4 agent has to handle.
"""

from __future__ import annotations

import argparse
import socket
import sys
from contextlib import closing


def _stamp(msg: str, ok: bool | None = None) -> None:
    mark = "    " if ok is None else (" ok " if ok else "FAIL")
    print(f"[{mark}] {msg}")


def check_tcp(ip: str, port: int, timeout: float) -> bool:
    _stamp(f"TCP {ip}:{port} …")
    try:
        with closing(socket.create_connection((ip, port), timeout=timeout)):
            _stamp(f"TCP {ip}:{port} reachable", ok=True)
            return True
    except OSError as exc:
        _stamp(f"TCP {ip}:{port} unreachable — {exc}", ok=False)
        _stamp("  the device is off, on another subnet, or the port differs.")
        return False


def probe_zk(args: argparse.Namespace) -> int:
    try:
        from zk import ZK
    except ImportError:
        _stamp("pyzk not installed. Run:  pip install pyzk", ok=False)
        return 2

    zk = ZK(
        args.ip,
        port=args.port,
        timeout=args.timeout,
        password=args.password,
        force_udp=args.force_udp,
        ommit_ping=True,
    )

    conn = None
    try:
        _stamp("ZK connect / handshake …")
        conn = zk.connect()
        _stamp("ZK handshake succeeded", ok=True)

        conn.disable_device()
        try:
            _stamp(f"firmware       : {conn.get_firmware_version()}")
            _stamp(f"serial number  : {conn.get_serialnumber()}")
            _stamp(f"platform       : {conn.get_platform()}")
            _stamp(f"device name    : {conn.get_device_name()}")
        except Exception as exc:  # noqa: BLE001
            _stamp(f"metadata read partial — {exc}", ok=False)

        users = conn.get_users()
        _stamp(f"users on device: {len(users)}", ok=True)
        for u in users[: args.dump]:
            _stamp(f"  user {u.user_id!r:>8}  name={u.name!r}  card={getattr(u, 'card', '')}")

        logs = conn.get_attendance()
        _stamp(f"attendance logs: {len(logs)}", ok=True)
        for rec in logs[-args.dump :]:
            _stamp(
                f"  {rec.timestamp}  uid={rec.user_id!r:>8}  "
                f"status={rec.status}  punch={rec.punch}"
            )

        _stamp("", None)
        _stamp("RESULT: ZK protocol works on this device. Proceed with the live agent.", ok=True)
        return 0

    except Exception as exc:  # noqa: BLE001
        _stamp(f"ZK communication failed — {type(exc).__name__}: {exc}", ok=False)
        _stamp("", None)
        _stamp("RESULT: ZK protocol did NOT work here.", ok=False)
        _stamp("  Fall back to USB / CSV export. The ingest API contract is the")
        _stamp("  same — a CSV importer POSTs the same PunchBatch payload.")
        return 2
    finally:
        if conn is not None:
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", required=True, help="device IP on the site LAN")
    parser.add_argument("--port", type=int, default=4370)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--password", type=int, default=0, help="device comm key, usually 0")
    parser.add_argument("--force-udp", action="store_true", help="try if TCP handshake hangs")
    parser.add_argument("--dump", type=int, default=10, help="sample rows to print")
    args = parser.parse_args()

    print(f"\nProbing attendance device at {args.ip}:{args.port}\n" + "=" * 60)
    if not check_tcp(args.ip, args.port, args.timeout) and not args.force_udp:
        _stamp("", None)
        _stamp("Stopping — no TCP route. Check the cable, IP, and subnet first.", ok=False)
        _stamp("If the device is UDP-only, re-run with --force-udp.")
        return 2
    return probe_zk(args)


if __name__ == "__main__":
    sys.exit(main())
