/*******************************************************************************
 * LoRaWAN Packet Counter + OTA Firmware Update
 * Based on Heltec WiFi LoRa 32 V2
 *
 * OTA frame layout (fPort 10):
 *   Byte 0:    sequence number       (uint8,  wraps at 255)
 *   Byte 1-2:  total chunks          (uint16, big-endian)
 *   Byte 3-6:  total firmware bytes  (uint32, big-endian)
 *   Byte 7+:   firmware payload
 *******************************************************************************/
#define CFG_eu868 1
#define CFG_sx1276_radio 1
#define LMIC_USE_INTERRUPTS 0
#define DISABLE_PING
#define DISABLE_BEACONS

#include <Arduino.h>
#include <lmic.h>
#include <hal/hal.h>
#include <Wire.h>
#include "HT_SSD1306Wire.h"
#include <Update.h>       // ESP32 built-in OTA library

// ─── OLED ─────────────────────────────────────────────────────────────────────
SSD1306Wire display(0x3c, 500000, 4, 15, GEOMETRY_128_64, 16);

// ─── LoRaWAN Credentials ──────────────────────────────────────────────────────

// This EUI must be in little-endian format (LSB first)
// Your AppEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This should also be in little endian format (LSB first)
// Your DevEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM DEVEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This key should be in big endian format (MSB first)
// Your AppKey from ChirpStack: XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPKEY[16] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

void os_getArtEui(u1_t* buf) { memcpy_P(buf, APPEUI, 8); }
void os_getDevEui(u1_t* buf) { memcpy_P(buf, DEVEUI, 8); }
void os_getDevKey(u1_t* buf) { memcpy_P(buf, APPKEY, 16); }

// ─── Pin Mapping ──────────────────────────────────────────────────────────────
const lmic_pinmap lmic_pins = {
  .nss            = 18,
  .rxtx           = LMIC_UNUSED_PIN,
  .rst            = 14,
  .dio            = {26, 35, 34},
  .rxtx_rx_active = 0,
  .rssi_cal       = 8,
  .spi_freq       = 8000000,
};

// ─── Config ───────────────────────────────────────────────────────────────────
const unsigned TX_INTERVAL = 10;
#define PORT_UPLINK  2
#define PORT_OTA    10

// ─── Firmware Version ─────────────────────────────────────────────────────────
// Bump this string every flash so the OLED confirms the update landed.
#define FW_VERSION "v1.0.0"

// ─── Counters ─────────────────────────────────────────── ──────────────────────
uint32_t uplinkCount   = 0;
uint32_t downlinkCount = 0;

// ─── OTA State ────────────────────────────────────────────────────────────────
struct OtaState {
  bool     active         = false;
  uint16_t totalChunks    = 0;
  uint32_t totalBytes     = 0;   // passed to Update.begin()
  uint16_t receivedCount  = 0;
  uint8_t  lastSeq        = 0xFF;
  uint32_t startMs        = 0;
  bool     updateBegun    = false;
  bool     updateError    = false;
} ota;

// ─── State ────────────────────────────────────────────────────────────────────
static osjob_t sendjob;

// ─── Display Helper ───────────────────────────────────────────────────────────
void updateDisplay(String l1, String l2 = "", String l3 = "", String l4 = "") {
  display.clear();
  display.setFont(ArialMT_Plain_10);
  display.setTextAlignment(TEXT_ALIGN_LEFT);
  display.drawString(0,  0, l1);
  if (l2.length()) display.drawString(0, 16, l2);
  if (l3.length()) display.drawString(0, 32, l3);
  if (l4.length()) display.drawString(0, 48, l4);
  display.display();
}

void showStats() {
  updateDisplay(
    "Packet Counter " FW_VERSION,
    "TX (uplink):   " + String(uplinkCount),
    "RX (downlink): " + String(downlinkCount),
    "Interval: " + String(TX_INTERVAL) + "s"
  );
}

// ─── OTA Progress Display ─────────────────────────────────────────────────────
void showOtaProgress() {
  uint16_t pct     = (ota.totalChunks > 0)
                     ? (uint16_t)((uint32_t)ota.receivedCount * 100 / ota.totalChunks)
                     : 0;
  uint32_t elapsed = (millis() - ota.startMs) / 1000;

  const uint8_t BAR_LEN = 16;
  uint8_t filled = (ota.totalChunks > 0)
                   ? (uint8_t)((uint32_t)ota.receivedCount * BAR_LEN / ota.totalChunks)
                   : 0;
  String bar = "[";
  for (uint8_t i = 0; i < BAR_LEN; i++) bar += (i < filled) ? (char)0xDB : '-';
  bar += "]";

  updateDisplay(
    "OTA  " FW_VERSION,
    bar,
    String(ota.receivedCount) + "/" + String(ota.totalChunks) + "  (" + String(pct) + "%)",
    "Elapsed: " + String(elapsed) + "s"
  );
}

// ─── OTA Chunk Handler ────────────────────────────────────────────────────────
void processOtaChunk(uint8_t* data, uint8_t len) {
  // Header is 7 bytes minimum
  if (len < 8) {
    Serial.println(F("[OTA] Frame too short, ignoring"));
    return;
  }

  uint8_t  seq         =  data[0];
  uint16_t totalChunks = ((uint16_t)data[1] << 8) | data[2];
  uint32_t totalBytes  = ((uint32_t)data[3] << 24)
                       | ((uint32_t)data[4] << 16)
                       | ((uint32_t)data[5] <<  8)
                       |  (uint32_t)data[6];
  uint8_t* payload     = &data[7];
  uint8_t  payloadLen  = len - 7;

  // ── Duplicate detection ────────────────────────────────────────────────────
  if (ota.active && seq == ota.lastSeq) {
    Serial.printf("[OTA] Duplicate seq=%u, skipping\n", seq);
    return;
  }

  // ── First chunk — initialise OTA session and call Update.begin() ──────────
  if (!ota.active) {
    ota.active        = true;
    ota.totalChunks   = totalChunks;
    ota.totalBytes    = totalBytes;
    ota.receivedCount = 0;
    ota.lastSeq       = 0xFF;
    ota.startMs       = millis();
    ota.updateError   = false;

    Serial.printf("[OTA] Session start — %u chunks, %lu bytes total\n",
                  totalChunks, totalBytes);

    // Begin the ESP32 OTA update — allocates the OTA flash partition
    if (!Update.begin(totalBytes)) {
      Serial.printf("[OTA] Update.begin() FAILED: %s\n",
                    Update.errorString());
      ota.updateError = true;
      ota.active      = false;
      updateDisplay("OTA FAILED", "Update.begin() error", Update.errorString());
      return;
    }

    ota.updateBegun = true;
    Serial.println(F("[OTA] Update.begin() OK — flash partition ready"));

    updateDisplay(
      "OTA Starting!",
      "Chunks: " + String(totalChunks),
      String(totalBytes) + " bytes",
      FW_VERSION
    );
    delay(1000);
  }

  // ── Write payload to flash ─────────────────────────────────────────────────
  if (ota.updateBegun && !ota.updateError) {
    size_t written = Update.write(payload, payloadLen);
    if (written != payloadLen) {
      Serial.printf("[OTA] Update.write() FAILED at chunk %u: %s\n",
                    ota.receivedCount + 1, Update.errorString());
      ota.updateError = true;
      Update.abort();
      updateDisplay(
        "OTA WRITE ERROR",
        "Chunk: " + String(ota.receivedCount + 1),
        Update.errorString(),
        "Reboot to retry"
      );
      return;
    }
  }

  ota.lastSeq = seq;
  ota.receivedCount++;

  Serial.printf("[OTA] Chunk %u/%u  seq=%u  %uB written\n",
                ota.receivedCount, ota.totalChunks, seq, payloadLen);

  showOtaProgress();

  // ── Last chunk — finalise and reboot ──────────────────────────────────────
  if (ota.receivedCount >= ota.totalChunks) {
    uint32_t elapsed = (millis() - ota.startMs) / 1000;
    Serial.printf("[OTA] All chunks received in %lus — finalising...\n", elapsed);

    if (Update.end(true)) {
      Serial.println(F("[OTA] Update.end() OK — rebooting!"));
      updateDisplay(
        "OTA Complete!",
        "Chunks: " + String(ota.totalChunks),
        "Time:   " + String(elapsed) + "s",
        "Rebooting..."
      );
      delay(2000);
      ESP.restart();
    } else {
      Serial.printf("[OTA] Update.end() FAILED: %s\n", Update.errorString());
      updateDisplay(
        "OTA FINAL ERROR",
        Update.errorString(),
        "Reboot to retry"
      );
      ota = OtaState();   // reset so a retry is possible without reflash
    }
  }
}

// ─── Downlink Handler ─────────────────────────────────────────────────────────
void processDownlink(uint8_t port, uint8_t* data, uint8_t len) {
  downlinkCount++;

  Serial.println(F("─────────────────────────────"));
  Serial.printf("⬇  DOWNLINK #%lu | port %d | %d bytes\n", downlinkCount, port, len);
  Serial.print(F("   HEX: "));
  for (int i = 0; i < len; i++) Serial.printf("%02X ", data[i]);
  Serial.println();
  Serial.printf("   TX sent: %lu  |  RX recv: %lu\n", uplinkCount, downlinkCount);
  Serial.println(F("─────────────────────────────"));

  if (port == PORT_OTA) {
    processOtaChunk(data, len);
    return;
  }

  updateDisplay(
    "DOWNLINK RECEIVED!",
    "DL #" + String(downlinkCount) + "  port " + String(port),
    "TX: " + String(uplinkCount) + "  RX: " + String(downlinkCount),
    String(len) + " bytes"
  );
  delay(2000);
  showStats();
}

// ─── Forward declaration ──────────────────────────────────────────────────────
void do_send(osjob_t* j);

// ─── LMIC Event Handler ───────────────────────────────────────────────────────
void onEvent(ev_t ev) {
  switch (ev) {

    case EV_JOINING:
      Serial.println(F("EV_JOINING"));
      updateDisplay("Packet Counter", "Joining network...", "Please wait...", FW_VERSION);
      break;

    case EV_JOINED:
      Serial.println(F("EV_JOINED"));
      {
        u4_t netid = 0;
        devaddr_t devaddr = 0;
        u1_t nwkKey[16], artKey[16];
        LMIC_getSessionKeys(&netid, &devaddr, nwkKey, artKey);
        Serial.printf("DevAddr: 0x%08X\n", devaddr);
      }
      LMIC_setLinkCheckMode(0);
      updateDisplay("Packet Counter", "JOINED!", "Starting TX...", FW_VERSION);
      delay(1000);
      do_send(&sendjob);
      break;

    case EV_JOIN_FAILED:
      Serial.println(F("EV_JOIN_FAILED"));
      updateDisplay("Packet Counter", "JOIN FAILED", "Retrying...", FW_VERSION);
      break;

    case EV_TXSTART:
      Serial.printf("⬆  Transmitting uplink #%lu...\n", uplinkCount + 1);
      if (!ota.active) {
        updateDisplay(
          "Packet Counter",
          "Transmitting...",
          "TX #" + String(uplinkCount + 1),
          "RX: " + String(downlinkCount)
        );
      }
      break;

    case EV_TXCOMPLETE:
      Serial.println(F("EV_TXCOMPLETE"));
      Serial.printf("   txrxFlags: 0x%02X\n", LMIC.txrxFlags);
      Serial.printf("   dataLen:   %d\n",     LMIC.dataLen);
      Serial.printf("   dataBeg:   %d\n",     LMIC.dataBeg);

      if (LMIC.txrxFlags & TXRX_DNW1 || LMIC.txrxFlags & TXRX_DNW2) {
        Serial.println(F("   RX window opened"));
        if (LMIC.dataLen > 0) {
          if (LMIC.txrxFlags & TXRX_PORT) {
            uint8_t port = LMIC.frame[LMIC.dataBeg - 1];
            processDownlink(port, &LMIC.frame[LMIC.dataBeg], LMIC.dataLen);
          } else {
            Serial.println(F("   MAC-only frame, skipping"));
            if (!ota.active) showStats();
          }
        } else {
          Serial.println(F("   RX window opened but no payload"));
          if (!ota.active) showStats();
        }
      } else {
        Serial.println(F("   No RX window this cycle"));
        if (!ota.active) showStats();
      }

      os_setTimedCallback(&sendjob, os_getTime() + sec2osticks(TX_INTERVAL), do_send);
      break;

    case EV_JOIN_TXCOMPLETE:
      Serial.println(F("EV_JOIN_TXCOMPLETE: no JoinAccept"));
      updateDisplay("Packet Counter", "Join sent...", "Waiting for accept...", FW_VERSION);
      break;

    default:
      Serial.printf("Event: %u\n", (unsigned)ev);
      break;
  }
}

// ─── Send Uplink ──────────────────────────────────────────────────────────────
void do_send(osjob_t* j) {
  if (LMIC.opmode & OP_TXRXPEND) {
    Serial.println(F("TX pending, skipping"));
    return;
  }

  uplinkCount++;

  uint8_t payload[8];
  payload[0] = 'P';
  payload[1] = 'K';
  payload[2] = (uplinkCount   >> 8) & 0xFF;
  payload[3] =  uplinkCount         & 0xFF;
  payload[4] = (downlinkCount >> 8) & 0xFF;
  payload[5] =  downlinkCount       & 0xFF;
  payload[6] = 0x00;
  payload[7] = 0x00;

  LMIC_setTxData2(PORT_UPLINK, payload, sizeof(payload), 0);
  Serial.printf("⬆  Queued uplink #%lu\n", uplinkCount);
}

// ─── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(1000);

  display.init();
  display.flipScreenVertically();
  display.setFont(ArialMT_Plain_10);
  updateDisplay("Packet Counter", "Initializing...", "", FW_VERSION);

  Serial.println(F("======================================"));
  Serial.println(F("  LoRaWAN Uplink/Downlink Counter"));
  Serial.println(F("  Heltec WiFi LoRa 32 V2 - EU868"));
  Serial.println(F("  FW: " FW_VERSION));
  Serial.println(F("======================================"));

  os_init();
  LMIC_reset();
  LMIC_reset();
  LMIC_setClockError(MAX_CLOCK_ERROR * 10 / 100);  // ← add this
  // Force RX1 delay to match ChirpStack default (1 second)
  LMIC.rxDelay = 1;
  LMIC_startJoining();

  updateDisplay("Packet Counter", "OTAA Joining...", "", FW_VERSION);
}

// ─── Loop ─────────────────────────────────────────────────────────────────────
void loop() {
  os_runloop_once();
}
