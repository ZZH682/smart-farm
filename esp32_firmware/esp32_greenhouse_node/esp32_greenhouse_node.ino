/*
 * ============================================================
 *  智慧农业大棚 ESP32 节点固件
 *  功能：DHT11 空气温湿度 + DS18B20 土壤温度 + 继电器控制
 *  通信：WiFi + MQTT，对接 SmartFarm 2.0 系统
 *  板型：ESP32-C3 DevKitM-1 / ESP32-C3-DevKitC-02（RISC-V 架构）
 * ============================================================
 *
 * 【所需 Arduino 库】（库管理器中搜索安装）：
 *   1. DHT sensor library (Adafruit)        — DHT11 温湿度
 *   2. Adafruit Unified Sensor               — DHT 依赖
 *   3. OneWire (Paul Stoffregen)            — DS18B20 单总线
 *   4. DallasTemperature (Miles Burton)     — DS18B20 温度读取
 *   5. PubSubClient (Nick O'Leary)          — MQTT 客户端
 *   6. ArduinoJson (Benoit Blanchon)        — JSON 解析（v6.x）
 *
 * 【Arduino IDE 配置】：
 *   开发板：ESP32C3 Dev Module
 *   上传速率：921600
 *   CPU 频率：160MHz
 *   Flash 频率：80MHz
 *   Flash 大小：4MB
 *   Partition Scheme：Default 4MB with spiffs
 *
 * 【接线说明】（ESP32-C3 可用 GPIO: 0-10, 18-21；GPIO11为strapping，12-17为SPI Flash不可用）：
 *   继电器1(风扇)     IN -> GPIO5   （已接）
 *   DHT11      DATA -> GPIO4   （未接，读取失败不影响运行）
 *   DS18B20    DATA -> GPIO10  （未接，读取失败不影响运行；GPIO21是UART0串口勿用！）
 *   YL-69土壤湿度 AO -> GPIO3   （未接，AO=模拟输出；必须用ADC1，GPIO6属ADC2与WiFi冲突！）
 *   继电器2(水泵)     IN -> GPIO18  （未接）
 *   继电器3(补光灯)   IN -> GPIO19  （未接）
 *   继电器4(通风)     IN -> GPIO20  （未接）
 *
 *   注意：继电器模块 VCC 接 5V（大多数继电器模块需要5V驱动），
 *        ESP32 的 3.3V 逻辑电平可以直接驱动 IN 引脚。
 *        继电器模块是低电平触发还是高电平触发，在 RELAY_ACTIVE_LEVEL 配置。
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <ArduinoJson.h>
// 私密配置（WiFi 密码 / MQTT 地址）单独放在 config_secret.h，
// 已被 .gitignore 排除，防止随源码泄露；编辑该文件即可，勿改本文件。
#include "config_secret.h"

// ============================================================
//  配置区域（根据实际情况修改）
// ============================================================

// ---- 引脚配置（ESP32-C3）----
#define PIN_DHT         4    // DHT11 数据引脚（未接可不接，读取失败不影响运行）
#define PIN_DS18B20     10   // DS18B20 土壤温度（未接可不接；GPIO10避开UART0的GPIO20/21）
#define PIN_SOIL_MOISTURE 3  // YL-69 土壤湿度 AO 输出（ADC1_CH3，WiFi下稳定；勿用ADC2引脚！）
#define PIN_RELAY_FAN       5    // 继电器1：风扇（已接 GPIO5）
#define PIN_RELAY_PUMP      18   // 继电器2：水泵（未接）
#define PIN_RELAY_LIGHT     19   // 继电器3：补光灯（未接）
#define PIN_RELAY_VENT      20   // 继电器4：通风（未接，没有就不接）

// ---- 继电器触发电平 ----
// 实测：本继电器模块为【高电平触发】（IN 接高电平时吸合）= HIGH
// 若换成低电平触发模块，改回 LOW
#define RELAY_ACTIVE_LEVEL  HIGH

// ---- 传感器安装开关（0=未接，1=已接）----
// 重要：只有接了对应传感器才设为 1！悬空引脚会读出假数据并发布，污染系统！
#define DHT11_INSTALLED     1    // DHT11 空气温湿度（GPIO4）——已接
#define YL69_INSTALLED      1    // YL-69 土壤湿度（GPIO3）——已接
// DS18B20 无需开关：未接时读取失败会自动不发布（天然安全）

// ---- 传感器配置 ----
#define DHT_TYPE        DHT11
#define SENSOR_INTERVAL 5000   // 传感器上报间隔（毫秒），默认5秒
#define HEARTBEAT_INTERVAL 10000  // 心跳间隔（毫秒）

// ============================================================
//  MQTT 主题定义（与 SmartFarm 2.0 系统一致，请勿修改）
// ============================================================
#define TOPIC_PREFIX          "farm/greenhouse"
#define TOPIC_SENSOR_TEMP     TOPIC_PREFIX "/sensor/temperature"
#define TOPIC_SENSOR_HUMI     TOPIC_PREFIX "/sensor/humidity"
#define TOPIC_SENSOR_SOIL_TEMP TOPIC_PREFIX "/sensor/soil_temperature"
#define TOPIC_SENSOR_SOIL_MOIST TOPIC_PREFIX "/sensor/soil_moisture"
#define TOPIC_CONTROL_FAN     TOPIC_PREFIX "/control/fan"
#define TOPIC_CONTROL_PUMP    TOPIC_PREFIX "/control/water_pump"
#define TOPIC_CONTROL_LIGHT   TOPIC_PREFIX "/control/light_supplement"
#define TOPIC_CONTROL_VENT    TOPIC_PREFIX "/control/ventilation"
#define TOPIC_STATUS_FAN      TOPIC_PREFIX "/device/fan/status"
#define TOPIC_STATUS_PUMP     TOPIC_PREFIX "/device/water_pump/status"
#define TOPIC_STATUS_LIGHT    TOPIC_PREFIX "/device/light_supplement/status"
#define TOPIC_STATUS_VENT     TOPIC_PREFIX "/device/ventilation/status"
#define TOPIC_HEARTBEAT       TOPIC_PREFIX "/device/esp32/heartbeat"
#define TOPIC_LWT             TOPIC_PREFIX "/device/esp32/status"

// ============================================================
//  全局对象
// ============================================================
WiFiClient espClient;
PubSubClient client(espClient);
DHT dht(PIN_DHT, DHT_TYPE);
OneWire oneWire(PIN_DS18B20);
DallasTemperature soilTempSensor(&oneWire);

// 继电器引脚数组（顺序：风扇、水泵、补光灯、通风）
const int relayPins[4] = {PIN_RELAY_FAN, PIN_RELAY_PUMP, PIN_RELAY_LIGHT, PIN_RELAY_VENT};
const char* relayNames[4] = {"fan", "water_pump", "light_supplement", "ventilation"};
const char* relayControlTopics[4] = {TOPIC_CONTROL_FAN, TOPIC_CONTROL_PUMP, TOPIC_CONTROL_LIGHT, TOPIC_CONTROL_VENT};
const char* relayStatusTopics[4] = {TOPIC_STATUS_FAN, TOPIC_STATUS_PUMP, TOPIC_STATUS_LIGHT, TOPIC_STATUS_VENT};
bool relayStates[4] = {false, false, false, false};  // 当前继电器状态

// 定时器
unsigned long lastSensorTime = 0;
unsigned long lastHeartbeatTime = 0;
unsigned long lastWifiReconnectAttempt = 0;

// ============================================================
//  继电器控制
// ============================================================
void setRelay(int index, bool state) {
  if (index < 0 || index >= 4) return;
  relayStates[index] = state;
  int level = state ? RELAY_ACTIVE_LEVEL : !RELAY_ACTIVE_LEVEL;
  digitalWrite(relayPins[index], level);

  // 上报设备状态
  const char* statusStr = state ? "开启" : "关闭";
  StaticJsonDocument<128> doc;
  doc["device"] = relayNames[index];
  doc["status"] = statusStr;
  doc["state"] = state ? 1 : 0;
  doc["timestamp"] = millis();
  char buffer[128];
  serializeJson(doc, buffer);
  client.publish(relayStatusTopics[index], buffer, true);  // retained 消息

  Serial.printf("[继电器] %s -> %s\n", relayNames[index], statusStr);
}

// ============================================================
//  MQTT 回调（处理控制指令）
// ============================================================
void mqttCallback(char* topic, byte* payload, unsigned int length) {
  // 将 payload 转为字符串
  char payloadStr[length + 1];
  memcpy(payloadStr, payload, length);
  payloadStr[length] = '\0';

  Serial.printf("[MQTT] 收到指令: %s -> %s\n", topic, payloadStr);

  // 匹配控制主题
  for (int i = 0; i < 4; i++) {
    if (strcmp(topic, relayControlTopics[i]) == 0) {
      // 解析指令：支持 "开启"/"关闭"/"on"/"off"/"1"/"0"/true/false
      String cmd = String(payloadStr);
      cmd.toLowerCase();
      bool state = false;
      if (cmd == "开启" || cmd == "on" || cmd == "1" || cmd == "true") {
        state = true;
      } else if (cmd == "关闭" || cmd == "off" || cmd == "0" || cmd == "false") {
        state = false;
      } else {
        // 尝试 JSON 格式解析 {"state":"开启"} 或 {"action":"on"}
        StaticJsonDocument<128> doc;
        DeserializationError error = deserializeJson(doc, payloadStr);
        if (!error) {
          if (doc.containsKey("state")) {
            String s = doc["state"].as<String>();
            s.toLowerCase();
            state = (s == "开启" || s == "on" || s == "1" || s == "true");
          } else if (doc.containsKey("action")) {
            String s = doc["action"].as<String>();
            s.toLowerCase();
            state = (s == "开启" || s == "on" || s == "1" || s == "true");
          }
        }
      }
      setRelay(i, state);
      return;
    }
  }
}

// ============================================================
//  WiFi 连接
// ============================================================
void connectWiFi() {
  Serial.printf("[WiFi] 正在连接 %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] 连接成功! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WiFi] 连接失败，将在 loop 中重试");
  }
}

// ============================================================
//  MQTT 连接
// ============================================================
void connectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return;

  client.setServer(MQTT_BROKER, MQTT_PORT);
  client.setCallback(mqttCallback);
  client.setBufferSize(512);

  Serial.printf("[MQTT] 正在连接 %s:%d ...\n", MQTT_BROKER, MQTT_PORT);

  // LWT 遗嘱消息：设备离线时自动发布
  bool connected;
  if (strlen(MQTT_USERNAME) > 0) {
    connected = client.connect(MQTT_CLIENT_ID, MQTT_USERNAME, MQTT_PASSWORD,
                                TOPIC_LWT, 1, true, "{\"status\":\"offline\"}");
  } else {
    connected = client.connect(MQTT_CLIENT_ID, TOPIC_LWT, 1, true, "{\"status\":\"offline\"}");
  }

  if (connected) {
    Serial.println("[MQTT] 连接成功!");

    // 订阅所有控制主题
    for (int i = 0; i < 4; i++) {
      client.subscribe(relayControlTopics[i], 1);
      Serial.printf("[MQTT] 订阅: %s\n", relayControlTopics[i]);
    }

    // 发布上线消息
    String onlineMsg = String("{\"status\":\"online\",\"ip\":\"") + WiFi.localIP().toString() + "\"}";
    client.publish(TOPIC_LWT, onlineMsg.c_str(), true);

    // 上报当前所有继电器状态
    for (int i = 0; i < 4; i++) {
      setRelay(i, relayStates[i]);
    }
  } else {
    Serial.printf("[MQTT] 连接失败，错误码: %d\n", client.state());
  }
}

// ============================================================
//  读取传感器并上报
// ============================================================
void readAndPublishSensors() {
  // ---- DHT11 空气温湿度（未安装时跳过，避免悬空脏读污染数据）----
#if DHT11_INSTALLED
  // 三重校验：①读取失败(isnan) ②物理范围 ③与上次成功读数对比（跳变检测）。
  // 模块损坏/供电不足/接触不良时，DHT11 可能返回"看似正常但离谱"的恒定值
  // （如 1℃/8%），isnan 拦不住、范围也拦不住，必须用跳变检测拦截：
  // 真实环境下 5 秒内温度不可能跳变 >3℃、湿度不可能跳变 >8%。
  static float lastGoodTemp = -999.0f;
  static float lastGoodHum = -999.0f;
  float temperature = NAN;
  float humidity = NAN;
  for (int attempt = 0; attempt < 3; attempt++) {
    float t1 = dht.readTemperature();
    float h1 = dht.readHumidity();
    if (isnan(t1) || isnan(h1)) { delay(300); continue; }
    delay(120);  // 两次读取间隔，校验一致性
    float t2 = dht.readTemperature();
    float h2 = dht.readHumidity();
    if (isnan(t2) || isnan(h2)) { delay(300); continue; }
    // 两次读数差异过大 -> 读取不稳定（时序/供电问题）
    if (fabsf(t1 - t2) <= 2.0f && fabsf(h1 - h2) <= 5.0f) {
      temperature = (t1 + t2) / 2.0f;
      humidity = (h1 + h2) / 2.0f;
      break;
    }
    delay(300);
  }

  if (isnan(temperature) || isnan(humidity)) {
    Serial.println("[DHT11] 读取失败或读数不稳定（连续校验未通过），本次不上报");
  } else if (temperature < -10.0f || temperature > 55.0f || humidity < 5.0f || humidity > 100.0f) {
    Serial.printf("[DHT11] 读数超出合理范围，丢弃: %.1f°C / %.1f%%\n", temperature, humidity);
  } else if (lastGoodTemp > -900.0f
             && (fabsf(temperature - lastGoodTemp) > 3.0f || fabsf(humidity - lastGoodHum) > 8.0f)) {
    Serial.printf("[DHT11] 读数异常跳变（传感器可能故障），丢弃: %.1f°C / %.1f%%（上次 %.1f°C / %.1f%%）\n",
                  temperature, humidity, lastGoodTemp, lastGoodHum);
  } else {
    lastGoodTemp = temperature;
    lastGoodHum = humidity;
    Serial.printf("[DHT11] 温度: %.1f°C, 湿度: %.1f%%\n", temperature, humidity);

    // 上报温度
    StaticJsonDocument<128> tempDoc;
    tempDoc["value"] = temperature;
    tempDoc["unit"] = "°C";
    tempDoc["sensor"] = "dht11";
    tempDoc["timestamp"] = millis();
    char tempBuf[128];
    serializeJson(tempDoc, tempBuf);
    client.publish(TOPIC_SENSOR_TEMP, tempBuf);

    // 上报湿度
    StaticJsonDocument<128> humiDoc;
    humiDoc["value"] = humidity;
    humiDoc["unit"] = "%";
    humiDoc["sensor"] = "dht11";
    humiDoc["timestamp"] = millis();
    char humiBuf[128];
    serializeJson(humiDoc, humiBuf);
    client.publish(TOPIC_SENSOR_HUMI, humiBuf);
  }
#endif  // DHT11_INSTALLED

  // ---- DS18B20 土壤温度 ----
  soilTempSensor.requestTemperatures();
  float soilTemp = soilTempSensor.getTempCByIndex(0);

  if (soilTemp == DEVICE_DISCONNECTED_C || soilTemp < -50 || soilTemp > 125) {
    Serial.println("[DS18B20] 读取失败或设备未连接!");
  } else {
    Serial.printf("[DS18B20] 土壤温度: %.1f°C\n", soilTemp);

    StaticJsonDocument<128> soilDoc;
    soilDoc["value"] = soilTemp;
    soilDoc["unit"] = "°C";
    soilDoc["sensor"] = "ds18b20";
    soilDoc["timestamp"] = millis();
    char soilBuf[128];
    serializeJson(soilDoc, soilBuf);
    client.publish(TOPIC_SENSOR_SOIL_TEMP, soilBuf);
  }

  // ---- YL-69 土壤湿度（AO 模拟输出 -> ADC1，未安装时跳过）----
#if YL69_INSTALLED
  int soilRawMv = analogReadMilliVolts(PIN_SOIL_MOISTURE);
  // YL-69 AO：越湿电压越低。干燥约3000mV，泡水约500mV（实测后可按环境校准）
  const int SOIL_DRY_MV = 3000;   // 干燥时电压(mV)
  const int SOIL_WET_MV = 500;    // 完全浸湿时电压(mV)
  float soilMoisture = (SOIL_DRY_MV - soilRawMv) * 100.0f / (SOIL_DRY_MV - SOIL_WET_MV);
  if (soilMoisture < 0) soilMoisture = 0;
  if (soilMoisture > 100) soilMoisture = 100;
  Serial.printf("[YL69] 土壤湿度: %.1f%% (raw=%dmV)\n", soilMoisture, soilRawMv);

  StaticJsonDocument<128> moistDoc;
  moistDoc["value"] = (int)soilMoisture;
  moistDoc["unit"] = "%";
  moistDoc["sensor"] = "yl69";
  moistDoc["timestamp"] = millis();
  char moistBuf[128];
  serializeJson(moistDoc, moistBuf);
  client.publish(TOPIC_SENSOR_SOIL_MOIST, moistBuf);
#endif  // YL69_INSTALLED
}

// ============================================================
//  心跳
// ============================================================
void sendHeartbeat() {
  StaticJsonDocument<192> doc;
  doc["device"] = "esp32_greenhouse_node";
  doc["status"] = "online";
  doc["uptime"] = millis() / 1000;
  doc["wifi_rssi"] = WiFi.RSSI();
  doc["ip"] = WiFi.localIP().toString();
  doc["free_heap"] = ESP.getFreeHeap();
  char buffer[192];
  serializeJson(doc, buffer);
  client.publish(TOPIC_HEARTBEAT, buffer);
  Serial.println("[心跳] 已发送");
}

// ============================================================
//  setup
// ============================================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n========================================");
  Serial.println("  智慧农业 ESP32 节点启动中...");
  Serial.println("========================================");

  // 初始化继电器引脚（默认断开）
  for (int i = 0; i < 4; i++) {
    pinMode(relayPins[i], OUTPUT);
    digitalWrite(relayPins[i], !RELAY_ACTIVE_LEVEL);  // 默认断开
  }
  Serial.println("[继电器] 初始化完成，全部断开");

  // 初始化 DHT11
  dht.begin();
  Serial.println("[DHT11] 初始化完成");

  // 初始化 YL-69 土壤湿度（ADC1）
  analogReadResolution(12);  // ADC 12位精度
  pinMode(PIN_SOIL_MOISTURE, INPUT);
  Serial.println("[YL69] 土壤湿度初始化完成");

  // 初始化 DS18B20
  soilTempSensor.begin();
  Serial.printf("[DS18B20] 初始化完成，总线上设备数: %d\n", soilTempSensor.getDeviceCount());

  // 连接 WiFi
  connectWiFi();

  // 连接 MQTT
  if (WiFi.status() == WL_CONNECTED) {
    connectMQTT();
  }

  Serial.println("========================================\n");
}

// ============================================================
//  loop
// ============================================================
void loop() {
  // ---- WiFi 重连 ----
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastWifiReconnectAttempt > 5000) {
      lastWifiReconnectAttempt = millis();
      Serial.println("[WiFi] 断开，尝试重连...");
      WiFi.disconnect();
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    }
    delay(100);
    return;
  }

  // ---- MQTT 重连 ----
  if (!client.connected()) {
    if (millis() - lastWifiReconnectAttempt > 3000) {
      lastWifiReconnectAttempt = millis();
      connectMQTT();
    }
    delay(100);
    return;
  }

  // MQTT 循环（必须持续调用）
  client.loop();

  // ---- 定时上报传感器 ----
  if (millis() - lastSensorTime >= SENSOR_INTERVAL) {
    lastSensorTime = millis();
    readAndPublishSensors();
  }

  // ---- 定时心跳 ----
  if (millis() - lastHeartbeatTime >= HEARTBEAT_INTERVAL) {
    lastHeartbeatTime = millis();
    sendHeartbeat();
  }

  delay(10);  // 短暂延时，避免 CPU 占用过高
}
