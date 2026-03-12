# -*- coding: utf-8 -*-
"""
PTLand 控制端（Server）

功能：
- 启动 rpyc ThreadedServer，端口 18861，监听 0.0.0.0
- 通过 pdusnmp 控制 PDU 电源
- 接收客户端心跳，管理看门狗，在电池放空后自动上电
"""

import logging
import os
import threading
import time
from typing import Optional

import rpyc

# 假设当前目录下已有 pdusnmp.py，并提供类似接口
# 你可以在 pdusnmp.py 中实现兼容如下调用的类：
# class PDUController:
#     def __init__(self, ip: str, outlet_index: int): ...
#     def get_device_name(self) -> str: ...
#     def power_on(self): ...
#     def power_off(self): ...
from pdusnmp import PDUController  # type: ignore

SERVER_TITLE = "PTLand_Server"
PDU_IP = os.getenv("PTLAND_PDU_IP", "192.168.0.163")
PDU_OUTLET_INDEX = int(os.getenv("PTLAND_PDU_OUTLET_INDEX", "1"))

HEARTBEAT_TIMEOUT_SECONDS = 30
POWER_ON_DELAY_AFTER_TIMEOUT = 30  # 额外等待 30s 再上电，确保完全掉电
POWER_OPERATION_RETRIES = 3
POWER_RETRY_INTERVAL_SECONDS = 2


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[{asctime}] [{levelname}] {message}",
        style="{",
        datefmt="%H:%M:%S",
    )


class PTLandState(object):
    """保存 Server 侧共享状态"""

    def __init__(self, pdu: PDUController) -> None:
        self.pdu = pdu
        self.last_heartbeat: Optional[float] = None
        self.watchdog_enabled: bool = False
        self.lock = threading.Lock()
        self._stop_event = threading.Event()

    def safe_power_on(self) -> bool:
        for attempt in range(1, POWER_OPERATION_RETRIES + 1):
            try:
                self.pdu.power_on()
                logging.info("PDU 插座已上电")
                return True
            except Exception as exc:  # noqa: BLE001
                logging.error("执行 PDU Power ON 失败（第 %d/%d 次）：%s", attempt, POWER_OPERATION_RETRIES, exc)
                if attempt < POWER_OPERATION_RETRIES:
                    time.sleep(POWER_RETRY_INTERVAL_SECONDS)
        return False

    def safe_power_off(self) -> bool:
        for attempt in range(1, POWER_OPERATION_RETRIES + 1):
            try:
                self.pdu.power_off()
                logging.info("PDU 插座已断电")
                return True
            except Exception as exc:  # noqa: BLE001
                logging.error("执行 PDU Power OFF 失败（第 %d/%d 次）：%s", attempt, POWER_OPERATION_RETRIES, exc)
                if attempt < POWER_OPERATION_RETRIES:
                    time.sleep(POWER_RETRY_INTERVAL_SECONDS)
        return False

    def update_heartbeat(self) -> None:
        with self.lock:
            self.last_heartbeat = time.time()

    def enable_watchdog(self) -> None:
        with self.lock:
            self.watchdog_enabled = True
            self.last_heartbeat = time.time()
        logging.info("看门狗已启用（等待客户端心跳超时以触发上电）")

    def disable_watchdog(self) -> None:
        with self.lock:
            if self.watchdog_enabled:
                logging.info("看门狗已关闭")
            self.watchdog_enabled = False

    def stop(self) -> None:
        self._stop_event.set()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()


SERVER_STATE: Optional["PTLandState"] = None


class PTLandService(rpyc.Service):
    """
    rpyc 服务：暴露给客户端调用的接口
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super(PTLandService, self).__init__(*args, **kwargs)
        # 使用模块级全局状态，避免为 rpyc 自定义工厂
        global SERVER_STATE
        if SERVER_STATE is None:
            raise RuntimeError("SERVER_STATE 尚未初始化")
        self.state = SERVER_STATE

    # --- 公开给客户端的 RPC 方法 ---

    def exposed_heartbeat(self) -> None:
        """
        客户端定期调用，用于喂狗。
        """
        self.state.update_heartbeat()
        logging.debug("收到客户端心跳")

    def exposed_request_power_off(self) -> None:
        """
        客户端请求断电（开始放电过程）。
        """
        logging.info("收到客户端断电请求：执行 PDU Power OFF，并启用看门狗")
        self.state.safe_power_off()
        self.state.enable_watchdog()

    def exposed_request_power_on(self) -> None:
        """
        客户端请求上电（例如充电阶段或开机双重保险）。
        """
        logging.info("收到客户端上电请求：执行 PDU Power ON")
        self.state.safe_power_on()
        # 上电后可以关闭看门狗，避免误触发
        self.state.disable_watchdog()

    def exposed_get_status(self) -> dict:
        """
        简单状态查询，便于调试。
        """
        with self.state.lock:
            data = {
                "watchdog_enabled": self.state.watchdog_enabled,
                "last_heartbeat": self.state.last_heartbeat,
            }
        return data


def watchdog_loop(state: PTLandState) -> None:
    """
    后台线程：监控心跳，在超时后执行上电。
    """
    logging.info(
        "看门狗线程已启动：超时时间=%ds，超时后额外等待=%ds，然后执行上电",
        HEARTBEAT_TIMEOUT_SECONDS,
        POWER_ON_DELAY_AFTER_TIMEOUT,
    )
    while not state.is_stopped():
        time.sleep(1.0)
        with state.lock:
            if not state.watchdog_enabled:
                continue
            last = state.last_heartbeat

        if last is None:
            # 尚未收到心跳，继续等待
            continue

        elapsed = time.time() - last
        if elapsed > HEARTBEAT_TIMEOUT_SECONDS:
            logging.warning(
                "心跳超时：已 %0.1fs 未收到客户端心跳，推测客户端因电池耗尽进入 S5",
                elapsed,
            )
            # 先关闭看门狗，避免重复触发
            state.disable_watchdog()

            logging.info(
                "等待 %ds 确保完全断电，然后执行 PDU 上电",
                POWER_ON_DELAY_AFTER_TIMEOUT,
            )
            for _ in range(POWER_ON_DELAY_AFTER_TIMEOUT):
                if state.is_stopped():
                    return
                time.sleep(1.0)

            if state.safe_power_on():
                logging.info("PDU 已上电，等待客户端重新开机并恢复测试")


def main() -> None:
    # 设置控制台标题（仅在 Windows 有效）
    try:
        os.system(f"title {SERVER_TITLE}")
    except Exception:
        pass

    setup_logging()
    logging.info("PTLand 控制端启动")

    # 初始化 PDU
    logging.info("初始化 PDU：IP=%s, Outlet Index=%s", PDU_IP, PDU_OUTLET_INDEX)
    pdu = PDUController(PDU_IP, PDU_OUTLET_INDEX)
    try:
        dev_name = pdu.get_device_name()
    except Exception as exc:  # noqa: BLE001
        logging.error("读取 PDU 设备名失败：%s", exc)
        dev_name = "Unknown PDU"

    logging.info("已连接到 PDU 设备：%s", dev_name)

    # 初始化共享状态
    state = PTLandState(pdu)

    # 启动看门狗线程
    t_watchdog = threading.Thread(
        target=watchdog_loop,
        args=(state,),
        daemon=True,
    )
    t_watchdog.start()

    # 启动 rpyc ThreadedServer
    logging.info("启动 RPC 服务：0.0.0.0:18861")

    global SERVER_STATE
    SERVER_STATE = state

    server = rpyc.ThreadedServer(
        service=PTLandService,
        hostname="0.0.0.0",
        port=18861,
        protocol_config={
            "allow_public_attrs": True,
        },
    )

    try:
        server.start()
    except KeyboardInterrupt:
        logging.info("收到中断信号，正在停止服务...")
    finally:
        state.stop()
        logging.info("PTLand 控制端已退出")


if __name__ == "__main__":
    main()
