# Аудио pipeline — ES8388 + DSP

## Общая схема

```
LINE_IN (петличка L+R)
        │
   ES8388 АЦП (8kHz, 16bit)
        │
   I2S RX (DMA)
        │
   ┌────▼────────────────────────────────────────────┐
   │  DSP Pipeline (порт с ESP32-S3 проекта)         │
   │                                                  │
   │  1. L+R averaging (моно из стерео петлички)     │
   │  2. HPF 80 Hz (убираем гул, вибрации)           │
   │  3. Notch 50/150/250 Hz (помеха сети)           │
   │  4. Wiener noise suppression                    │
   │  5. Hysteresis noise gate                       │
   │  6. AEC (акустическая эхо-компенсация)          │
   │  7. Soft limiter (антиклип)                     │
   └────┬────────────────────────────────────────────┘
        │
   ┌────▼────────────────┐    ┌──────────────────────────────────┐
   │  UDP TX (WiFi)      │    │  SCO TX (BT HCI outgoing_cb)     │
   │  порт 5005 → ПК     │    │  bt_hf_client_outgoing_cb()      │
   └─────────────────────┘    └──────────────────────────────────┘
```

---

## Инициализация ES8388

ES8388 не поддерживается напрямую в ESP-IDF (это ESP-ADF компонент).
Нужно инициализировать через I2C напрямую.

### Минимальная инициализация I2C:

```c
#define ES8388_ADDR     0x10
#define I2C_MASTER_SCL  23
#define I2C_MASTER_SDA  18
#define I2C_MASTER_NUM  I2C_NUM_0
#define I2C_FREQ_HZ     100000

static void es8388_write_reg(uint8_t reg, uint8_t val) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (ES8388_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_write_byte(cmd, val, true);
    i2c_master_stop(cmd);
    i2c_master_cmd_begin(I2C_MASTER_NUM, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
}
```

### Ключевые регистры ES8388 для HFP (8kHz, LINE_IN):

```c
// Последовательность инициализации ES8388
es8388_write_reg(0x00, 0x80);  // Chip reset
vTaskDelay(pdMS_TO_TICKS(10));
es8388_write_reg(0x00, 0x00);  // Normal operation

// Chip control
es8388_write_reg(0x01, 0x58);  // VMIDSEL=2 (normal), ENREF
es8388_write_reg(0x02, 0xF3);  // Power up DAC L/R, ADC L/R

// ADC (микрофон/LINE_IN)
es8388_write_reg(0x09, 0x00);  // ADC L/R: LINPUT1/RINPUT1 (LINE_IN)
es8388_write_reg(0x0A, 0x00);  // PGA gain = 0dB (настроить по снр)
es8388_write_reg(0x0B, 0x02);  // ADC control: HPF enable

// Sampling rate: 8kHz при MCLK=256*fs
es8388_write_reg(0x08, 0x80);  // Single speed, MCLK/1

// DAC (наушники — для call audio от телефона через SCO)
es8388_write_reg(0x17, 0x18);  // DAC 16bit I2S
es8388_write_reg(0x1A, 0x00);  // DAC volume = 0dB L
es8388_write_reg(0x1B, 0x00);  // DAC volume = 0dB R

// Mixer: DAC → LOUT/ROUT и HPOUT
es8388_write_reg(0x26, 0x12);  // LOUT1 volume ~0dB
es8388_write_reg(0x27, 0x12);  // ROUT1 volume ~0dB
es8388_write_reg(0x28, 0x12);  // LOUT2 (HP) volume
es8388_write_reg(0x29, 0x12);  // ROUT2 (HP) volume

// Выходы ON
es8388_write_reg(0x04, 0x3C);  // Enable LOUT1, ROUT1, LOUT2, ROUT2
```

> Регистры ES8388 из даташита v2.0. Настройки усиления LINE_IN аналогичны
> WM8960 из предыдущей схемы — но регистры другие.

---

## Конфигурация I2S

```c
// I2S для ES8388 (аудио данные)
i2s_config_t i2s_cfg = {
    .mode = I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX,
    .sample_rate = 8000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .dma_buf_count = 8,
    .dma_buf_len = 480,        // 60ms при 8kHz = 480 семплов (как на S3)
    .use_apll = true,          // Точная частота APLL для аудио
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
};

i2s_pin_config_t i2s_pins = {
    .bck_io_num   = 5,   // BCLK
    .ws_io_num    = 25,  // LRCLK
    .data_out_num = 26,  // DO → ES8388 DIN
    .data_in_num  = 35,  // DI ← ES8388 DOUT
};

i2s_driver_install(I2S_NUM_0, &i2s_cfg, 0, NULL);
i2s_set_pin(I2S_NUM_0, &i2s_pins);
```

> `dma_buf_len=480` (60ms) — проверено на ESP32-S3, 0 DMA underrun.
> При 8kHz: 480 семплов = 60ms. Задержка pipeline = 60ms * 8 буферов / 2 = ~240ms.

---

## DSP фильтры (порт с ESP32-S3)

Весь DSP код переносится без изменений — только кодек другой.

### 1. L+R averaging (петличка стерео → моно)
```c
// Петличка подключена на оба канала I2S
for (int i = 0; i < N; i++) {
    mono[i] = (left[i] / 2) + (right[i] / 2);
}
```

### 2. HPF 80 Hz — убирает гул и вибрации
IIR фильтр первого порядка, rc = 1/(2π×80) ≈ 2ms.
```c
// α = RC/(RC + dt), dt = 1/8000
float alpha = 0.9375f;  // HPF 80Hz @ 8kHz
static float prev_in = 0, prev_out = 0;
for (int i = 0; i < N; i++) {
    float out = alpha * (prev_out + x[i] - prev_in);
    prev_in = x[i]; prev_out = out;
    y[i] = (int16_t)out;
}
```

### 3. Notch 50/150/250 Hz
Убирает помеху 50 Hz (сеть) и её гармоники.
IIR notch: `H(z) = (1 - 2cos(ω₀)z⁻¹ + z⁻²) / (1 - 2r·cos(ω₀)z⁻¹ + r²z⁻²)`, r=0.95.

### 4. Wiener noise suppression
Спектральное подавление шума. Оценивает шумовой пол по первым 10 фреймам тишины.

### 5. Hysteresis noise gate
```c
// Открывать при уровне > OPEN_THRESH, закрывать при < CLOSE_THRESH
#define GATE_OPEN_THRESH   800    // ~-32 dBFS
#define GATE_CLOSE_THRESH  400
```

### 6. AEC (aec.c)
Переносится как есть из ESP32-S3 проекта. Reference signal = SCO output (то что пришло от телефона).

### 7. Soft limiter
```c
// Защита от клиппирования call audio
// MIC_SW_GAIN=2 для петлички (проверено на S3: <1% clips)
#define CALL_SW_GAIN  4   // для call audio (громче)
#define MIC_SW_GAIN   2   // для mic input
```

---

## Усиление микрофона (LINE_IN)

Из опыта с WM8960 на ESP32-S3:
- SNR петлички: **34 dB** при MIC_SW_GAIN=2
- Клипы: **<1%**
- Поэтому начать с PGA 0dB в ES8388, SW gain=2

ES8388 PGA регулировка (регистр 0x0A):
```
0x00 = 0 dB
0x04 = +6 dB
0x08 = +12 dB  ← начать отсюда если тихо
0x0C = +18 dB
```

---

## Буферы DMA и задержка

```
DMA буферов: 8 × 480 = 3840 семплов = 480ms буфер
Задержка pipeline: ~60ms (один буфер)
UDP jitter buffer на ПК: рекомендуется 100-200ms
```
