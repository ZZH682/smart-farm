# 智慧农业大棚环境监控系统（Smart Farm）

基于 **MQTT + WebSocket** 的农业大棚环境监控与自动控制系统，实现「采集—传输—判定—执行」全自动闭环。个人独立开发项目，同时作为本科毕业设计课题。

## 功能特性

- **多参数实时采集**：温湿度、光照强度、土壤湿度；STM32 / ESP32 节点 ADC 多通道定时采样（1s 周期）
- **数据清洗**：滑动平均滤波抑制采样抖动与噪声，采集误差控制在 ±2% 以内；传感器坏值校验，避免误动作与告警风暴
- **MQTT 上报**：paho-mqtt 长连接，Topic 分级管理，设备心跳与在线状态监控
- **Web 实时大屏**：Flask + Flask-SocketIO 秒级推送，传感器数据 / 告警 / 控制事件即时到达浏览器；控制接口 Token 鉴权、CORS 同源收紧
- **越限告警**：阈值触发自动告警，支持企业微信 / 飞书 / 钉钉 / Server酱 webhook 推送
- **自动控制**：温度 35℃ 或湿度超限时触发继电器控制风扇 / 灌溉设备，闭环响应 200ms
- **AI 病虫害识别**：EfficientNet-B0（PlantVillage 38 类，准确率 97%+）首选，YOLOv8 检测降级，颜色分析规则引擎兜底；识别结果自动告警并持久化
- **数据持久化与分析**：SQLite 存储、趋势分析、温度预测（NumPy）、产量预测、定时报告生成
- **ESP32 固件**：Arduino 工程 + 一键编译烧录脚本

## 技术栈

Python 3 · Flask · Flask-SocketIO · paho-mqtt · SQLite · NumPy · EfficientNet-B0（PyTorch / timm）· YOLOv8（ultralytics）· STM32 / ESP32（Arduino）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
# 启用 AI 高精度识别需另装 torch / timm / ultralytics，见 requirements.txt 内注释

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env：MQTT broker 地址、LLM API Key、告警 webhook、Web Token 等
# .env 已被 .gitignore 排除，不会提交

# 3. 下载 AI 模型（可选，约 38MB，不随仓库分发）
python download_pest_model.py
# 不下载时自动降级为规则引擎 / 模拟识别

# 4. 启动
python main.py
# Web 监控端默认 http://localhost:5000（Token 启动时打印）
```

## 目录结构

```
smart_farm/
├── main.py               # 系统主入口（模块装配与启动）
├── config.py             # 配置加载（敏感项走环境变量）
├── mqtt_client.py        # MQTT 连接与 Topic 管理
├── device_manager.py     # 设备注册与心跳管理
├── sensor_simulator.py   # 传感器模拟器（无硬件调试用）
├── auto_control.py       # 阈值判定与继电器控制
├── data_storage.py       # SQLite 持久化
├── pest_detect.py        # AI 病虫害识别（EfficientNet/YOLO/规则引擎）
├── yield_predict.py      # 产量预测
├── temp_forecast.py      # 温度预测
├── trend_analysis.py     # 趋势分析
├── report_generator.py   # 报告生成
├── ai_assistant.py       # 农业知识问答助手（LLM）
├── web_monitor.py        # Flask + SocketIO 实时监控大屏
├── notify.py             # 多渠道告警推送
├── templates/            # 监控端页面模板
├── esp32_firmware/       # ESP32 节点固件（Arduino）
└── models/               # AI 模型权重（不随仓库分发）
```

## 安全说明

所有密钥（LLM API Key、告警 webhook、MQTT 密码、Web Token）均通过环境变量 / `.env` 配置，代码中无任何硬编码；`.env` 与模型文件已列入 `.gitignore`。

## 说明

本项目为本科毕业设计课题《基于 MQTT 的智慧农业大棚监控系统设计与实现》，仓库仅包含工程代码；论文与过程文档不随仓库公开。
