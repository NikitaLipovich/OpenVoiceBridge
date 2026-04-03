# Сборка и прошивка

## Требования

- ESP-IDF v5.3+ (тестируется на v5.5.2)
- `IDF_PATH` установлен и `idf.py` доступен в PATH
- Плата ESP32-LyraT подключена по USB

---

## Первый запуск (чистый проект)

```bash
cd C:\Users\Admin\workspace\phone_stream\lyra\lyrat_resend

# 1. Установить target (читает sdkconfig.defaults + sdkconfig.defaults.esp32)
idf.py set-target esp32

# 2. Проверить конфигурацию (опционально)
idf.py menuconfig

# 3. Собрать
idf.py build

# 4. Прошить (укажи правильный COM-порт)
idf.py -p COM3 flash

# 5. Монитор
idf.py -p COM3 monitor
```

---

## Пересборка после изменений в sdkconfig.defaults

Если изменил `sdkconfig.defaults` или `sdkconfig.defaults.esp32`:

```bash
# Полная пересборка с новым sdkconfig
idf.py fullclean
idf.py set-target esp32
idf.py build
```

> `idf.py set-target` всегда пересоздаёт `sdkconfig` из defaults-файлов.
> Старый `sdkconfig` при этом удаляется автоматически.

---

## Порядок применения defaults-файлов

```
sdkconfig.defaults          ← общие настройки (BT, HFP, partition table)
sdkconfig.defaults.esp32    ← ESP32-специфичные (PSRAM, flash mode, CPU freq)
```

Файлы применяются именно в этом порядке. `sdkconfig.defaults.esp32` перекрывает
общие настройки из `sdkconfig.defaults`.

---

## Проверка Bluetooth после прошивки

1. Включить LyraT
2. На Android: Настройки → Bluetooth → Поиск устройств
3. Должно появиться устройство `ESP_HFP_HF` (или имя заданное в коде)
4. Сопрячь — Android запросит PIN (по умолчанию 0000 или SSP)
5. Позвонить на телефон — SCO аудио должно переключиться на LyraT

---

## Полезные команды

```bash
# Размер бинарника по секциям
idf.py size

# Только прошить без пересборки
idf.py -p COM3 flash --no-stub

# Прошить конкретный раздел
idf.py -p COM3 partition-table-flash

# Стереть всю flash (NVS + app)
idf.py -p COM3 erase-flash
```

---

## Типичные ошибки при первой сборке

| Ошибка | Причина | Решение |
|---|---|---|
| `CONFIG_BT_ENABLED not set` | sdkconfig не из defaults | `idf.py fullclean && idf.py set-target esp32` |
| `Partition too large` | flash = 2MB в sdkconfig | Удалить sdkconfig, пересоздать |
| `undefined reference to esp_hfp_*` | HFP не включён в menuconfig | Проверить BT → HFP → Client в menuconfig |
| `PSRAM not found` | Плата не WROVER | Отключить `CONFIG_SPIRAM` в menuconfig |
