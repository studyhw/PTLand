真正要控制插座时，需要：
换成真实 PDU 的 IP、community（PDU_IP 和 PDUController 里的 community）；
把 pdusnmp.py 里 PDU_OUTLET_ON_OID_TEMPLATE / PDU_OUTLET_OFF_OID_TEMPLATE 改成你设备文档里的真实 OID 模板。
换成真实 PDU 的 IP、community（PDU_IP 和 PDUController 里的 community）；这个文件是哪一个

需要修改的两个地方
1）PDU_IP：在 control_server.py 里
在文件开头这段：
PDU_IP = "192.168.0.163"PDU_OUTLET_INDEX = 1
把 "192.168.0.163" 改成你真实 PDU 的管理口 IP。
如果不是第 1 个插口，也可以改 PDU_OUTLET_INDEX。
2）community：在 pdusnmp.py 的 PDUController 里
def __init__(        self,        ip: str,        outlet_index: int,        *,        community: str = "private",        port: int = 161,        timeout: float = 2.0,        retries: int = 1,    ) -> None:
有两种改法，选一种即可：
如果你们 PDU 的团体名固定，比如 public：
直接把上面 community: str = "private" 改成：
  community: str = "public",
如果将来不同设备团体名不一样，可以在 control_server.py 里改成显式传入，例如：
  pdu = PDUController(PDU_IP, PDU_OUTLET_INDEX, community="public")
总结：IP 在 control_server.py，community 默认值在 pdusnmp.py 的 PDUController.__init__ 里。
