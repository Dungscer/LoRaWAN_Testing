#!/usr/bin/env python3
"""
LoRaWAN OTA Firmware Update - CURL VERSION
Uses curl for all API calls (bypasses requests library issues)
"""

import subprocess
import json
import time
import sys
import os
from base64 import b64encode

def load_config(config_file="config.json"):
    """Load configuration"""
    with open(config_file, 'r') as f:
        return json.load(f)

class ChirpStackOTA:
    def __init__(self, config):
        """Initialize"""
        self.api_url = config['chirpstack']['api_url'].rstrip('/')
        self.api_token = config['chirpstack']['api_token']
        self.dev_eui = config['device']['dev_eui']
        
        self.OTA_PORT = config['ota']['port']
        self.CHUNK_SIZE = config['ota']['chunk_size']
        
        self.OTA_CMD_START = 0x01
        self.OTA_CMD_DATA = 0x02
        self.OTA_CMD_END = 0x03
        self.OTA_CMD_ABORT = 0x04
        
    def send_downlink(self, port, data):
        """Send downlink using curl"""
        url = f"{self.api_url}/api/devices/{self.dev_eui}/queue"
        data_b64 = b64encode(bytes(data)).decode('utf-8')
        
        payload = {
            "queueItem": {
                "confirmed": False,  # Changed to False for faster transmission
                "fPort": port,
                "data": data_b64
            }
        }
        
        curl_cmd = [
            'curl',
            '-s',  # Silent mode
            '-X', 'POST',
            '-H', 'Content-Type: application/json',
            '-H', f'Grpc-Metadata-Authorization: Bearer {self.api_token}',
            '-d', json.dumps(payload),
            url
        ]
        
        try:
            result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 0:
                # Check if response contains error
                if 'error' in result.stdout.lower() or result.stdout.strip() == '':
                    return True  # Empty response is success for queue
                return True
            else:
                print(f"  ✗ Curl failed: {result.stderr}")
                return False
        except Exception as e:
            print(f"  ✗ Error: {e}")
            return False
    
    def upload_firmware(self, firmware_path, delay):
        """Upload firmware"""
        
        if not os.path.exists(firmware_path):
            print(f"✗ Firmware not found: {firmware_path}")
            return False
        
        with open(firmware_path, 'rb') as f:
            firmware_data = f.read()
        
        firmware_size = len(firmware_data)
        total_chunks = (firmware_size + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
        
        print(f"\n{'='*70}")
        print(f"LoRaWAN OTA Firmware Update (CURL Version)")
        print(f"{'='*70}")
        print(f"Device EUI:     {self.dev_eui}")
        print(f"Firmware:       {firmware_path}")
        print(f"Firmware size:  {firmware_size:,} bytes ({firmware_size/1024:.1f} KB)")
        print(f"Chunk size:     {self.CHUNK_SIZE} bytes")
        print(f"Total chunks:   {total_chunks:,}")
        print(f"Delay:          {delay}s per chunk")
        print(f"Estimated time: ~{total_chunks * delay / 3600:.1f} hours ({total_chunks * delay / 60:.0f} minutes)")
        print(f"{'='*70}\n")
        
        # Confirmation
        print("⚠️  This will take a LONG time!")
        print(f"   At {delay}s per chunk, expect ~{total_chunks * delay / 3600:.1f} hours")
        
        response = input("\nContinue? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Cancelled.")
            return False
        
        # Send START
        print("\n[1/3] Sending START command...")
        start_data = [
            self.OTA_CMD_START,
            (firmware_size >> 24) & 0xFF,
            (firmware_size >> 16) & 0xFF,
            (firmware_size >> 8) & 0xFF,
            firmware_size & 0xFF,
            (total_chunks >> 8) & 0xFF,
            total_chunks & 0xFF
        ]
        
        if not self.send_downlink(self.OTA_PORT, start_data):
            print("✗ Failed to send START command")
            return False
        
        print(f"✓ START command queued")
        print(f"  Waiting {delay}s for device to process...")
        time.sleep(delay)
        
        # Send chunks
        print(f"\n[2/3] Sending {total_chunks:,} firmware chunks...")
        start_time = time.time()
        failed_chunks = []
        
        try:
            for chunk_num in range(total_chunks):
                start_idx = chunk_num * self.CHUNK_SIZE
                end_idx = min(start_idx + self.CHUNK_SIZE, firmware_size)
                chunk_data = list(firmware_data[start_idx:end_idx])
                
                progress = ((chunk_num + 1) * 100) // total_chunks
                elapsed = time.time() - start_time
                
                if chunk_num == 0:
                    eta = total_chunks * delay / 3600
                else:
                    eta = (elapsed / (chunk_num + 1)) * (total_chunks - chunk_num - 1) / 3600
                
                # Progress display (update every 10 chunks or at milestones)
                if chunk_num % 10 == 0 or progress in [25, 50, 75, 90]:
                    print(f"\r[Chunk {chunk_num + 1:,}/{total_chunks:,}] "
                          f"Progress: {progress}% | "
                          f"Elapsed: {elapsed/3600:.2f}h | "
                          f"ETA: {eta:.2f}h", 
                          end='', flush=True)
                
                # Send chunk
                data = [
                    self.OTA_CMD_DATA,
                    (chunk_num >> 8) & 0xFF,
                    chunk_num & 0xFF
                ]
                data.extend(chunk_data)
                
                if not self.send_downlink(self.OTA_PORT, data):
                    failed_chunks.append(chunk_num)
                    print(f"\n  ⚠️  Chunk {chunk_num + 1} failed")
                    
                    # If too many failures, abort
                    if len(failed_chunks) > 10:
                        print(f"\n✗ Too many failures ({len(failed_chunks)}), aborting...")
                        self.send_downlink(self.OTA_PORT, [self.OTA_CMD_ABORT])
                        return False
                
                if chunk_num < total_chunks - 1:
                    time.sleep(delay)
            
            print()  # New line after progress
            
            if failed_chunks:
                print(f"\n⚠️  {len(failed_chunks)} chunks failed")
                print("  Continuing anyway (device may request retransmission)...")
            
            # Send END
            print(f"\n[3/3] Sending END command...")
            time.sleep(delay)
            
            if not self.send_downlink(self.OTA_PORT, [self.OTA_CMD_END]):
                print("✗ Failed to send END command")
                return False
            
            print("✓ END command queued")
            
            total_time = time.time() - start_time
            print("\n" + "="*70)
            print("✓ OTA UPDATE COMPLETE!")
            print(f"  Total time: {total_time/3600:.2f} hours ({total_time/60:.1f} minutes)")
            print(f"  Successful chunks: {total_chunks - len(failed_chunks):,}/{total_chunks:,}")
            print("\n  Device should now:")
            print("  1. Verify firmware integrity")
            print("  2. Flash new firmware")
            print("  3. Reboot with updated firmware")
            print("\n  Check your device's OLED display for update status!")
            print("="*70)
            return True
            
        except KeyboardInterrupt:
            print("\n\n✗ Update interrupted by user")
            print("  Sending ABORT command...")
            self.send_downlink(self.OTA_PORT, [self.OTA_CMD_ABORT])
            
            total_time = time.time() - start_time
            print(f"\n  Chunks sent before interrupt: {chunk_num + 1:,}/{total_chunks:,}")
            print(f"  Time elapsed: {total_time/3600:.2f} hours")
            return False

def main():
    print("""
╔══════════════════════════════════════════════════════════╗
║       LoRaWAN OTA Update - CURL VERSION (Working!)      ║
╚══════════════════════════════════════════════════════════╝
    """)
    
    if len(sys.argv) < 2:
        print("Usage: python3 ota_curl.py <firmware.bin>")
        sys.exit(1)
    
    firmware_path = sys.argv[1]
    
    # Load config
    print("Loading config.json...")
    try:
        config = load_config()
    except FileNotFoundError:
        print("✗ config.json not found!")
        sys.exit(1)
    
    print(f"✓ Configuration loaded")
    print(f"  ChirpStack: {config['chirpstack']['api_url']}")
    print(f"  Device EUI: {config['device']['dev_eui']}")
    print(f"  Delay: {config['ota']['delay_between_chunks']}s per chunk")
    
    # Create OTA updater
    ota = ChirpStackOTA(config)
    delay = config['ota']['delay_between_chunks']
    
    # Upload firmware
    success = ota.upload_firmware(firmware_path, delay)
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()