# -*- coding: utf-8 -*-
"""
PDU 抽象层：通过 SNMP 控制插座通断电

本文件只做了一份「参考实现骨架」，方便你按照自己实际的 PDU 设备
（品牌 / 型号 / MIB）去补全 OID 等细节。

核心目标是向上层暴露统一的 `PDUController` 接口，满足
`control_server.py` 中的调用方式：

    pdu = PDUController(ip, outlet_index)
    pdu.get_device_name()
    pdu.power_on()
    pdu.power_off()

你可以直接修改本文件以适配真实设备。
"""

from __future__ import annotations

import logging
from typing import Optional

from pysnmp.proto.rfc1902 import Integer  # type: ignore[import-not-found]

from pysnmp.hlapi import (  # type: ignore[import-not-found]
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    getCmd,
    setCmd,
)

LOG = logging.getLogger(__name__)


# ===== 根据你的 PDU 设备进行配置 =====

# sysName，用于读取设备名；大多数设备都支持该标准 OID
SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"

# 下面两个 OID 仅为「示例占位」，你需要：
# 1. 查阅自己 PDU 的 MIB / 文档；
# 2. 找到用于控制某个插座通断电的 OID 模板；
# 3. 把 OID 中表示「插座序号」的部分替换为 {index}。
#
# 例子（不一定适用于你的设备）：
#   "1.3.6.1.4.1.xxx.yyy.1.3.{index}.0"
PDU_OUTLET_ON_OID_TEMPLATE = "1.3.6.1.4.1.xxxx.yyyy.1.1.{index}"
PDU_OUTLET_OFF_OID_TEMPLATE = "1.3.6.1.4.1.xxxx.yyyy.1.1.{index}"

PLACEHOLDER_OID_MARKER = "xxxx.yyyy"

# 设置开关量时写入的值（常见情况是 1/on, 0/off，或者 1/2）
PDU_ON_VALUE = 1
PDU_OFF_VALUE = 0


class PDUError(RuntimeError):
    """PDU 通讯或操作异常。"""


class PDUController:
    """
    通过 SNMP 控制单个插座的简单封装。

    - `ip`：PDU 管理口 IP
    - `outlet_index`：插座序号（从 1 开始）
    - `community`：SNMP 团体名，通常默认 "public"/"private"
    - `port`：SNMP 端口，默认 161
    """

    def __init__(
        self,
        ip: str,
        outlet_index: int,
        *,
        community: str = "private",  #pdu = PDUController(PDU_IP, PDU_OUTLET_INDEX, community="public")
        port: int = 161,
        timeout: float = 2.0,
        retries: int = 1,
    ) -> None:
        self.ip = ip
        self.outlet_index = outlet_index
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = retries

        if outlet_index <= 0:
            raise ValueError("outlet_index 必须是大于 0 的整数")

        if PLACEHOLDER_OID_MARKER in PDU_OUTLET_ON_OID_TEMPLATE or PLACEHOLDER_OID_MARKER in PDU_OUTLET_OFF_OID_TEMPLATE:
            raise ValueError("请先在 pdusnmp.py 中配置真实的 PDU OID 模板，再运行程序")

    # ===== 对上层暴露的 API =====

    def get_device_name(self) -> str:
        """读取 PDU 设备名（SNMP sysName）。"""
        value = self._snmp_get(SYS_NAME_OID)
        return str(value) if value is not None else "Unknown PDU"

    def power_on(self) -> None:
        """给当前插座上电。"""
        oid = PDU_OUTLET_ON_OID_TEMPLATE.format(index=self.outlet_index)
        LOG.info("PDU[%s] 插座 %s 上电 (OID=%s)", self.ip, self.outlet_index, oid)
        self._snmp_set(oid, PDU_ON_VALUE)

    def power_off(self) -> None:
        """关闭当前插座电源。"""
        oid = PDU_OUTLET_OFF_OID_TEMPLATE.format(index=self.outlet_index)
        LOG.info("PDU[%s] 插座 %s 断电 (OID=%s)", self.ip, self.outlet_index, oid)
        self._snmp_set(oid, PDU_OFF_VALUE)

    # ===== 内部 SNMP 工具方法 =====

    def _snmp_get(self, oid: str) -> Optional[object]:
        iterator = getCmd(
            SnmpEngine(),
            CommunityData(self.community, mpModel=1),
            UdpTransportTarget(
                (self.ip, self.port),
                timeout=self.timeout,
                retries=self.retries,
            ),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )

        error_indication, error_status, error_index, var_binds = next(iterator)

        if error_indication:
            raise PDUError(f"SNMP GET 失败: {error_indication}")
        if error_status:
            raise PDUError(
                f"SNMP GET 错误: {error_status.prettyPrint()} at {error_index}",
            )
        for _name, val in var_binds:
            return val
        return None

    def _snmp_set(self, oid: str, value: int) -> None:
        iterator = setCmd(
            SnmpEngine(),
            CommunityData(self.community, mpModel=1),
            UdpTransportTarget(
                (self.ip, self.port),
                timeout=self.timeout,
                retries=self.retries,
            ),
            ContextData(),
            ObjectType(ObjectIdentity(oid), Integer(int(value))),
        )

        error_indication, error_status, error_index, _var_binds = next(iterator)

        if error_indication:
            raise PDUError(f"SNMP SET 失败: {error_indication}")
        if error_status:
            raise PDUError(
                f"SNMP SET 错误: {error_status.prettyPrint()} at {error_index}",
            )

