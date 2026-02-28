/*******************************************************************************
 * LoRaWAN OTAA for Heltec WiFi LoRa 32 V2
 * With OLED Display and OTA Update Support
 * 
 * Features:
 * - LoRaWAN OTAA connection
 * - Firmware OTA updates via LoRaWAN downlink
 * - OLED status display
 * - Persistent storage for firmware chunks
 *******************************************************************************/

// Configure for EU868
#define CFG_eu868 1
#define CFG_sx1276_radio 1
#define DISABLE_PING
#define DISABLE_BEACONS

#include <Arduino.h>
#include <lmic.h>
#include <hal/hal.h>
#include <Wire.h>
#include "HT_SSD1306Wire.h"
#include <Update.h>
#include <Preferences.h>

// OLED Display
SSD1306Wire display(0x3c, 500000, 4, 15, GEOMETRY_128_64, 16);

// This EUI must be in little-endian format (LSB first)
// Your AppEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This should also be in little endian format (LSB first)
// Your DevEUI from ChirpStack: XXXXXXXXXXXXXXXX
static const u1_t PROGMEM DEVEUI[8] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

// This key should be in big endian format (MSB first)
// Your AppKey from ChirpStack: XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
static const u1_t PROGMEM APPKEY[16] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };

void os_getArtEui (u1_t* buf) { memcpy_P(buf, APPEUI, 8);}
void os_getDevEui (u1_t* buf) { memcpy_P(buf, DEVEUI, 8);}
void os_getDevKey (u1_t* buf) { memcpy_P(buf, APPKEY, 16);}

// Pin mapping for Heltec WiFi LoRa 32 V2
const lmic_pinmap lmic_pins = {
    .nss = 18,
    .rxtx = LMIC_UNUSED_PIN,
    .rst = 14,
    .dio = {26, 35, 34},
};

// Variables
static osjob_t sendjob;
const unsigned TX_INTERVAL = 10; // seconds between transmissions
int packetCount = 0;
bool joined = false;

// OTA Update variables
Preferences preferences;
bool otaInProgress = false;
uint32_t totalFirmwareSize = 0;
uint32_t receivedBytes = 0;
uint16_t currentChunk = 0;
uint16_t totalChunks = 0;

// Firmware version
#define FIRMWARE_VERSION "1.0.10"

// OTA Command codes
#define OTA_CMD_START    0x01  // Start OTA update
#define OTA_CMD_DATA     0x02  // Firmware data chunk
#define OTA_CMD_END      0x03  // End OTA update
#define OTA_CMD_ABORT    0x04  // Abort OTA update
#define OTA_CMD_REQUEST  0x05  // Request firmware info

// OTA Response codes
#define OTA_RESP_READY   0x10  // Device ready for update
#define OTA_RESP_ACK     0x11  // Chunk received OK
#define OTA_RESP_NACK    0x12  // Chunk error, resend
#define OTA_RESP_SUCCESS 0x13  // Update successful
#define OTA_RESP_FAIL    0x14  // Update failed
#define OTA_RESP_INFO    0x15  // Firmware info response

void updateDisplay(String line1, String line2 = "", String line3 = "", String line4 = "") {
    display.clear();
    display.setFont(ArialMT_Plain_10);
    display.setTextAlignment(TEXT_ALIGN_LEFT);
    display.drawString(0, 0, line1);
    if (line2.length() > 0) display.drawString(0, 16, line2);
    if (line3.length() > 0) display.drawString(0, 32, line3);
    if (line4.length() > 0) display.drawString(0, 48, line4);
    display.display();
}

void startOTA(uint32_t firmwareSize, uint16_t chunkCount) {
    Serial.println(F("Starting OTA Update..."));
    
    totalFirmwareSize = firmwareSize;
    totalChunks = chunkCount;
    currentChunk = 0;
    receivedBytes = 0;
    
    if (!Update.begin(firmwareSize)) {
        Serial.println(F("OTA Error: Not enough space"));
        otaInProgress = false;
        sendOTAResponse(OTA_RESP_FAIL);
        return;
    }
    
    otaInProgress = true;
    updateDisplay("OTA UPDATE", 
                 "Starting...",
                 "Size: " + String(firmwareSize) + " bytes",
                 "Chunks: " + String(chunkCount));
    
    sendOTAResponse(OTA_RESP_READY);
}

void processOTAChunk(uint16_t chunkNum, uint8_t* data, uint8_t len) {
    if (!otaInProgress) {
        Serial.println(F("OTA Error: Not in progress"));
        return;
    }
    
    // Check if this is the expected chunk
    if (chunkNum != currentChunk) {
        Serial.printf("OTA Error: Expected chunk %d, got %d\n", currentChunk, chunkNum);
        sendOTAResponse(OTA_RESP_NACK);
        return;
    }
    
    // Write chunk to flash
    if (Update.write(data, len) != len) {
        Serial.println(F("OTA Error: Write failed"));
        Update.abort();
        otaInProgress = false;
        sendOTAResponse(OTA_RESP_FAIL);
        return;
    }
    
    receivedBytes += len;
    currentChunk++;
    
    // Update display
    int progress = (receivedBytes * 100) / totalFirmwareSize;
    updateDisplay("OTA UPDATE", 
                 "Progress: " + String(progress) + "%",
                 "Chunk: " + String(currentChunk) + "/" + String(totalChunks),
                 String(receivedBytes) + "/" + String(totalFirmwareSize));
    
    Serial.printf("Chunk %d/%d received (%d%%)\n", currentChunk, totalChunks, progress);
    
    sendOTAResponse(OTA_RESP_ACK);
}

void endOTA() {
    if (!otaInProgress) {
        return;
    }
    
    if (Update.end(true)) {
        Serial.println(F("OTA Update Success!"));
        updateDisplay("OTA UPDATE", 
                     "SUCCESS!",
                     "Rebooting in 5s...");
        sendOTAResponse(OTA_RESP_SUCCESS);
        delay(5000);
        ESP.restart();
    } else {
        Serial.println(F("OTA Update Failed!"));
        updateDisplay("OTA UPDATE", "FAILED!", Update.errorString());
        sendOTAResponse(OTA_RESP_FAIL);
        otaInProgress = false;
    }
}

void abortOTA() {
    if (otaInProgress) {
        Update.abort();
        otaInProgress = false;
        Serial.println(F("OTA Update Aborted"));
        updateDisplay("OTA UPDATE", "ABORTED");
    }
}

void sendOTAResponse(uint8_t responseCode) {
    uint8_t response[4];
    response[0] = responseCode;
    response[1] = (currentChunk >> 8) & 0xFF;
    response[2] = currentChunk & 0xFF;
    response[3] = 0x00; // Reserved
    
    if (LMIC.opmode & OP_TXRXPEND) {
        Serial.println(F("Cannot send OTA response: TX pending"));
    } else {
        LMIC_setTxData2(1, response, sizeof(response), 0); // Port 1 for OTA responses
    }
}

void sendFirmwareInfo() {
    // Send current firmware version and device status
    uint8_t info[20];
    info[0] = OTA_RESP_INFO;
    
    // Version string (max 10 chars)
    String version = FIRMWARE_VERSION;
    for (int i = 0; i < 10; i++) {
        info[i + 1] = (i < version.length()) ? version.charAt(i) : 0;
    }
    
    // Free heap space
    uint32_t freeHeap = ESP.getFreeHeap();
    info[11] = (freeHeap >> 24) & 0xFF;
    info[12] = (freeHeap >> 16) & 0xFF;
    info[13] = (freeHeap >> 8) & 0xFF;
    info[14] = freeHeap & 0xFF;
    
    // Max update size (sketch size)
    uint32_t maxSize = ESP.getFreeSketchSpace();
    info[15] = (maxSize >> 24) & 0xFF;
    info[16] = (maxSize >> 16) & 0xFF;
    info[17] = (maxSize >> 8) & 0xFF;
    info[18] = maxSize & 0xFF;
    
    info[19] = 0x00; // Reserved
    
    if (!(LMIC.opmode & OP_TXRXPEND)) {
        LMIC_setTxData2(1, info, sizeof(info), 0);
    }
}

void processDownlink(uint8_t port, uint8_t* data, uint8_t len) {
    Serial.printf("Downlink on port %d, length %d\n", port, len);
    
    if (port == 1) { // OTA command port
        if (len < 1) return;
        
        uint8_t cmd = data[0];
        
        switch (cmd) {
            case OTA_CMD_START:
                if (len >= 7) {
                    uint32_t size = (data[1] << 24) | (data[2] << 16) | (data[3] << 8) | data[4];
                    uint16_t chunks = (data[5] << 8) | data[6];
                    startOTA(size, chunks);
                }
                break;
                
            case OTA_CMD_DATA:
                if (len >= 3) {
                    uint16_t chunkNum = (data[1] << 8) | data[2];
                    processOTAChunk(chunkNum, &data[3], len - 3);
                }
                break;
                
            case OTA_CMD_END:
                endOTA();
                break;
                
            case OTA_CMD_ABORT:
                abortOTA();
                break;
                
            case OTA_CMD_REQUEST:
                sendFirmwareInfo();
                break;
                
            default:
                Serial.printf("Unknown OTA command: 0x%02X\n", cmd);
                break;
        }
    }
}

void onEvent (ev_t ev) {
    Serial.print(os_getTime());
    Serial.print(": ");
    
    switch(ev) {
        case EV_JOINING:
            Serial.println(F("EV_JOINING"));
            updateDisplay("LoRaWAN OTAA", "Status: JOINING", "Waiting for accept...");
            break;
            
        case EV_JOINED:
            Serial.println(F("EV_JOINED - Successfully joined!"));
            {
              u4_t netid = 0;
              devaddr_t devaddr = 0;
              u1_t nwkKey[16];
              u1_t artKey[16];
              LMIC_getSessionKeys(&netid, &devaddr, nwkKey, artKey);
              Serial.print("DevAddr: ");
              Serial.println(devaddr, HEX);
              
              updateDisplay("LoRaWAN OTAA", 
                           "Status: JOINED!", 
                           "FW: " + String(FIRMWARE_VERSION),
                           "DevAddr: 0x" + String(devaddr, HEX));
            }
            LMIC_setLinkCheckMode(0);
            joined = true;
            
            // Send first packet immediately after join
            do_send(&sendjob);
            break;
            
        case EV_JOIN_FAILED:
            Serial.println(F("EV_JOIN_FAILED"));
            updateDisplay("LoRaWAN OTAA", "Status: JOIN FAILED", "Retrying...");
            break;
            
        case EV_TXCOMPLETE:
            Serial.println(F("EV_TXCOMPLETE"));
            
            // Check for downlink data
            if (LMIC.dataLen) {
                Serial.printf("Received %d bytes on port %d\n", LMIC.dataLen, LMIC.frame[LMIC.dataBeg - 1]);
                processDownlink(LMIC.frame[LMIC.dataBeg - 1], &LMIC.frame[LMIC.dataBeg], LMIC.dataLen);
            }
            
            if (!otaInProgress) {
                if (LMIC.txrxFlags & TXRX_ACK) {
                    Serial.println(F("Received ACK"));
                    updateDisplay("LoRaWAN OTAA", 
                                 "Status: ACTIVE",
                                 "FW: " + String(FIRMWARE_VERSION),
                                 "Packets: " + String(packetCount));
                } else {
                    updateDisplay("LoRaWAN OTAA", 
                                 "Status: ACTIVE",
                                 "FW: " + String(FIRMWARE_VERSION),
                                 "Packets: " + String(packetCount));
                }
                
                // Schedule next transmission
                os_setTimedCallback(&sendjob, os_getTime()+sec2osticks(TX_INTERVAL), do_send);
            }
            break;
            
        case EV_TXSTART:
            Serial.println(F("EV_TXSTART"));
            if (!otaInProgress) {
                updateDisplay("LoRaWAN OTAA", 
                             "Status: ACTIVE",
                             "FW: " + String(FIRMWARE_VERSION),
                             "Transmitting...");
            }
            break;
            
        case EV_JOIN_TXCOMPLETE:
            Serial.println(F("EV_JOIN_TXCOMPLETE: no JoinAccept"));
            updateDisplay("LoRaWAN OTAA", "Join request sent", "Waiting for accept...");
            break;
            
        default:
            Serial.print(F("Event: "));
            Serial.println((unsigned) ev);
            break;
    }
}

void do_send(osjob_t* j){
    if (otaInProgress) {
        Serial.println(F("OTA in progress, skipping normal transmission"));
        return;
    }
    
    // Check if there is not a current TX/RX job running
    if (LMIC.opmode & OP_TXRXPEND) {
        Serial.println(F("OP_TXRXPEND, not sending"));
    } else {
        // Prepare data - send packet counter and firmware version
        packetCount++;
        uint8_t mydata[10];
        mydata[0] = 'T';
        mydata[1] = 'X';
        mydata[2] = (packetCount >> 8) & 0xFF;
        mydata[3] = packetCount & 0xFF;
        
        // Add firmware version
        String version = FIRMWARE_VERSION;
        for (int i = 0; i < 6 && i < version.length(); i++) {
            mydata[4 + i] = version.charAt(i);
        }
        
        LMIC_setTxData2(2, mydata, 10, 0); // Port 2 for normal data
        Serial.print(F("Packet queued: "));
        Serial.println(packetCount);
    }
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    
    // Initialize OLED
    display.init();
    display.flipScreenVertically();
    display.setFont(ArialMT_Plain_10);
    
    updateDisplay("LoRaWAN OTAA", 
                 "FW: " + String(FIRMWARE_VERSION),
                 "Initializing...");
    
    Serial.println();
    Serial.println(F("======================================"));
    Serial.println(F("LoRaWAN OTAA with OTA Update"));
    Serial.println("Firmware Version: " + String(FIRMWARE_VERSION));
    Serial.println(F("Using MCCI LMIC Library"));
    Serial.println(F("======================================"));
    
    // Initialize preferences
    preferences.begin("lora-ota", false);
    
    // LMIC init
    os_init();
    LMIC_reset();

    // Start join procedure (OTAA)
    LMIC_startJoining();
    
    updateDisplay("LoRaWAN OTAA", 
                 "FW: " + String(FIRMWARE_VERSION),
                 "Starting join...");
    Serial.println(F("Starting OTAA join..."));
}

void loop() {
    os_runloop_once();
}
