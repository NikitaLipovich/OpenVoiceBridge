# lyrat_resend — ESP32 LyraT Phone Stream

## Назначение

Устройство прикидывается Bluetooth-гарнитурой (HFP Hands-Free Unit) для Android телефона.
При входящем звонке ESP32 захватывает SCO-аудио (8 кГц, CVSD) и одновременно
транслирует голос по UDP через WiFi на приёмную сторону (ПК / сервер).

Это замена предыдущей схемы на ESP32-S3 + внешний модуль FSC-BT1036x.

---

## Аппаратная платформа

| Компонент | Значение |
|---|---|
| Плата | ESP32-LyraT v4.3 |
| MCU | ESP32-WROVER-IE (ESP32 dual-core Xtensa LX6) |
| PSRAM | 8 MB (quad SPI) |
| Flash | 4 MB |
| Bluetooth | ESP32 Classic BT (BR/EDR) — встроенный |
| WiFi | 802.11 b/g/n (shared antenna с BT) |
| Аудио кодек | ES8388 (встроен на плате LyraT) |
| Микрофон | Внешняя петличка → LINE_IN ES8388 (L+R) |
| Наушники | HPout ES8388 (для мониторинга / call audio) |

**Антенна**: BT и WiFi делят одну 2.4 ГГц антенну (IPEX) — требует SW coexistence.

---

## Ключевые профили Bluetooth

| Профиль | Роль ESP32 | Назначение |
|---|---|---|
| HFP (Hands-Free Profile) | HF Unit (гарнитура) | Управление звонками + SCO аудио |
| A2DP (Advanced Audio Distribution) | Sink | Приём стерео музыки (опционально) |

Android выступает как **AG (Audio Gateway)** и автоматически маршрутизирует SCO на HFP-устройство при звонке — без участия пользователя.

---

## Аудио поток данных

```
Android (AG) ──SCO CVSD 8kHz──► ESP32 BT HFP (HF Unit)
                                        │
                              PCM GPIO pins (CLK/DOUT/DIN/SYNC)
                                        │
                                    ES8388 кодек
                                        │
                        ┌───────────────┴───────────────┐
                    LINE_IN                           HPout
                  (петличка)                    (наушники)
                        │
                   DSP pipeline
                   (HPF + Notch + Wiener + Gate + AEC + Limiter)
                        │
                   UDP socket (5004/5005)
                        │
                    WiFi ──────────────────────────────► ПК/сервер
```

---

## Требования к окружению

- **ESP-IDF**: v5.3 или новее (v5.5.2 используется в проекте)
- **ESP-ADF**: не требуется (plain IDF проект, ES8388 инициализируется напрямую)
- Python 3.8+, CMake 3.16+

---

## Документация проекта

| Файл | Содержание |
|---|---|
| `docs/PROJECT_OVERVIEW.md` | Этот файл — общее описание |
| `docs/SETUP.md` | Сборка, прошивка, первый запуск |
| `docs/HARDWARE.md` | Пины, схема подключения, ES8388, PCM |
| `docs/BT_HFP.md` | Bluetooth стек, HFP, SCO, coexistence |
| `docs/AUDIO_PIPELINE.md` | DSP фильтры, кодек ES8388, I2S |
| `docs/WIFI_UDP.md` | WiFi, UDP стриминг, coexistence |
| `docs/KNOWN_ISSUES.md` | Известные проблемы и решения |
