"""
diagnose.py — standalone diagnostic for ChirpStack gRPC connectivity
Run this FIRST before the OTA tool to verify GetDevice and GetQueue work.

Usage:
    python3 diagnose.py
    python3 diagnose.py --config config.json --watch 60
"""

import json
import grpc
import time
import argparse
from chirpstack_api import api


def load_config(path="config.json"):
    with open(path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",  default="config.json")
    parser.add_argument("--watch",   type=int, default=60,
                        help="How many seconds to watch for uplinks (default 60)")
    args = parser.parse_args()

    cfg        = load_config(args.config)
    server     = cfg["chirpstack"]["device_api_url"]
    api_token  = cfg["chirpstack"]["api_token"]
    dev_eui    = cfg["device"]["dev_eui"]
    auth       = [("authorization", f"Bearer {api_token}")]

    print(f"\n=== ChirpStack gRPC Diagnostics ===")
    print(f"Server  : {server}")
    print(f"DevEUI  : {dev_eui}\n")

    channel = grpc.insecure_channel(server)
    client  = api.DeviceServiceStub(channel)

    # ── Test 1: GetDevice ──────────────────────────────────────
    print("── Test 1: GetDevice ──────────────────────────────")
    try:
        req = api.GetDeviceRequest()
        req.dev_eui = dev_eui
        resp = client.Get(req, metadata=auth)
        ts   = resp.device_info.last_seen_at
        last_seen = ts.seconds + ts.nanos / 1e9
        print(f"  OK — last_seen_at = {last_seen:.3f}  ({time.ctime(last_seen)})")
        print(f"  device name      = {resp.device.name}")
    except Exception as e:
        print(f"  FAILED: {e}")
        last_seen = 0.0

    # ── Test 2: GetQueue ───────────────────────────────────────
    print("\n── Test 2: GetQueue ───────────────────────────────")
    try:
        req = api.GetDeviceQueueItemsRequest()
        req.dev_eui = dev_eui
        resp = client.GetQueue(req, metadata=auth)
        items = resp.result
        print(f"  OK — queue depth = {len(items)}")
        for i, item in enumerate(items):
            print(f"    [{i}] fport={item.f_port}  confirmed={item.confirmed}  "
                  f"len={len(item.data)}B")
    except Exception as e:
        print(f"  FAILED: {e}")

    # ── Test 3: FlushQueue ─────────────────────────────────────
    print("\n── Test 3: FlushQueue ─────────────────────────────")
    try:
        req = api.FlushDeviceQueueRequest()
        req.dev_eui = dev_eui
        client.FlushQueue(req, metadata=auth)
        print(f"  OK — queue flushed")
    except Exception as e:
        print(f"  FAILED: {e}")

    # ── Test 4: Enqueue 1 test downlink ───────────────────────
    print("\n── Test 4: Enqueue test downlink (fPort 10) ───────")
    try:
        req = api.EnqueueDeviceQueueItemRequest()
        req.queue_item.confirmed = False
        req.queue_item.data      = bytes([0xDE, 0xAD, 0xBE, 0xEF])
        req.queue_item.dev_eui   = dev_eui
        req.queue_item.f_port    = 10
        resp = client.Enqueue(req, metadata=auth)
        print(f"  OK — downlink id = {resp.id}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # ── Test 5: Watch for uplinks + queue drain ────────────────
    print(f"\n── Test 5: Watch {args.watch}s for uplink + queue drain ──")
    print(f"  Waiting for node to uplink (TX_INTERVAL=10s)...")
    print(f"  [last_seen delta] [queue depth] [status]")

    deadline   = time.monotonic() + args.watch
    prev_seen  = last_seen
    delivered  = False

    while time.monotonic() < deadline:
        # Poll GetDevice
        try:
            req = api.GetDeviceRequest()
            req.dev_eui = dev_eui
            resp = client.Get(req, metadata=auth)
            ts   = resp.device_info.last_seen_at
            cur_seen = ts.seconds + ts.nanos / 1e9
        except Exception as e:
            print(f"  GetDevice error: {e}")
            cur_seen = prev_seen

        # Poll GetQueue
        try:
            req = api.GetDeviceQueueItemsRequest()
            req.dev_eui = dev_eui
            resp = client.GetQueue(req, metadata=auth)
            depth = len(resp.result)
        except Exception as e:
            print(f"  GetQueue error: {e}")
            depth = -1

        delta      = cur_seen - last_seen
        new_uplink = cur_seen > prev_seen + 0.5

        status = ""
        if new_uplink and depth == 0:
            status = "<<< DELIVERED — downlink received by node!"
            delivered = True
        elif new_uplink and depth > 0:
            status = "uplink seen but queue not empty (duty cycle skip?)"
        elif new_uplink and depth < 0:
            status = "uplink seen but GetQueue FAILED"
        elif depth == 0 and delta > 0:
            status = "queue empty (already drained before watch started?)"

        print(f"  delta={delta:6.1f}s  depth={depth:2}  {status}")

        if delivered:
            break

        prev_seen = cur_seen if new_uplink else prev_seen
        time.sleep(2)

    if not delivered:
        print(f"\n  RESULT: No delivery detected in {args.watch}s")
        print(f"  → If delta never increased: GetDevice last_seen_at is broken")
        print(f"  → If delta increased but depth stayed 1: ChirpStack not transmitting")
        print(f"  → If depth returned -1: GetQueue is failing")
    else:
        print(f"\n  RESULT: Delivery confirmed — OTA tool should work!")

    print()


if __name__ == "__main__":
    main()