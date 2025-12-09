# OpenVoiceBridge: Bluetooth Audio Bridge

An open hardware–software platform that gives end users controlled, transparent access to their own call audio for real-time translation, captioning, accessibility, language learning, and AI-assisted communication. The system does not modify smartphones or mobile OS internals and operates strictly within standard Bluetooth profiles.

## Executive Summary
- Mobile operating systems intentionally limit API access to in-call audio, which slows down innovation in real-time translation, captioning, accessibility, and AI-driven communication tools.
- OpenVoiceBridge combines two standard Bluetooth roles (HF and AG) inside one device, forming a transparent, user-controlled audio relay between the phone and the headset.
- The MCU routes SCO audio and mirrors the user’s own stream to optional on-device or cloud STT/LLM pipelines—without altering call content and without introducing any new participants into the communication.
- All hardware, firmware, documentation, and legal guidance in this repository are fully open for audit and research.

## Project Goals
1. Provide users controlled access to their own call audio without modifying smartphones.
2. Enable real-time translation, AR subtitles, hearing assistance, language learning, and AI call coaching.
3. Address supplier and regulator requirements through a transparent design and a built-in optional Notification Mode.
4. Deliver a complete reference implementation of a standards-compliant hardware HFP bridge.

## Problem Statement
| Constraint | Consequence |
|------------|-------------|
| Android/iOS restrict programmatic access to call audio | No in-call translation, captioning, or AI assistants |
| No commercial HF↔AG bridge exists | Users cannot process their own speech in real time |
| BT vendors do not expose dual-role stacks | Builders must assemble their own hardware |
| Regulatory expectations are unclear | Component sourcing may face unnecessary delays |

## Solution Overview
OpenVoiceBridge uses two dedicated Bluetooth modules:

- Module A (HF role) paired with the phone
- Module B (AG role) paired with the headset

The MCU transparently routes audio between them and mirrors the user’s own audio stream to user-selected processing apps.

```
Phone (HFP) ── BT Module A (HF) ──┐
                                  │ PCM/I2S
                                  ▼
                            MCU Router ── UDP/USB/BLE ( Android ) ── STT/LLM/AR
                                  ▲
                                  │ PCM/I2S
Headset ── BT Module B (AG) ──────┘
```

## Key Capabilities
- Transparent HF↔AG audio relay with <40 ms one-way latency.
- SCO/eSCO routing with CVSD/mSBC codec support.
- Optional audio mirroring to local buffers, USB Audio Gadget, Wi-Fi/UDP, or BLE channels.
- Plugins for Whisper, Azure Cognitive Services, Google Speech-to-Text, ElevenLabs, and local LLM stacks.
- Notification Mode: automatic spoken disclosure for two-party-consent markets.
- Fully open schematics, firmware, and compliance templates.

## ⚖️ Disclaimer
This project exists solely to process a user’s own audio for translation, captioning, accessibility, education, and AI assistance.

It must not be used for:

- covert recording of others,
- unauthorized capture of third-party communications,
- violating privacy regulations,
- any surveillance purpose.

Usage is subject to local law. Operators must comply with applicable one-party/two-party consent rules, GDPR/CCPA/PIPL obligations, and telecom regulations. Contributors do not control end-user behavior and do not provide legal advice.

For two-party-consent jurisdictions (Germany, Switzerland, certain US states), Notification Mode enforces required spoken disclosure.

## For Regulators and Suppliers
### What the Device Does
1. Receives audio already available to the user via standard HFP/HSP.
2. Relays that same stream to a headset without altering content.
3. Mirrors the user’s own audio into optional STT/translation apps acting as data processors.
4. Publishes schematics and firmware for independent audit and certification.

### What the Device Does Not Do
- does not pair with other people’s phones;
- does not operate without the phone owner's participation;
- does not inject or modify communication content;
- does not interact with carrier networks;
- does not store data unless explicitly enabled.

### Compliance Posture
- Uses only public Bluetooth HFP/HSP profiles.
- Speech processing follows the data processor model (GDPR/CCPA/PIPL compliant).
- Notification Mode supports two-party consent compliance.
- Functionally comparable to hearing aids, call-center captioning, and personal accessibility tools.

## Architecture
### Hardware Layer
| Component | Role | Requirements |
|-----------|------|--------------|
| BT Module A | HF | SCO/eSCO, CVSD/mSBC |
| BT Module B | AG | HFP control + bi-directional audio |
| MCU | Router | Dual PCM/I2S, DMA, USB/Wi-Fi |

Suggested platforms: CSR8675, QCC512x, AB5301A; ESP32-S3, STM32H743, RP2040.

## Legal Model
- **One-party consent:** Israel, China, UK, France, India, most US states, etc.
- **Two-party consent:** Germany, Switzerland, California, Pennsylvania, etc.
- **STT and LLM services** act as data processors, not conversation participants.

## Use Cases
1. Real-time call translation for mobile and AR glasses.
2. Captions and accessibility support.
3. AI call assistance and coaching.
4. Language learning from real conversations.
5. AR/HUD teleprompter overlays.
6. Personal call recording where permitted.

## Roadmap
| Version | Milestones |
|---------|------------|
| 0.1 | Baseline HF↔AG schematic, PCM bridge |
| 0.2 | MCU router, USB Audio Gadget, Whisper PoC |
| 0.3 | Wi-Fi API, AR rendering, Notification Mode |
| 1.0 | Complete reference design and prototype |
