# WiFi и UDP стриминг

## Архитектура

```
ESP32 LyraT
    │
    │  raw lwIP UDP (порт 5005 → микрофон)
    │  raw lwIP UDP (порт 5004 → call audio от телефона)
    │
  WiFi 802.11 b/g/n
    │
  ПК / сервер
    │
  UDP listener (Python / C)
    │
  Аудио воспроизведение / запись
```

Протокол полностью аналогичен схеме ESP32-S3 + FSC-BT1036x.
Код UDP логики переносится без изменений, только инициализация WiFi.

---

## Конфигурация WiFi

```c
// Инициализация WiFi STA (клиент)
void wifi_init_sta(const char *ssid, const char *pass) {
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid = "...",
            .password = "...",
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    esp_wifi_start();
}
```

---

## UDP сокеты (raw lwIP)

```c
// Передача аудио данных по UDP (порт 5005)
static int udp_sock = -1;
static struct sockaddr_in dest_addr;

void udp_init(const char *host_ip) {
    udp_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);

    dest_addr.sin_family = AF_INET;
    dest_addr.sin_port = htons(5005);
    inet_aton(host_ip, &dest_addr.sin_addr);
}

void udp_send(const int16_t *pcm, int samples) {
    sendto(udp_sock, pcm, samples * sizeof(int16_t), 0,
           (struct sockaddr *)&dest_addr, sizeof(dest_addr));
}
```

Размер UDP пакета: `480 семплов × 2 байта = 960 байт` (один DMA буфер).
При 8kHz это **60ms аудио** на пакет → 16.7 пакетов/сек.

---

## BT + WiFi Coexistence

### Проблема

ESP32 использует одну антенну для BT и WiFi. При активном SCO звонке:
- BT SCO требует гарантированный слот каждые **7.5ms** (Classic BT timing)
- WiFi пакеты конкурируют за радиоканал
- Без coexistence: потери UDP до 5-10%, jitter до 50ms

### Решение

```c
// В начале main или при установке SCO соединения:
#include "esp_coexist.h"

// Приоритет BT при звонке
esp_coex_preference_set(ESP_COEX_PREFER_BT);

// Вернуть баланс когда SCO завершён
esp_coex_preference_set(ESP_COEX_PREFER_BALANCE);
```

Из sdkconfig.defaults:
```
CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y
```

### Снижение мощности WiFi (при RF помехах)

```c
// 44 = 11 dBm (по умолчанию ~78 = 19.5 dBm)
// Снижает помехи BT↔WiFi при передаче на небольшие расстояния
esp_wifi_set_max_tx_power(44);
```

Применять только если jitter неприемлем и ПК находится рядом с ESP32.

### Ожидаемые характеристики при активном SCO

| Метрика | Ожидаемое значение |
|---|---|
| UDP потери пакетов | < 1% |
| Jitter | < 20ms с SW coexist |
| SCO качество | Без артефактов |

---

## Порты и протокол

| Порт | Направление | Содержимое |
|---|---|---|
| 5005 | ESP32 → ПК | Микрофон (петличка после DSP), 16bit LE, 8kHz |
| 5004 | ESP32 → ПК | Call audio (от телефона через SCO), 16bit LE, 8kHz |

Формат: raw PCM без заголовков. На приёмной стороне (Python):
```python
import socket, pyaudio
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5005))
p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt16, channels=1, rate=8000, output=True)
while True:
    data, _ = sock.recvfrom(4096)
    stream.write(data)
```

---

## Jitter буферизация на приёмной стороне

При BT SCO активен jitter UDP увеличивается. Рекомендуется:
- Буфер на ПК: **100-200ms** (накопить 2-3 пакета перед воспроизведением)
- При потере пакета: вставлять тишину (comfort noise)

```python
import collections, threading, time

class JitterBuffer:
    def __init__(self, size_ms=150, pkt_ms=60):
        self.buf = collections.deque()
        self.size = size_ms // pkt_ms  # пакетов в буфере
    
    def push(self, pkt):
        self.buf.append(pkt)
    
    def pop(self):
        if len(self.buf) >= self.size:
            return self.buf.popleft()
        return b'\x00' * 960  # тишина
```
