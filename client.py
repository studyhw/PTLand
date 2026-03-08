# -*- coding: utf-8 -*-
"""
PTLand 客户端（Client）

运行在被测笔记本上，实现：
- Tkinter GUI（可滚动布局）
- 与 Server 通过 rpyc 通信
- 充放电状态机（IDLE / DISCHARGING / WAITING_S5 / CHARGING）
- 配置与进度持久化（test_config.json）
- 开机自启注册表管理（HKCU\...\Run）
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

import psutil
import rpyc
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from tkinter import ttk

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:  # 非 Windows 环境下仅为类型兼容
    winreg = None  # type: ignore[assignment]

APP_TITLE = "PTLand"
CONFIG_FILE = "test_config.json"
REG_AUTORUN_NAME = "PTLand"

HEARTBEAT_INTERVAL = 5  # 秒
RPC_RECONNECT_INTERVAL = 5  # 秒
STATE_LOOP_INTERVAL_IDLE = 10  # 秒
STATE_LOOP_INTERVAL_DISCHARGING = 30  # 秒
STATE_LOOP_INTERVAL_CHARGING = 30  # 秒

BATTERY_START_DISCHARGE_THRESHOLD = 99  # %
BATTERY_LOW_THRESHOLD = 3  # %
BATTERY_FULL_THRESHOLD = 100  # %

STATE_IDLE = "IDLE"
STATE_DISCHARGING = "DISCHARGING"
STATE_WAITING_S5 = "WAITING_S5"
STATE_CHARGING = "CHARGING"
STATE_STOPPED = "STOPPED"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[{asctime}] [{levelname}] {message}",
        style="{",
        datefmt="%H:%M:%S",
    )


class RPCClient(object):
    """
    管理与 Server 的 rpyc 连接，支持自动重连和心跳。
    """

    def __init__(self, get_server_ip_callable) -> None:
        self._get_server_ip = get_server_ip_callable
        self._conn: Optional[rpyc.Connection] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start_background(self) -> None:
        t = threading.Thread(target=self._connect_loop, daemon=True)
        t.start()
        t_hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        t_hb.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _connect_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                conn = self._conn
            if conn is None:
                server_ip = self._get_server_ip()
                if not server_ip:
                    time.sleep(RPC_RECONNECT_INTERVAL)
                    continue
                try:
                    logging.info("尝试连接 Server：%s:18861", server_ip)
                    conn = rpyc.connect(server_ip, 18861, config={"allow_public_attrs": True})
                    with self._lock:
                        self._conn = conn
                    logging.info("已连接到 Server")
                except Exception as exc:  # noqa: BLE001
                    logging.error("连接 Server 失败：%s", exc)
                    time.sleep(RPC_RECONNECT_INTERVAL)
                    continue
            time.sleep(1.0)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            self.heartbeat()
            time.sleep(HEARTBEAT_INTERVAL)

    def _safe_call(self, method_name: str) -> None:
        with self._lock:
            conn = self._conn
        if conn is None:
            return
        try:
            getattr(conn.root, method_name)()
        except Exception as exc:  # noqa: BLE001
            logging.error("RPC 调用 %s 失败：%s", method_name, exc)
            with self._lock:
                try:
                    if self._conn is not None:
                        self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # 供外部调用的高层接口
    def heartbeat(self) -> None:
        self._safe_call("heartbeat")

    def request_power_off(self) -> None:
        logging.info("RPC 请求断电 (Power OFF)")
        self._safe_call("request_power_off")

    def request_power_on(self) -> None:
        logging.info("RPC 请求上电 (Power ON)")
        self._safe_call("request_power_on")


class ScrollableFrame(ttk.Frame):
    """
    使用 Canvas + Scrollbar + Frame 实现的可滚动容器。
    """

    def __init__(self, container, *args, **kwargs) -> None:
        super(ScrollableFrame, self).__init__(container, *args, **kwargs)

        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.v_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.v_scrollbar.set)

        self.inner_frame = ttk.Frame(self.canvas)

        # 把 inner_frame 放入 canvas
        self.inner_window = self.canvas.create_window(
            (0, 0),
            window=self.inner_frame,
            anchor="nw",
        )

        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.v_scrollbar.grid(row=0, column=1, sticky="ns")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # 绑定尺寸变化事件，更新 scrollregion
        self.inner_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # 支持鼠标滚轮
        self.inner_frame.bind("<Enter>", self._bind_mousewheel)
        self.inner_frame.bind("<Leave>", self._unbind_mousewheel)

    def _on_frame_configure(self, event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        # 当 canvas 大小变化时，让 inner_frame 宽度跟随 canvas 变化，保证自适应
        canvas_width = event.width
        self.canvas.itemconfig(self.inner_window, width=canvas_width)

    def _bind_mousewheel(self, _event) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event) -> None:
        # Windows 下 delta 为 120 的倍数
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class AppConfig(object):
    """
    管理配置与进度持久化（test_config.json）
    """

    def __init__(self) -> None:
        self.server_ip: str = ""
        self.ectool_path: str = ""
        self.bit_path: str = ""
        self.total_cycles: int = 1
        self.current_cycle: int = 0
        self.state: str = STATE_IDLE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "server_ip": self.server_ip,
            "ectool_path": self.ectool_path,
            "bit_path": self.bit_path,
            "total_cycles": self.total_cycles,
            "current_cycle": self.current_cycle,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        cfg = cls()
        cfg.server_ip = str(data.get("server_ip", ""))
        cfg.ectool_path = str(data.get("ectool_path", ""))
        cfg.bit_path = str(data.get("bit_path", ""))
        try:
            cfg.total_cycles = int(data.get("total_cycles", 1))
        except Exception:
            cfg.total_cycles = 1
        try:
            cfg.current_cycle = int(data.get("current_cycle", 0))
        except Exception:
            cfg.current_cycle = 0
        cfg.state = str(data.get("state", STATE_IDLE))
        return cfg

    def load(self, path: str = CONFIG_FILE) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = AppConfig.from_dict(data)
            self.server_ip = cfg.server_ip
            self.ectool_path = cfg.ectool_path
            self.bit_path = cfg.bit_path
            self.total_cycles = cfg.total_cycles
            self.current_cycle = cfg.current_cycle
            self.state = cfg.state
        except Exception as exc:  # noqa: BLE001
            logging.error("加载配置文件失败：%s", exc)

    def save(self, path: str = CONFIG_FILE) -> None:
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception as exc:  # noqa: BLE001
            logging.error("保存配置文件失败：%s", exc)


class AutoRunManager(object):
    """
    管理 HKCU\\...\\Run 自启动。
    """

    RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @staticmethod
    def is_supported() -> bool:
        return winreg is not None

    @staticmethod
    def _get_executable_command() -> str:
        """
        返回写入注册表的启动命令：
        - 运行 .py 时：写入 "python.exe client.py" 的形式；
        - 打包为 .exe 时：只写入打包后的 exe 路径。
        """
        # PyInstaller 打包后的运行环境会有 sys.frozen 标记
        if getattr(sys, "frozen", False):
            # 此时 sys.executable 就是打包后的 exe 完整路径
            exe_path = sys.executable
            return f"\"{exe_path}\""

        # 普通脚本模式
        exe = sys.executable
        script = os.path.abspath(__file__)
        return f"\"{exe}\" \"{script}\""

    @classmethod
    def enable_autorun(cls) -> None:
        if not cls.is_supported():
            logging.warning("当前环境不支持 winreg，自启动设置将被忽略")
            return
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                cls.RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            )
        except FileNotFoundError:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, cls.RUN_KEY_PATH)

        try:
            cmd = cls._get_executable_command()
            winreg.SetValueEx(key, REG_AUTORUN_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
            logging.info("已添加开机自启注册表键：%s -> %s", REG_AUTORUN_NAME, cmd)
        except Exception as exc:  # noqa: BLE001
            logging.error("设置开机自启失败：%s", exc)

    @classmethod
    def disable_autorun(cls) -> None:
        if not cls.is_supported():
            return
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                cls.RUN_KEY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            )
            try:
                winreg.DeleteValue(key, REG_AUTORUN_NAME)
                logging.info("已删除开机自启注册表键：%s", REG_AUTORUN_NAME)
            except FileNotFoundError:
                pass
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logging.error("删除开机自启失败：%s", exc)


class PTLandLogic(object):
    """
    负责状态机、进程管理、电池监控等业务逻辑。
    """

    def __init__(self, cfg: AppConfig, rpc_client: RPCClient, log_func) -> None:
        self.cfg = cfg
        self.rpc_client = rpc_client
        self.log = log_func

        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # --- 状态机控制 ---

    def start(self) -> None:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            if self.cfg.state not in (STATE_IDLE, STATE_DISCHARGING, STATE_CHARGING):
                # 如果是 WAITING_S5 或 STOPPED，默认从 IDLE 开始
                self.cfg.state = STATE_IDLE
            self.log(f"测试启动，当前状态：{self.cfg.state}，当前圈数：{self.cfg.current_cycle}/{self.cfg.total_cycles}")
            self.cfg.save()
            AutoRunManager.enable_autorun()
            t = threading.Thread(target=self._state_loop, daemon=True)
            self._worker_thread = t
            t.start()

    def stop(self) -> None:
        self.log("收到停止测试请求")
        self._stop_event.set()
        AutoRunManager.disable_autorun()
        self.cfg.state = STATE_STOPPED
        self.cfg.save()
        self._kill_test_processes()

    def reset(self) -> None:
        self.log("重置测试进度与状态")
        self.stop()
        self.cfg.current_cycle = 0
        self.cfg.state = STATE_IDLE
        self.cfg.save()

    # --- 核心状态循环 ---

    def _state_loop(self) -> None:
        while not self._stop_event.is_set():
            state = self.cfg.state
            if state == STATE_IDLE:
                self._handle_idle()
                time.sleep(STATE_LOOP_INTERVAL_IDLE)
            elif state == STATE_DISCHARGING:
                self._handle_discharging()
                time.sleep(STATE_LOOP_INTERVAL_DISCHARGING)
            elif state == STATE_WAITING_S5:
                self._handle_waiting_s5()
                # 等待关机状态下，通常不会继续循环，这里适当 sleep
                time.sleep(STATE_LOOP_INTERVAL_IDLE)
            elif state == STATE_CHARGING:
                self._handle_charging()
                time.sleep(STATE_LOOP_INTERVAL_CHARGING)
            else:
                # STOPPED 或未知状态
                self.log(f"状态机退出：当前状态={state}")
                break

    # --- 各状态处理 ---

    def _get_battery(self) -> Optional[psutil._common.sbattery]:
        try:
            batt = psutil.sensors_battery()
            if batt is None:
                self.log("无法获取电池信息（psutil.sensors_battery 返回 None）")
            return batt
        except Exception as exc:  # noqa: BLE001
            self.log(f"获取电池信息失败：{exc}")
            return None

    def _handle_idle(self) -> None:
        self.log("状态：IDLE（空闲） - 检查电池电量")
        batt = self._get_battery()
        if not batt:
            return

        percent = int(batt.percent)
        self.log(f"当前电量：{percent}%")

        if percent >= BATTERY_START_DISCHARGE_THRESHOLD:
            self.log("电量已超过阈值，进入放电状态 DISCHARGING")
            self.cfg.state = STATE_DISCHARGING
            self.cfg.save()
        else:
            self.log("电量不足以开始放电，请求服务器上电进行充电，进入 CHARGING")
            self.rpc_client.request_power_on()
            self.cfg.state = STATE_CHARGING
            self.cfg.save()

    def _kill_test_processes(self) -> None:
        self.log("清理残留测试进程（bit.exe / BurnInTest / ECTool 等）")
        target_names = {
            "bit.exe",
            "burnintest.exe",
            "burnintest64.exe",
            "BurnInTest.exe",
            "ECTool.exe",
            "ectool.exe",
        }
        for proc in psutil.process_iter(attrs=["pid", "name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "").lower()
                if any(t.lower() in (name, exe) for t in target_names):
                    self.log(f"终止进程：PID={proc.pid}, Name={proc.info.get('name')}")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as exc:  # noqa: BLE001
                self.log(f"终止进程失败：{exc}")

    def _start_ectool(self) -> None:
        if not self.cfg.ectool_path:
            self.log("ECTool 路径未配置，跳过启动")
            return
        exe_path = self.cfg.ectool_path
        cwd = os.path.dirname(exe_path) or "."
        self.log(f"启动 ECTool：{exe_path}")
        try:
            subprocess.Popen(
                [exe_path],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"启动 ECTool 失败：{exc}")

    def _start_burnintest(self) -> None:
        if not self.cfg.bit_path:
            self.log("BurnInTest 路径未配置，跳过启动")
            return
        exe_path = self.cfg.bit_path
        cwd = os.path.dirname(exe_path) or "."
        self.log(f"启动 BurnInTest：{exe_path} /r /D 0")
        try:
            subprocess.Popen(
                [exe_path, "/r", "/D", "0"],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"启动 BurnInTest 失败：{exc}")

    def _handle_discharging(self) -> None:
        self.log("状态：DISCHARGING（放电）")

        # 环境清理
        self._kill_test_processes()

        # 启动 ECTool
        self._start_ectool()

        # RPC 请求 Server 断电
        self.rpc_client.request_power_off()

        # 启动 BurnInTest
        self._start_burnintest()

        # 放电监控循环（直到电量 <= 3%）
        while not self._stop_event.is_set() and self.cfg.state == STATE_DISCHARGING:
            batt = self._get_battery()
            if not batt:
                time.sleep(STATE_LOOP_INTERVAL_DISCHARGING)
                continue

            percent = int(batt.percent)
            self.log(f"放电中，当前电量：{percent}%")

            if percent <= BATTERY_LOW_THRESHOLD:
                self.log(
                    f"电量已低于阈值 {BATTERY_LOW_THRESHOLD}%，关闭测试软件，准备进入 WAITING_S5",
                )
                self._kill_test_processes()
                # 在进入 WAITING_S5 前，把持久化状态设置为 CHARGING
                # 这样重启后可以直接进入充电流程
                self.cfg.state = STATE_WAITING_S5
                self.cfg.save()
                break

            time.sleep(STATE_LOOP_INTERVAL_DISCHARGING)

    def _handle_waiting_s5(self) -> None:
        # 正常情况下，系统会很快进入休眠或关机，本进程也会终止。
        # 这里仅做日志记录与状态预设。
        self.log("状态：WAITING_S5 - 等待系统自动休眠/关机")
        # 预设为 CHARGING 状态（重启后从 CHARGING 开始）
        self.cfg.state = STATE_CHARGING
        self.cfg.save()

    def _handle_charging(self) -> None:
        self.log("状态：CHARGING（充电） - 请求服务器上电并监控电量")

        # 双重保险，请求上电
        self.rpc_client.request_power_on()

        # 启动 ECTool 记录充电过程
        self._start_ectool()

        while not self._stop_event.is_set() and self.cfg.state == STATE_CHARGING:
            batt = self._get_battery()
            if not batt:
                time.sleep(STATE_LOOP_INTERVAL_CHARGING)
                continue

            percent = int(batt.percent)
            self.log(f"充电中，当前电量：{percent}%")

            if percent >= BATTERY_FULL_THRESHOLD:
                self.log("电量已充满，完成本轮充放电循环")
                self.cfg.current_cycle += 1
                self.cfg.save()

                if self.cfg.current_cycle >= self.cfg.total_cycles:
                    self.log("已达到总圈数，测试完成，停止状态机")
                    self.stop()
                    break
                else:
                    self.log(
                        f"准备进入下一轮放电：第 {self.cfg.current_cycle + 1}/{self.cfg.total_cycles} 轮",
                    )
                    self.cfg.state = STATE_DISCHARGING
                    self.cfg.save()
                    break

            time.sleep(STATE_LOOP_INTERVAL_CHARGING)


class PTLandGUI(object):
    """
    Tkinter GUI：包含输入控件、日志窗口与按钮。
    """

    def __init__(self, root: tk.Tk, cfg: AppConfig, logic: PTLandLogic, rpc_client: RPCClient) -> None:
        self.root = root
        self.cfg = cfg
        self.logic = logic
        self.rpc_client = rpc_client

        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self._build_ui()
        self._load_config_to_ui()
        self._start_log_updater()

    # --- UI 构建 ---

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)

        # 设置图标（如果 icon.ico 存在）
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
        if os.path.exists(icon_path):
            try:
                self.root.iconbitmap(icon_path)
            except Exception:
                pass

        # 主窗口布局
        self.root.geometry("800x600")
        self.root.minsize(600, 400)

        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=5, pady=5)

        # 顶部：Configuration 区域
        cfg_frame = ttk.LabelFrame(main_frame, text="Configuration")
        cfg_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(cfg_frame, text="Server IP:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.var_server_ip = tk.StringVar()
        self.entry_server_ip = ttk.Entry(cfg_frame, textvariable=self.var_server_ip)
        self.entry_server_ip.grid(row=0, column=1, sticky="ew", padx=5, pady=5)

        ttk.Label(cfg_frame, text="Cycles:").grid(row=0, column=2, sticky="w", padx=5, pady=5)
        self.var_total_cycles = tk.StringVar()
        self.entry_total_cycles = ttk.Entry(cfg_frame, textvariable=self.var_total_cycles, width=10)
        self.entry_total_cycles.grid(row=0, column=3, sticky="w", padx=5, pady=5)

        ttk.Label(cfg_frame, text="Progress:").grid(row=0, column=4, sticky="e", padx=5, pady=5)
        self.lbl_progress = ttk.Label(cfg_frame, text="0 / 0")
        self.lbl_progress.grid(row=0, column=5, sticky="w", padx=5, pady=5)

        cfg_frame.grid_columnconfigure(1, weight=1)

        # 中部：Software Paths 区域
        sw_frame = ttk.LabelFrame(main_frame, text="Software Paths")
        sw_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(sw_frame, text="ECTool:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.var_ectool = tk.StringVar()
        self.entry_ectool = ttk.Entry(sw_frame, textvariable=self.var_ectool)
        self.entry_ectool.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        btn_ectool = ttk.Button(sw_frame, text="Browse", command=self._browse_ectool)
        btn_ectool.grid(row=0, column=2, sticky="w", padx=5, pady=5)

        ttk.Label(sw_frame, text="BurnIn:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.var_bit = tk.StringVar()
        self.entry_bit = ttk.Entry(sw_frame, textvariable=self.var_bit)
        self.entry_bit.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        btn_bit = ttk.Button(sw_frame, text="Browse", command=self._browse_bit)
        btn_bit.grid(row=1, column=2, sticky="w", padx=5, pady=5)

        sw_frame.grid_columnconfigure(1, weight=1)

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="Log")
        log_frame.pack(fill="both", expand=True, pady=(0, 5))

        self.txt_log = scrolledtext.ScrolledText(log_frame, wrap="word", height=10, state="disabled")
        self.txt_log.pack(fill="both", expand=True)

        # 底部按钮
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill="x")

        self.btn_start = ttk.Button(btn_frame, text="Start Test", command=self.on_start)
        self.btn_start.pack(side="left", padx=5, pady=5)

        self.btn_reset = ttk.Button(btn_frame, text="Reset", command=self.on_reset)
        self.btn_reset.pack(side="left", padx=5, pady=5)

        self.btn_stop = ttk.Button(btn_frame, text="Stop Test", command=self.on_stop)
        self.btn_stop.pack(side="right", padx=5, pady=5)

        # 绑定关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _load_config_to_ui(self) -> None:
        self.var_server_ip.set(self.cfg.server_ip)
        self.var_ectool.set(self.cfg.ectool_path)
        self.var_bit.set(self.cfg.bit_path)
        self.var_total_cycles.set(str(self.cfg.total_cycles))
        self._update_state_labels()

    def _update_state_labels(self) -> None:
        self.lbl_progress.config(
            text=f"{self.cfg.current_cycle} / {self.cfg.total_cycles}",
        )

    # --- 文件选择 ---

    def _browse_ectool(self) -> None:
        path = filedialog.askopenfilename(title="选择 ECTool 可执行文件")
        if path:
            self.var_ectool.set(path)

    def _browse_bit(self) -> None:
        path = filedialog.askopenfilename(title="选择 BurnInTest 可执行文件")
        if path:
            self.var_bit.set(path)

    # --- 日志 ---

    def log(self, msg: str) -> None:
        """
        对外的日志接口，线程安全：其他线程使用该方法写日志。
        """
        logging.info(msg)
        self.log_queue.put(msg)

    def _start_log_updater(self) -> None:
        self._consume_log_queue()
        self.root.after(200, self._start_log_updater)

    def _consume_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg + "\n")
        except queue.Empty:
            pass
        self._update_state_labels()

    def _append_log(self, text: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # --- 事件处理 ---

    def _validate_and_save_config_from_ui(self) -> bool:
        self.cfg.server_ip = self.var_server_ip.get().strip()
        self.cfg.ectool_path = self.var_ectool.get().strip()
        self.cfg.bit_path = self.var_bit.get().strip()
        try:
            total = int(self.var_total_cycles.get().strip() or "1")
            if total <= 0:
                raise ValueError
            self.cfg.total_cycles = total
        except Exception:
            messagebox.showerror("错误", "总圈数必须是大于 0 的整数")
            return False

        if not self.cfg.server_ip:
            messagebox.showerror("错误", "Server IP 不能为空")
            return False

        self.cfg.save()
        self._update_state_labels()
        return True

    def on_start(self) -> None:
        if not self._validate_and_save_config_from_ui():
            return
        self.logic.start()

    def on_stop(self) -> None:
        self.logic.stop()

    def on_reset(self) -> None:
        if messagebox.askyesno("确认", "确定要重置测试进度并停止当前测试吗？"):
            self.logic.reset()
            self._load_config_to_ui()

    def on_close(self) -> None:
        # 停止逻辑与 RPC，再退出
        self.logic.stop()
        self.rpc_client.stop()
        self.root.destroy()


def main() -> None:
    setup_logging()

    # 加载配置
    cfg = AppConfig()
    cfg.load()

    # Tk 初始化
    root = tk.Tk()

    # RPC 客户端
    rpc_client = RPCClient(get_server_ip_callable=lambda: cfg.server_ip)
    rpc_client.start_background()

    # 逻辑层与 GUI
    # 先占位一个简单的 log_func，稍后由 GUI 覆盖
    dummy_log = lambda m: print(m)  # noqa: E731
    logic = PTLandLogic(cfg=cfg, rpc_client=rpc_client, log_func=dummy_log)

    gui = PTLandGUI(root=root, cfg=cfg, logic=logic, rpc_client=rpc_client)
    logic.log = gui.log  # 修正为 GUI 日志

    # 如果上次状态为 CHARGING 或 DISCHARGING，可选择自动恢复
    if cfg.state in (STATE_CHARGING, STATE_DISCHARGING):
        gui.log(f"检测到上次未完成状态：{cfg.state}，自动恢复状态机")
        logic.start()

    root.mainloop()


if __name__ == "__main__":
    main()

