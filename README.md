# Land

Land 是一个双机联动的电池自动化测试系统，用于在长时间充放电循环中，验证笔记本电脑在断电、关机、重启等场景下的稳定性与可靠性。

## 功能特点

- **无人值守的长时间电池充放电循环测试**：自动控制 PDU 插座通断电，模拟"拔市电 → 电池放电 → 电量耗尽关机 → 再上电重启 → 重新充满电"的完整流程
- **断电重启后测试连续**：通过持久化状态与开机自启，实现设备关机后自动恢复测试
- **双机协同**：控制端通过 SNMP 控制 PDU，客户端通过 RPC 通信，实现看门狗逻辑

## 运行环境

- Windows 10 / 11
- Python 3.8+
- 依赖：见 `requirements.txt`

## 快速开始

1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

2. 启动控制端（在连接 PDU 的控制电脑上）：
   ```bash
   python control_server.py
   ```

3. 启动客户端（在被测笔记本上）：
   ```bash
   python client.py
   ```

4. 在客户端 GUI 中配置 Server IP、ECTool 路径、BurnInTest 路径后点击「开始」

## 项目结构

- `control_server.py` - 控制端主程序（RPC 服务、PDU 控制、看门狗）
- `client.py` - 客户端主程序（GUI、状态机、测试流程）
- `pdusnmp.py` - PDU SNMP 封装模块
- `EC Tool/ec_tool.py` - 电池数据采集工具
- `操作设置以及说明书/` - 详细文档

## 详细说明

项目结构思维导图：https://boardmix.cn/app/editor/nVL7SXhGbOIgGGKcH1VsjA?inviteCode=2wApuf
全流程AI，开发

请参阅 [操作设置以及说明书/项目说明.md](操作设置以及说明书/项目说明.md) 获取完整的使用说明和配置指南。
