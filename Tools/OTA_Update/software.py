"""
chirpstack_ota.py  —  LoRaWAN OTA firmware updater
Sends firmware in fixed-size chunks over ChirpStack gRPC downlinks.
One chunk in queue at a time, delivery confirmed by queue depth == 0.

Diagnosis results:
  - GetDevice FAILS (device_info field missing in installed chirpstack-api)
  - GetQueue WORKS  (returns correct depth)
  - FlushQueue WORKS
  - Enqueue WORKS

Strategy: poll GetQueue only. No GetDevice needed.
  Enqueue chunk → poll until depth==0 → next chunk.

Requirements:
    pip install chirpstack-api grpcio rich

Usage:
    python chirpstack_ota.py firmware.bin
    python chirpstack_ota.py firmware.bin --chunk-size 200 --fport 10
"""

import json
import grpc
import sys
import time
import argparse
from chirpstack_api import api
from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn,
    TimeElapsedColumn, TimeRemainingColumn, SpinnerColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text

# ─────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE      = 50      # bytes of firmware payload per frame
DEFAULT_FPORT           = 10      # LoRaWAN fPort
DEFAULT_POLL_TIMEOUT    = 120     # seconds to wait for queue to empty per chunk
DEFAULT_RETRIES         = 3       # retries per chunk
POLL_INTERVAL           = 1.0     # seconds between GetQueue polls

console = Console()


# ─────────────────────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────────────────────

def load_config(path: str = "config.json") -> dict:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        console.print(f"[bold red][ERROR][/] Config file not found: {path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        console.print(f"[bold red][ERROR][/] Invalid JSON in config: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# OTA class
# ─────────────────────────────────────────────────────────────

class ChirpStackOTA:
    def __init__(self, config: dict):
        self.server     = config["chirpstack"]["device_api_url"]
        self.api_token  = config["chirpstack"]["api_token"]
        self.dev_eui    = config["device"]["dev_eui"]

        self.channel    = grpc.insecure_channel(self.server)
        self.client     = api.DeviceServiceStub(self.channel)
        self.auth_token = [("authorization", f"Bearer {self.api_token}")]

    # ── Queue management ───────────────────────────────────────

    def flush_queue(self) -> None:
        req = api.FlushDeviceQueueRequest()
        req.dev_eui = self.dev_eui
        self.client.FlushQueue(req, metadata=self.auth_token)

    def get_queue_depth(self) -> int:
        """
        Returns number of items in downlink queue, or -1 on error.
        NOTE: GetDevice is broken in this chirpstack-api version (missing
        device_info field), so we use GetQueue exclusively for delivery
        detection. GetQueue works correctly.
        """
        try:
            req = api.GetDeviceQueueItemsRequest()
            req.dev_eui = self.dev_eui
            resp = self.client.GetQueue(req, metadata=self.auth_token)
            return len(resp.result)
        except Exception as e:
            return -1

    # ── Low-level enqueue ──────────────────────────────────────

    def send_downlink(self, port: int, data: bytes) -> str:
        """Enqueue one unconfirmed downlink. Returns downlink ID."""
        req = api.EnqueueDeviceQueueItemRequest()
        req.queue_item.confirmed = False
        req.queue_item.data      = data
        req.queue_item.dev_eui   = self.dev_eui
        req.queue_item.f_port    = port
        resp = self.client.Enqueue(req, metadata=self.auth_token)
        return resp.id

    # ── Delivery detection: poll queue depth ───────────────────

    def _wait_for_queue_empty(self, timeout: float, progress=None) -> tuple:
        """
        Poll GetQueue until depth == 0 (ChirpStack transmitted the downlink)
        or timeout expires.
        Returns (delivered: bool, elapsed_seconds: float).
        """
        deadline  = time.monotonic() + timeout
        t_start   = time.monotonic()
        last_log  = time.monotonic()

        while time.monotonic() < deadline:
            depth   = self.get_queue_depth()
            waiting = time.monotonic() - t_start

            # Print status every 5s so we can see it's alive
            if progress is not None and time.monotonic() - last_log >= 5:
                if depth == -1:
                    progress.log(f"[dim]  waiting {waiting:.0f}s — GetQueue error[/]")
                else:
                    progress.log(f"[dim]  waiting {waiting:.0f}s — queue depth={depth}[/]")
                last_log = time.monotonic()

            if depth == 0:
                return True, time.monotonic() - t_start

            time.sleep(POLL_INTERVAL)

        return False, timeout

    # ── High-level firmware upload ─────────────────────────────

    def upload_firmware(
        self,
        firmware_path:  str,
        chunk_size:     int   = DEFAULT_CHUNK_SIZE,
        fport:          int   = DEFAULT_FPORT,
        poll_timeout:   float = DEFAULT_POLL_TIMEOUT,
        retries:        int   = DEFAULT_RETRIES,
    ) -> None:
        """
        Stream firmware to the device one chunk at a time.

        Frame layout (7-byte header):
          Byte 0:    sequence number       uint8
          Byte 1-2:  total chunks          uint16 big-endian
          Byte 3-6:  total firmware bytes  uint32 big-endian  (for Update.begin())
          Byte 7+:   firmware payload
        """

        # ── Read firmware ──────────────────────────────────────
        try:
            with open(firmware_path, "rb") as f:
                firmware = f.read()
        except FileNotFoundError:
            console.print(f"[bold red][ERROR][/] File not found: {firmware_path}")
            sys.exit(1)

        total_bytes = len(firmware)
        if total_bytes == 0:
            console.print("[bold red][ERROR][/] Firmware file is empty.")
            sys.exit(1)

        chunks = [
            firmware[i : i + chunk_size]
            for i in range(0, total_bytes, chunk_size)
        ]
        total_chunks = len(chunks)

        # ── Summary panel ──────────────────────────────────────
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", min_width=16)
        t.add_column(style="white")
        t.add_row("Firmware",       firmware_path)
        t.add_row("Size",           f"{total_bytes:,} bytes")
        t.add_row("Chunks",         f"{total_chunks}  x  {chunk_size}B payload + 7B header")
        t.add_row("fPort",          str(fport))
        t.add_row("Poll timeout",   f"{poll_timeout}s per chunk")
        t.add_row("Retries",        str(retries))
        t.add_row("Device EUI",     self.dev_eui)
        t.add_row("Delivery check", "GetQueue depth == 0")
        console.print(Panel(t, title="[bold yellow]Transfer Settings[/]",
                             border_style="yellow", box=box.ROUNDED))

        # ── Flush existing queue ───────────────────────────────
        console.print("[cyan]Flushing device queue...[/]", end=" ")
        try:
            self.flush_queue()
            console.print("[green]done[/]")
        except grpc.RpcError as e:
            console.print(f"[red]FAILED ({e.code()})[/] — continuing anyway")

        # Verify GetQueue works before starting
        depth = self.get_queue_depth()
        if depth == -1:
            console.print("[bold red][ERROR][/] GetQueue is not working — cannot proceed.")
            sys.exit(1)
        console.print(f"[green]Queue verified: depth={depth}[/]\n")

        # ── Progress bar ───────────────────────────────────────
        progress = Progress(
            SpinnerColumn(spinner_name="dots", style="yellow"),
            TextColumn("[bold white]{task.description}"),
            BarColumn(bar_width=34, style="dark_orange", complete_style="green"),
            TextColumn("[cyan]{task.completed}[white]/[yellow]{task.total}"),
            TextColumn("[white]chunks  •"),
            TimeElapsedColumn(),
            TextColumn("[white]ETA"),
            TimeRemainingColumn(),
            console=console,
        )

        stats = {"retries_total": 0, "start_time": time.time()}

        with progress:
            task = progress.add_task(
                f"Chunk [cyan]0[/]/[yellow]{total_chunks}[/]",
                total=total_chunks,
            )

            for idx, chunk in enumerate(chunks):
                # ── 7-byte header ───────────────────────────────
                seq      =  idx & 0xFF
                total_hi = (total_chunks >> 8)  & 0xFF
                total_lo =  total_chunks        & 0xFF
                size_b0  = (total_bytes  >> 24) & 0xFF
                size_b1  = (total_bytes  >> 16) & 0xFF
                size_b2  = (total_bytes  >>  8) & 0xFF
                size_b3  =  total_bytes         & 0xFF
                frame    = bytes([seq, total_hi, total_lo,
                                  size_b0, size_b1, size_b2, size_b3]) + chunk

                delivered = False

                for attempt in range(retries + 1):
                    if attempt > 0:
                        stats["retries_total"] += 1
                        progress.log(
                            f"[yellow]Retry {attempt}/{retries}[/]  "
                            f"chunk {idx + 1}/{total_chunks}"
                        )
                        # Flush before retry to avoid double-queuing
                        try:
                            self.flush_queue()
                            time.sleep(1)
                        except Exception:
                            pass

                    # Enqueue the chunk
                    try:
                        dl_id = self.send_downlink(fport, frame)
                    except grpc.RpcError as e:
                        progress.log(
                            f"[bold red]gRPC error[/] chunk {idx + 1}: "
                            f"{e.code()} — {e.details()}"
                        )
                        time.sleep(2)
                        continue

                    # Wait for queue to empty = delivery confirmed
                    delivered, chunk_elapsed = self._wait_for_queue_empty(
                        poll_timeout, progress=progress
                    )

                    if delivered:
                        if chunk_elapsed > 30:
                            progress.log(
                                f"[yellow]  chunk {idx + 1} took {chunk_elapsed:.0f}s "
                                f"— consider checking ChirpStack ADR / scheduler[/]"
                            )
                        break
                    else:
                        progress.log(
                            f"[yellow]Queue did not empty within {poll_timeout}s "
                            f"for chunk {idx + 1} — retrying[/]"
                        )

                if not delivered:
                    progress.log(
                        f"[bold red]Chunk {idx + 1} failed after {retries} retries. "
                        f"Aborting.[/]"
                    )
                    _print_summary(stats, idx + 1, total_chunks, failed=True)
                    sys.exit(1)

                progress.update(
                    task,
                    advance=1,
                    description=(
                        f"Chunk [cyan]{idx + 1}[/]/[yellow]{total_chunks}[/]  "
                        f"[dim]id={dl_id[:8]}[/]"
                    ),
                )

        _print_summary(stats, total_chunks, total_chunks, failed=False)


# ─────────────────────────────────────────────────────────────
# Summary panel
# ─────────────────────────────────────────────────────────────

def _print_summary(stats: dict, sent: int, total: int, failed: bool) -> None:
    elapsed = time.time() - stats["start_time"]
    status  = "[bold red]FAILED[/]" if failed else "[bold green]SUCCESS[/]"
    color   = "red" if failed else "green"

    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", min_width=16)
    t.add_column()
    t.add_row("Status",        status)
    t.add_row("Chunks sent",   f"{sent}/{total}")
    t.add_row("Total retries", str(stats["retries_total"]))
    t.add_row("Elapsed",       f"{elapsed:.1f}s")
    if sent > 0 and elapsed > 0:
        t.add_row("Avg / chunk", f"{elapsed / sent:.2f}s")

    console.print(Panel(t, title="[bold]Transfer Summary[/]",
                         border_style=color, box=box.ROUNDED))


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    console.print(
        Panel(
            Text(
                "LoRaWAN OTA Update\nChirpStack gRPC Downlink Tool",
                justify="center",
                style="bold yellow",
            ),
            border_style="yellow",
            box=box.DOUBLE_EDGE,
        )
    )

    parser = argparse.ArgumentParser(
        description="LoRaWAN OTA firmware updater via ChirpStack gRPC"
    )
    parser.add_argument("firmware",
        help="Path to firmware binary (.bin)")
    parser.add_argument("--config", default="config.json",
        help="Config file path (default: config.json)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
        help=f"Payload bytes per chunk, excl. 7B header (default: {DEFAULT_CHUNK_SIZE})")
    parser.add_argument("--fport", type=int, default=DEFAULT_FPORT,
        help=f"LoRaWAN fPort (default: {DEFAULT_FPORT})")
    parser.add_argument("--poll-timeout", type=float, default=DEFAULT_POLL_TIMEOUT,
        help=f"Seconds to wait for queue to empty per chunk (default: {DEFAULT_POLL_TIMEOUT})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
        help=f"Retransmission attempts per chunk (default: {DEFAULT_RETRIES})")

    args   = parser.parse_args()
    config = load_config(args.config)
    ota    = ChirpStackOTA(config)

    ota.upload_firmware(
        firmware_path = args.firmware,
        chunk_size    = args.chunk_size,
        fport         = args.fport,
        poll_timeout  = args.poll_timeout,
        retries       = args.retries,
    )


def bootstrap(config_path: str = "config.json") -> ChirpStackOTA:
    return ChirpStackOTA(load_config(config_path))


if __name__ == "__main__":
    main()