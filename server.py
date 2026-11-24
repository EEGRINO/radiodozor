"""Local dashboard and CSI receiver for the Радиодозор S3 experiment."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from math import ceil
from pathlib import Path
import socket
from threading import Event, Lock, Thread
import time
import webbrowser

from csi import from_udp, from_visor
from sensing import Baseline, CALIBRATION, COUNTDOWN, STALE, Channel

ROOT = Path(__file__).resolve().parent
MAX_LINES = 16


def load_baselines(path):
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        return {key: Baseline(**limits) for key, limits in stored.items()
                if limits["motion"] > 0 and limits["rhythm"] > 0}
    except (OSError, TypeError, ValueError, AttributeError, KeyError):
        return {}


class Station:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = Lock()
        self.lines = {}
        self.results = {}
        self.baselines = load_baselines(data_dir / "baseline.json")
        self.sleep_baselines = load_baselines(data_dir / "sleep_baseline.json")
        self.operation = "guard"
        self.sleep_since = None
        self.calibration = None
        self.calibration_result = None
        self.armed = False
        self.alarm = None
        self.candidate = None
        self.votes = 0
        self.events = deque(maxlen=25)
        self.storage_error = False
        self.received = 0
        self.rejected = 0
        self.mode = ""

    def active_baselines(self):
        return self.sleep_baselines if self.operation == "sleep" else self.baselines

    def ingest(self, frame, at):
        with self.lock:
            if frame is None:
                self.rejected += 1
                return
            if frame.key not in self.lines:
                if len(self.lines) >= MAX_LINES:
                    oldest = min(self.lines, key=lambda key: self.lines[key].last)
                    if at - self.lines[oldest].last <= STALE:
                        self.rejected += 1
                        return
                    self.lines.pop(oldest)
                self.lines[frame.key] = Channel()
            self.lines[frame.key].add(frame, at)
            self.received += 1

    def log(self, event, **values):
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **values}
        self.events.appendleft(row)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            self.storage_error = True

    def finish_calibration(self, end):
        limits = {}
        for key, line in self.lines.items():
            baseline = line.calibrate(end)
            if baseline is not None:
                limits[key] = baseline
        self.calibration = None
        if not limits:
            self.calibration_result = "no_data"
            self.log("calibration_failed")
            return
        if self.operation == "sleep":
            self.sleep_baselines = limits
            self.sleep_since = end
            filename = "sleep_baseline.json"
        else:
            self.baselines = limits
            filename = "baseline.json"
        self.calibration_result = "ready"
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / filename
            temporary = path.with_suffix(".tmp")
            values = {key: asdict(baseline) for key, baseline in limits.items()}
            temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            self.storage_error = True
        self.log("calibration_finished", lines=len(limits), operation=self.operation)

    def update(self, now):
        with self.lock:
            if self.calibration is not None and now >= self.calibration + CALIBRATION:
                self.finish_calibration(self.calibration + CALIBRATION)
            baselines = self.active_baselines()
            self.results = {
                key: line.measure(now, baselines.get(key), self.sleep_since)
                for key, line in self.lines.items()
            }
            if self.operation != "guard" or not self.armed or self.alarm:
                return
            signals = [(key, value) for key, value in self.results.items() if value["state"] in ("motion", "rhythm")]
            if not signals:
                self.candidate, self.votes = None, 0
                return
            key, result = max(signals, key=lambda pair: (
                pair[1]["state"] == "motion", pair[1].get("motion", 0) if pair[1]["state"] == "motion"
                else pair[1].get("rhythm_strength", 0)
            ))
            reason = result["state"]
            candidate = (key, reason)
            self.votes = self.votes + 1 if self.candidate == candidate else 1
            self.candidate = candidate
            if self.votes >= (2 if reason == "motion" else 3):
                self.alarm = reason
                self.log("alarm", reason=reason, link=key, rpm=result.get("rpm"))

    def action(self, name, now):
        with self.lock:
            if name in ("guard", "sleep"):
                if name == self.operation:
                    return None
                if self.armed:
                    return "Выключите тревогу перед сменой режима."
                if self.calibration is not None:
                    return "Завершите или отмените запись фона."
                self.operation = name
                self.sleep_since = now if name == "sleep" else None
                self.results = {}
                self.calibration_result = None
                self.log("sleep_mode" if name == "sleep" else "guard_mode")
            elif name == "calibrate":
                if self.armed:
                    return "Снимите режим наблюдения перед записью фона."
                if self.calibration is not None:
                    return "Фон уже записывается."
                if not any(now - line.last <= STALE for line in self.lines.values()):
                    return "Для записи фона нужен свежий CSI."
                self.calibration = now + COUNTDOWN
                self.calibration_result = None
                self.log("calibration_started", operation=self.operation)
            elif name == "cancel":
                if self.calibration is not None:
                    self.calibration = None
                    self.log("calibration_cancelled")
            elif name == "arm":
                if self.operation != "guard":
                    return "Тревога доступна в режиме «Охрана помещения»."
                if self.calibration is not None or not any(
                    key in self.baselines and now - line.last <= STALE for key, line in self.lines.items()
                ):
                    return "Сначала запишите фон комнаты и проверьте, что CSI поступает."
                if not self.armed:
                    self.armed, self.alarm = True, None
                    self.candidate, self.votes = None, 0
                    self.log("armed")
            elif name == "disarm":
                if self.armed:
                    self.armed, self.alarm = False, None
                    self.candidate, self.votes = None, 0
                    self.log("disarmed")
            elif name == "clear":
                if self.alarm:
                    self.alarm = None
                    self.candidate, self.votes = None, 0
                    self.log("alarm_cleared")
            else:
                return "Неизвестное действие."
        return None

    def snapshot(self, now):
        with self.lock:
            lines = []
            baselines = self.active_baselines()
            for key, line in self.lines.items():
                age = now - line.last
                if age > STALE:
                    continue
                calibrated = key in baselines
                pending = "settling" if calibrated and self.operation == "sleep" else "needs_calibration"
                measurement = self.results.get(key, {"state": pending})
                lines.append({**line.frame.wire_view(max(0, age)), **measurement, "calibrated": calibrated})
            phase = None
            if self.calibration is not None:
                remaining = int(ceil(self.calibration - now))
                phase = {"state": "countdown", "seconds": remaining} if remaining > 0 else {
                    "state": "recording", "seconds": max(0, int(ceil(self.calibration + CALIBRATION - now))),
                }
            return {
                "lines": lines, "calibration": phase, "calibration_result": self.calibration_result,
                "armed": self.armed, "alarm": self.alarm, "events": list(self.events),
                "mode": self.mode, "operation": self.operation,
                "received": self.received, "rejected": self.rejected,
                "storage_error": self.storage_error,
            }

    def journal(self):
        with self.lock:
            try:
                return (self.data_dir / "events.jsonl").read_bytes()
            except FileNotFoundError:
                return b""


def serve_udp(sock, station, stop):
    with sock:
        sock.settimeout(.4)
        while not stop.is_set():
            try:
                data, (address, _) = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except ConnectionResetError:
                continue
            station.ingest(from_udp(data, address), time.monotonic())


async def read_visor(url, station, stop):
    from websockets.asyncio.client import connect
    from websockets.exceptions import WebSocketException

    while not stop.is_set():
        try:
            async with connect(url, proxy=None, open_timeout=3, close_timeout=1) as ws:
                while not stop.is_set():
                    started = time.monotonic()
                    await ws.send('{"cmd":"visor_snapshot"}')
                    payload = json.loads(await asyncio.wait_for(ws.recv(), 3))
                    if payload.get("type") == "visor_csi":
                        now = time.monotonic()
                        for row in payload.get("frames", []):
                            frame = from_visor(row)
                            if frame is not None:
                                station.ingest(frame, now - row["age_ms"] / 1000)
                    await asyncio.sleep(max(0, .045 - (time.monotonic() - started)))
        except (OSError, TimeoutError, ValueError, WebSocketException):
            await asyncio.sleep(1)


def analyser(station, stop):
    while not stop.wait(1):
        station.update(time.monotonic())


def handler_for(station, port):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=ROOT / "static", **kwargs)

        def payload(self, body, mime, status=HTTPStatus.OK, attachment=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(body)

        def json(self, value, status=HTTPStatus.OK):
            self.payload(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                self.send_error(HTTPStatus.FORBIDDEN)
            elif self.path == "/api/state":
                self.json(station.snapshot(time.monotonic()))
            elif self.path == "/api/events":
                try:
                    content = station.journal()
                except OSError:
                    self.json({"error": "Журнал недоступен."}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                self.payload(content, "application/x-ndjson", attachment="radiodozor-events.jsonl")
            else:
                super().do_GET()

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") not in hosts or
                    origin not in {None, *(f"http://{host}" for host in hosts)} or
                    self.headers.get("Content-Type") != "application/json" or
                    not self.path.startswith("/api/action/")):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            error = station.action(self.path.rsplit("/", 1)[-1], time.monotonic())
            self.json({"error": error}, HTTPStatus.CONFLICT if error else HTTPStatus.OK)

        def log_message(self, fmt, *args):
            if self.path != "/api/state":
                super().log_message(fmt, *args)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Радиодозор · Wi-Fi CSI")
    parser.add_argument("--port", type=int, default=8010, help="локальный HTTP-порт")
    parser.add_argument("--udp-port", type=int, default=5555, help="UDP-порт ESP32-S3")
    parser.add_argument("--from-visor", metavar="URL", help="поток /visor запущенного WiFi DensePose")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    station = Station(args.data_dir)
    stop = Event()
    sock = None
    try:
        if args.from_visor:
            station.mode = "bridge"
            receiver = Thread(target=lambda: asyncio.run(read_visor(args.from_visor, station, stop)), daemon=True)
        else:
            station.mode = "udp"
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("0.0.0.0", args.udp_port))
            receiver = Thread(target=serve_udp, args=(sock, station, stop), daemon=True)
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(station, args.port)) as server:
            receiver.start()
            Thread(target=analyser, args=(station, stop), daemon=True).start()
            url = f"http://127.0.0.1:{args.port}/"
            source = args.from_visor or f"UDP :{args.udp_port}"
            print(f"Радиодозор · {url} · {source}", flush=True)
            if not args.no_browser:
                Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"Не удалось запустить Радиодозор ({error}). Если WiFi DensePose уже запущен, используйте --from-visor ws://127.0.0.1:8765/visor.\n")
    finally:
        stop.set()
        if sock is not None:
            sock.close()


if __name__ == "__main__":
    main()
