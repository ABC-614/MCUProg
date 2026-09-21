#! python3
import os
import re
import ast
import sys
import time
import html
import collections
import configparser
import traceback

from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import pyqtSlot, pyqtSignal, QThread
from PyQt5.QtWidgets import QApplication, QWidget, QMessageBox, QFileDialog

import jlink
import xlink
import chipid
import device
import device.chip
import device.flash


APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.environ['PATH'] = os.path.join(APP_DIR, 'libusb-1.0.24/MinGW64/dll') + os.pathsep + os.environ['PATH']


zero_if = lambda i: 0 if i == -1 else i


QSS = '''
QWidget         { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 9pt; }
QGroupBox       { border: 1px solid palette(mid); border-radius: 4px; margin-top: 9px; padding: 8px 6px 6px 6px; }
QGroupBox::title{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: palette(dark); }
QPushButton     { min-width: 76px; padding: 5px 10px; }
QToolButton     { padding: 5px 10px; }
QComboBox       { padding: 3px 6px; }
QProgressBar    { border: 1px solid palette(mid); border-radius: 3px; height: 20px; text-align: center; }
QProgressBar::chunk { background-color: #3daee9; }
QLabel#lblChipInfo, QLabel#lblStatus { color: palette(dark); }
QPlainTextEdit#txtLog, QPlainTextEdit#txtMem, QLineEdit#edtMemAddr, QLineEdit#edtMemData,
QTableWidget#tblRegs { font-family: Consolas, "Courier New", monospace; font-size: 9pt; }
'''


class LogStream(QtCore.QObject):
    ''' 把 print 的输出转发到界面日志框，同时保留原有的控制台输出 '''

    textWritten = pyqtSignal(str)

    def __init__(self, stream, parent=None):
        super(LogStream, self).__init__(parent)

        self.stream = stream

    def write(self, text):
        if self.stream:
            try: self.stream.write(text)
            except Exception: pass

        self.textWritten.emit(str(text))

    def flush(self):
        if self.stream:
            try: self.stream.flush()
            except Exception: pass


class MCUProg(QWidget):
    chipinfo = ''

    def __init__(self, parent=None):
        super(MCUProg, self).__init__(parent)

        uic.loadUi(os.path.join(APP_DIR, 'MCUProg.ui'), self)

        self.busy = False           # 正在擦除/烧写/读取
        self.linked = False         # 调试连接已建立
        self.daplinks = []
        self.probeerror = {}
        self.loading = False        # 正在填充烧写列表
        self.inipath = ''           # 当前烧写列表文件
        self.task = ''              # 当前任务名
        self.task_bytes = 0         # 当前任务的数据量，用于计算速度
        self.time_start = 0
        self.time_paint = 0
        self.logbuff = ''

        self.showList(False)
        self.btnStop.setVisible(False)
        self.grpDebug.setVisible(False)
        self.txtLog.setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 120)
        self.table.setColumnWidth(1, 100)
        self.table.setMaximumHeight(150)
        self.tblRegs.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)

        self.chipinfo = ''      # 窗口变窄时 lblChipInfo 只缩略显示，不撑大窗口的最小宽度
        self.lblChipInfo.setMinimumWidth(0)

        self.stdout, self.stderr = sys.stdout, sys.stderr
        sys.stdout = LogStream(self.stdout, self)
        sys.stderr = LogStream(self.stderr, self)
        sys.stdout.textWritten.connect(self.on_log)
        sys.stderr.textWritten.connect(self.on_log)

        self.initSetting()

        self.tmrDAP = QtCore.QTimer(self)
        self.tmrDAP.setInterval(1000)
        self.tmrDAP.timeout.connect(self.on_tmrDAP_timeout)
        self.tmrDAP.start()

        self.tmrTime = QtCore.QTimer(self)      # 任务计时
        self.tmrTime.setInterval(500)
        self.tmrTime.timeout.connect(self.on_tmrTime_timeout)

        QtCore.QTimer.singleShot(0, self.adjust_height)

    ''' 配置 '''

    def initSetting(self):
        self.conf = configparser.ConfigParser(interpolation=None)
        if os.path.exists(os.path.join(APP_DIR, 'setting.ini')):
            try:
                self.conf.read(os.path.join(APP_DIR, 'setting.ini'), encoding='utf-8')
            except Exception as e:
                print(f'读取 setting.ini 失败：{e}')

        for section in ('link', 'target', 'window', 'debug'):
            if not self.conf.has_section(section):
                self.conf.add_section(section)

        get = lambda sect, key, default='': self.conf.get(sect, key, fallback=default)

        self.cmbMode.setCurrentIndex(zero_if(self.cmbMode.findText(get('link', 'mode', 'ARM SWD'))))
        self.cmbSpeed.setCurrentIndex(zero_if(self.cmbSpeed.findText(get('link', 'speed', '4 MHz'))))

        self.cmbDLL.addItem(get('link', 'jlink', 'path/to/JLink_x64.dll'), 'jlink')
        self.cmbDLL.addItem('OpenOCD Tcl RPC (6666)', 'openocd')
        self.on_tmrDAP_timeout()    # add DAPLink

        self.cmbDLL.setCurrentIndex(zero_if(self.cmbDLL.findText(get('link', 'select'))))

        self.cmbMCU.addItems(self.parse_devices())
        self.cmbMCU.setCurrentIndex(zero_if(self.cmbMCU.findText(get('target', 'mcu', 'NUM480'))))

        self.cmbAddr.setCurrentIndex(zero_if(self.cmbAddr.findText(get('target', 'addr', '0 K'))))
        self.cmbSize.setCurrentIndex(zero_if(self.cmbSize.findText(get('target', 'size', '16 K'))))

        try:
            hexpath = ast.literal_eval(get('target', 'hexpath', '[]'))
        except Exception:
            hexpath = []
        self.cmbHEX.addItems([path for path in hexpath if path])

        self.savPath = get('target', 'savpath')

        self.chkVerify.setChecked(get('target', 'verify', 'yes') == 'yes')
        self.chkRun.setChecked(get('target', 'run', 'yes') == 'yes')

        self.edtMemAddr.setText(get('debug', 'addr', '0x20000000'))
        self.edtMemSize.setText(get('debug', 'size', '256'))

        self.btnDebug.setChecked(get('window', 'debug', 'no') == 'yes')
        self.btnLog.setChecked(get('window', 'log', 'no') == 'yes')

        self.update_debug_state()

        try:
            self.resize(max(int(get('window', 'width', '0')), self.width()), self.height())
        except ValueError:
            pass

    def parse_devices(self):
        try:
            for line in open(os.path.join(APP_DIR, 'devices.txt'), encoding='utf-8'):
                match = re.match(r'(\w+)\s+(.+)', line)
                if match:
                    name = match.group(1)
                    addr = 0x20000000
                    size = 0x1000
                    path = os.path.join(APP_DIR, match.group(2).strip())
                    device.Devices[name] = (name, addr, size, path)

                match = re.match(r'(\w+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(.+)', line)
                if match:
                    name = match.group(1)
                    addr = int(match.group(2), 16)
                    size = int(match.group(3), 16)
                    path = os.path.join(APP_DIR, match.group(4).strip())
                    device.Devices[name] = (name, addr, size, path)

        except Exception as e:
            print(f'解析 devices.txt 失败：{e}')

        return device.Devices.keys()

    ''' 日志 '''

    def on_log(self, text):
        self.logbuff += text

        while '\n' in self.logbuff:
            line, self.logbuff = self.logbuff.split('\n', 1)
            if line.strip():
                self.append_log(line.rstrip())

    def append_log(self, line):
        color = ''
        if re.search(r'error|fail|Traceback|失败|错误', line, re.I):
            color = '#c0392b'
        elif re.search(r'\bOK\b|完成', line, re.I):
            color = '#27ae60'

        text = html.escape(line)
        if color:
            text = f'<span style="color:{color}">{text}</span>'

        self.txtLog.appendHtml(f'<span style="color:#95a5a6">{time.strftime("%H:%M:%S")}</span>  {text}')

    ''' 连接 '''

    def device(self, name, xlink):
        dev = device.Devices[name]

        if isinstance(dev, tuple):
            return device.chip.Chip(xlink, dev)

        else:
            return dev(xlink)

    def on_tmrDAP_timeout(self):
        ''' 每秒扫描一次插上的探测器：CMSIS-DAP 走 pyocd，ST-Link 走 stlink.py '''
        if self.busy or self.linked:    # link working
            return

        probes = []     # [(显示名, ('dap'|'stlink', 唯一标识)), ...]

        try:
            from pyocd.probe import aggregator
            self.daplinks = aggregator.DebugProbeAggregator.get_all_connected_probes()

            self.probeerror.pop('dap', None)

        except Exception as e:
            self.daplinks = []
            self.report_probe_error('dap', f'枚举 CMSIS-DAP 调试器失败：{e}')

        for daplink in self.daplinks:
            probes.append((f'{daplink.product_name} ({daplink.unique_id})', ('dap', daplink.unique_id)))

        try:
            import stlink

            for serial, name in stlink.STLink.get_all_connected():
                probes.append((f'{name} ({serial})' if serial else name, ('stlink', serial)))

            self.probeerror.pop('stlink', None)

        except Exception as e:
            self.report_probe_error('stlink', f'枚举 ST-Link 失败：{e}')

        if [data for name, data in probes] != [self.cmbDLL.itemData(i) for i in range(2, self.cmbDLL.count())]:
            select = self.cmbDLL.currentText()

            for i in range(2, self.cmbDLL.count()):
                self.cmbDLL.removeItem(2)

            for name, data in probes:
                self.cmbDLL.addItem(name, data)

            self.cmbDLL.setCurrentIndex(zero_if(self.cmbDLL.findText(select)))

    def report_probe_error(self, kind, message):
        if self.probeerror.get(kind) != message:    # 同一个错误只报一次，避免每秒刷屏
            self.probeerror[kind] = message
            print(message)

    def link_open(self, algo=True):
        ''' algo 为 True 时顺便把烧写算法下载到目标 RAM（会复位并 halt 住核心），
            纯调试用途传 False，只建立连接，不动目标程序 '''
        try:
            if not self.linked:
                self.xlk = xlink.XLink(self.probe_open())

                self.linked = True

                try:
                    print(f'Connected: {self.xlk.read_core_type()} @ {self.cmbSpeed.currentText()}')

                    self.print_chip_id()

                except Exception as e:
                    print(f'读取芯片信息失败：{e}')

            if algo:
                self.dev = self.device(self.cmbMCU.currentText(), self.xlk)

        except Exception as e:
            print(f'连接失败：{e}')
            QMessageBox.critical(self, '连接失败', str(e), QMessageBox.Yes)

            self.link_close(force=True)

            return False

        self.update_debug_state()

        return True

    def probe_open(self):
        ''' 按下拉框里选中的探测器建立底层连接 '''
        mode = self.cmbMode.currentText()
        mode = mode.replace('RISC-V', 'RV').replace(' SWD', '').replace(' cJTAG', '').replace(' JTAG', 'J').lower()
        core = self.device(self.cmbMCU.currentText(), None).CHIP_CORE
        speed= int(self.cmbSpeed.currentText().split()[0]) * 1000 # KHz

        item_data = self.cmbDLL.currentData()

        if item_data == 'jlink':
            return jlink.JLink(self.cmbDLL.currentText(), mode, core, speed)

        if item_data == 'openocd':
            import openocd
            return openocd.OpenOCD(mode=mode, core=core, speed=speed)

        kind, uid = item_data

        if kind == 'stlink':
            import stlink
            return stlink.STLink(uid, mode, core, speed)

        from pyocd.coresight import dap, ap, cortex_m
        from pyocd.probe.debug_probe import DebugProbe

        if mode.startswith('rv'):
            raise Exception('CMSIS-DAP 方式暂不支持 RISC-V 目标，请改用 J-Link 或 OpenOCD')

        daplink = next((probe for probe in self.daplinks if probe.unique_id == uid), None)
        if daplink is None:
            raise Exception(f'找不到调试器 {uid}，请重新插拔后再试')

        daplink.open()

        _dp = dap.DebugPort(daplink, None)
        _dp.init(DebugProbe.Protocol.JTAG if mode == 'armj' else DebugProbe.Protocol.SWD)
        _dp.power_up_debug()
        _dp.set_clock(speed * 1000)

        _ap = ap.AHB_AP(_dp, 0)
        _ap.init()

        return cortex_m.CortexM(None, _ap)

    ''' STM32 及其兼容芯片（GD32、AT32 等）DBGMCU_IDCODE 低 12 位的型号编码 '''
    DEV_IDS = {
        0x410: 'STM32F101/102/103 中容量',  0x412: 'STM32F10x 小容量',
        0x414: 'STM32F10x 大容量',          0x418: 'STM32F105/107',
        0x420: 'STM32F100 中容量',          0x428: 'STM32F100 大容量',
        0x430: 'STM32F10x 超大容量',        0x411: 'STM32F2xx',
        0x413: 'STM32F405/407/415/417',     0x419: 'STM32F42x/43x',
        0x421: 'STM32F446',                 0x423: 'STM32F401xB/C',
        0x431: 'STM32F411',                 0x433: 'STM32F401xD/E',
        0x434: 'STM32F469/479',             0x441: 'STM32F412',
        0x458: 'STM32F410',                 0x463: 'STM32F413/423',
        0x449: 'STM32F74x/75x',             0x451: 'STM32F76x/77x',
        0x452: 'STM32F72x/73x',             0x440: 'STM32F030x8/F05x',
        0x442: 'STM32F09x',                 0x444: 'STM32F03x',
        0x445: 'STM32F04x',                 0x448: 'STM32F07x',
        0x415: 'STM32L4x1/475/476/486',     0x435: 'STM32L43x/44x',
        0x462: 'STM32L45x/46x',             0x464: 'STM32L41x/42x',
        0x416: 'STM32L1xx',                 0x417: 'STM32L0xx',
        0x450: 'STM32H742/743/750/753',     0x480: 'STM32H7A3/7B3',
        0x483: 'STM32H72x/73x',             0x460: 'STM32G07x/G08x',
        0x466: 'STM32G03x/G04x',            0x468: 'STM32G431/441',
        0x469: 'STM32G47x/48x',             0x479: 'STM32G491/4A1',
        0x482: 'STM32U575/585',             0x495: 'STM32WB55',
        0x497: 'STM32WLE5',
    }

    def print_chip_id(self):
        ''' 读 DBGMCU_IDCODE。这是 ST 系（含 GD32/AT32 兼容芯片）才有的寄存器，
            读不到就算了，只作提示用，不拿来拦截操作 '''
        bases = [0xE0042000]
        if self.xlk.read_core_type() in ('Cortex-M0', 'Cortex-M0+'):
            bases.append(0x40015800)    # M0 系列的 DBGMCU 挂在外设总线上

        for base in bases:
            try:
                idcode = self.xlk.read_U32(base)
            except Exception:
                continue

            dev_id, rev = idcode & 0xFFF, (idcode >> 16) & 0xFFFF
            if dev_id in (0x000, 0xFFF):
                continue

            name = self.DEV_IDS.get(dev_id, '未知型号')

            print(f'DBGMCU_IDCODE 0x{idcode:08X}：DEV_ID 0x{dev_id:03X}（{name}），REV 0x{rev:04X}')

            return dev_id

        print('未读到 DBGMCU_IDCODE，这颗芯片可能没有这个寄存器')

        return None

    def link_close(self, force=False):
        if self.btnConnect.isChecked() and not force:
            return              # 调试面板按住了连接，任务结束后不断开

        if not self.linked:
            return

        try:
            if self.chkRun.isChecked():
                self.xlk.reset()

            self.xlk.close()

        except Exception as e:
            print(f'断开连接出错：{e}')

        self.linked = False

        self.update_debug_state()

    ''' 任务调度 '''

    def start_task(self, name, func, *args, finished=None):
        if self.busy or not self.link_open():
            return

        self.task = name
        self.time_start = time.time()
        self.time_paint = 0

        self.set_busy(True)

        self.thread = ThreadAsync(func, *args)
        self.dev.abort = False      # 上一次点过停止的话，标志不能留到这一次
        self.dev.progress = self.thread.taskProgress.emit
        self.thread.taskProgress.connect(self.on_progress)
        self.thread.taskFinished.connect(finished or self.on_task_finished)
        self.thread.start()

        self.tmrTime.start()

    def set_busy(self, busy):
        self.busy = busy

        for widget in (self.grpTarget, self.grpLink, self.grpFile, self.chkVerify, self.chkRun,
                       self.btnChipErase, self.btnErase, self.btnWrite, self.btnRead):
            widget.setEnabled(not busy)

        self.btnStop.setVisible(busy)
        self.btnStop.setEnabled(busy)

        if not busy:
            self.tmrTime.stop()
            self.prgInfo.setMaximum(100)
            self.setWindowTitle('MCU Programmer')

        self.update_debug_state()

    def on_progress(self, done, total, message):
        now = time.time()
        if total and done not in (0, total) and now - self.time_paint < 0.1:
            return          # 限制刷新频率，避免烧写大文件时界面卡顿
        self.time_paint = now

        if total:
            self.prgInfo.setMaximum(100)
            self.prgInfo.setValue(done * 100 // total)
        else:
            self.prgInfo.setMaximum(0)      # 进度未知，显示滚动条

        self.lblStatus.setText(f'{self.task}  {message}')

    def on_tmrTime_timeout(self):
        self.setWindowTitle(f'MCU Programmer - {self.task}中 {time.time() - self.time_start:.0f} s')

    def task_summary(self):
        elapsed = time.time() - self.time_start

        summary = f'{self.task}完成，耗时 {elapsed:.1f} s'
        if self.task_bytes and elapsed > 0:
            summary += f'，{self.task_bytes / 1024 / elapsed:.1f} KB/s'

        return summary

    def on_task_finished(self, error):
        self.link_close()

        self.set_busy(False)

        if getattr(self.thread, 'aborted', False):
            self.prgInfo.setValue(0)
            self.lblStatus.setText(f'{self.task}已中断')
            print(f'{self.task}已中断')

        elif error:
            self.prgInfo.setValue(0)
            self.lblStatus.setText(f'{self.task}失败')
            QMessageBox.critical(self, f'{self.task}失败', error, QMessageBox.Yes)

        else:
            self.prgInfo.setValue(100)
            self.lblStatus.setText(self.task_summary())
            print(self.task_summary())

        self.task_bytes = 0

    @pyqtSlot()
    def on_btnStop_clicked(self):
        if not self.busy:
            return

        self.dev.abort = True       # 算法执行到下一个安全点就会抛 Aborted 退出来

        self.btnStop.setEnabled(False)
        self.lblStatus.setText(f'{self.task}  正在停止…')

    ''' 擦除 '''

    @pyqtSlot()
    def on_btnChipErase_clicked(self):
        if QMessageBox.question(self, '整片擦除', '将擦除整片 Flash 的全部内容，确定继续？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        self.start_task('整片擦除', lambda: self.dev.chip_erase())

    @pyqtSlot()
    def on_btnErase_clicked(self):
        addr, size = self.addr, self.size

        self.start_task('擦除', lambda: self.dev.sect_erase(addr, size))

    ''' 烧写 '''

    @pyqtSlot()
    def on_btnWrite_clicked(self):
        try:
            self.wrdata = self.load_write_data()
        except Exception as e:
            QMessageBox.warning(self, '文件错误', str(e), QMessageBox.Yes)
            return

        if not self.wrdata:
            QMessageBox.warning(self, '没有文件', '没有勾选任何要烧写的文件', QMessageBox.Yes)
            return

        self.task_bytes = sum(len(data) for addr, data in self.wrdata)

        self.start_task('烧写', self.write_all)

    def load_write_data(self):
        ''' 连接目标板之前就把全部文件读进内存，避免烧到一半才发现文件有问题 '''
        fpath = self.cmbHEX.currentText().strip()
        if not fpath:
            raise Exception('请先选择要烧写的程序文件')
        if not os.path.exists(fpath):
            raise Exception(f'文件不存在：{fpath}')

        read = lambda path: parseHex(path) if path.lower().endswith('.hex') else open(path, 'rb').read()

        if not fpath.lower().endswith('.ini'):
            wrdata = [(os.path.basename(fpath), self.addr, read(fpath))]

        else:
            base = self.device(self.cmbMCU.currentText(), None).CHIP_BASE

            wrdata = []
            for i in range(self.table.rowCount()):
                if self.table.item(i, 0).checkState() != QtCore.Qt.Checked:
                    continue

                name = self.table.item(i, 0).text()

                path = self.table.item(i, 2).text().strip()
                if not path:
                    raise Exception(f'[{name}] 还没有选择文件')
                if not os.path.exists(path):
                    raise Exception(f'[{name}] 文件不存在：{path}')

                try:
                    addr = int(self.table.item(i, 1).text(), 16) - base
                except ValueError:
                    raise Exception(f'[{name}] 地址格式错误：{self.table.item(i, 1).text()}')

                wrdata.append((name, addr, read(path)))

        self.check_write_data(wrdata)

        return [(addr, data) for name, addr, data in wrdata]

    def check_write_data(self, wrdata):
        ''' Bootloader + App 这类分区烧写最容易踩的几个坑：地址越界、没有按扇区对齐、
            两个文件落在同一个扇区里（擦后一个的时候会把前一个擦掉） '''
        dev = self.device(self.cmbMCU.currentText(), None)

        sect = lambda addr: addr // dev.SECT_SIZE   # 地址所在的扇区号（相对 CHIP_BASE）

        used = []
        for name, addr, data in wrdata:
            if not 0 <= addr < dev.CHIP_SIZE:
                raise Exception(f'[{name}] 地址 0x{dev.CHIP_BASE + addr:08X} 不在 Flash 范围内\n'
                                f'（0x{dev.CHIP_BASE:08X} ~ 0x{dev.CHIP_BASE + dev.CHIP_SIZE - 1:08X}）')

            if addr % dev.SECT_SIZE:
                raise Exception(f'[{name}] 地址 0x{dev.CHIP_BASE + addr:08X} 没有按扇区对齐\n'
                                f'扇区大小为 {dev.SECT_SIZE} 字节，烧写地址必须是它的整数倍')

            if addr + len(data) > dev.CHIP_SIZE:
                raise Exception(f'[{name}] {len(data)} 字节从 0x{dev.CHIP_BASE + addr:08X} 写入会超出 Flash 末尾')

            first, last = sect(addr), sect(addr + len(data) - 1)

            for name_, first_, last_ in used:
                if first <= last_ and first_ <= last:
                    raise Exception(f'[{name}] 与 [{name_}] 占用了相同的扇区，\n'
                                    f'烧写靠后的一个时会把前一个擦掉，请调整地址')

            used.append((name, first, last))

    def write_all(self):
        verify = self.chkVerify.isChecked()
        report = self.thread.taskProgress.emit

        for i, (addr, data) in enumerate(self.wrdata):
            print(f'--- 烧写 {i+1}/{len(self.wrdata)}：0x{self.dev.CHIP_BASE + addr:08X}, {len(data)} bytes ---')

            prefix = f'[{i+1}/{len(self.wrdata)}] ' if len(self.wrdata) > 1 else ''
            self.dev.progress = lambda done, total, message, prefix=prefix: report(done, total, prefix + message)

            self.dev.chip_write(addr, data, verify)

    ''' 读取 '''

    @pyqtSlot()
    def on_btnRead_clicked(self):
        addr, size = self.addr, self.size

        self.rdbuff = []    # bytes 无法 extend，因此用 list
        self.task_bytes = size

        self.start_task('读取', lambda: self.dev.chip_read(addr, size, self.rdbuff), finished=self.on_btnRead_finished)

    def on_btnRead_finished(self, error):
        if not error:
            binpath, filter = QFileDialog.getSaveFileName(self, '将读取到的数据保存到文件', filter='程序文件 (*.bin)', directory=self.savPath)
            if binpath:
                self.savPath = binpath
                with open(binpath, 'wb') as f:
                    f.write(bytes(self.rdbuff))
                print(f'已保存到 {binpath}')
            else:
                print('已取消保存')

        self.on_task_finished(error)

    ''' 自动识别型号 '''

    @pyqtSlot()
    def on_btnDetect_clicked(self):
        if not self.link_open(algo=False):      # 只连上，不下载算法、不复位目标
            return

        try:
            info = chipid.identify(self.xlk)
        except Exception as e:
            print(f'识别失败：{e}')
            QMessageBox.critical(self, '识别失败', str(e), QMessageBox.Yes)
            self.link_close()
            return

        print(f'识别结果：{chipid.describe(info)}')

        self.link_close()

        if not info['dev_id']:
            QMessageBox.information(self, '没认出来', chipid.describe(info) +
                                    '\n\n只有 STM32 以及兼容它的芯片（GD32、AT32 等）才有这组 ID 寄存器。',
                                    QMessageBox.Yes)
            return

        if not info['flash_kb']:
            QMessageBox.warning(self, '识别完成',
                                f'{chipid.describe(info)}\n\n没读到 Flash 容量，无法自动挑算法，请手动选型号。',
                                QMessageBox.Yes)
            return

        match, others = self.match_device(info)
        if match:
            self.cmbMCU.setCurrentIndex(self.cmbMCU.findText(match))

            QMessageBox.information(self, '识别完成',
                                    f'{chipid.describe(info)}\n\n已自动选中列表里的 {match}',
                                    QMessageBox.Yes)
            return

        hint = ''
        if others:
            hint = (f'\n\n（列表里的 {"、".join(others)} 容量一样，但不是 {info["family"]} 系列，'
                    f'算法不通用，不能拿来顶替。）')

        if QMessageBox.question(self, '本地没有对应的算法',
                                f'{chipid.describe(info)}\n\n'
                                f'devices.txt 里没有属于 {info["family"]} 系列、'
                                f'Flash 为 0x08000000 + {info["flash_kb"]} KB 的词条。{hint}\n\n'
                                f'要不要从 Keil 的 CMSIS-Pack 服务器下载对应的烧写算法并加进去？',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return

        self.fetch_device(info)

    def match_device(self, info):
        ''' 在已有词条里找能用的算法。

            只比 Flash 起址和容量是不够的：STM32F103RC 和 STM32F407VE 都是 0x08000000 + 512 KB，
            但分属 F1 和 F4，算法完全不通用，认错了会直接擦坏芯片。所以容量对上之后，
            还要求词条名属于识别出来的那个系列。

            返回 (确定能用的词条, 容量一样但系列不符的词条列表) '''
        prefix = chipid.FAMILIES[info['family']][4].upper()

        same_size = []
        for name in list(device.Devices):
            try:
                dev = self.device(name, None)
            except Exception:
                continue

            if dev.CHIP_BASE == 0x08000000 and dev.CHIP_SIZE == info['flash_kb'] * 1024:
                same_size.append(name)

        matched = [name for name in same_size if name.upper().startswith(prefix)]
        others  = [name for name in same_size if name not in matched]

        ''' 同系列里再挑型号号段也对得上的，比如 DEV_ID 0x413 认出的是 405/407/415/417，
            列表里同时有 STM32F411CE 和 STM32F407VET6 时应该选后者 '''
        for part in chipid.part_prefixes(info):
            exact = [name for name in matched if name.upper().startswith(part)]
            if exact:
                return exact[0], others

        return (matched[0] if matched else None), others

    def fetch_device(self, info):
        import cmsispack

        pack   = info['pack']
        prefix = chipid.FAMILIES[info['family']][4]

        QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            found = cmsispack.download_algorithm(pack, prefix, info['flash_kb'],
                                                 os.path.join(APP_DIR, 'FlashAlgo'), progress=print)
        except Exception as e:
            print(f'下载算法失败：{e}')
            QMessageBox.critical(self, '下载失败', str(e), QMessageBox.Yes)
            return
        finally:
            QApplication.restoreOverrideCursor()

        name = found['name']
        while name in device.Devices:       # 名字撞了就加后缀，不动已有词条
            name += '_'

        line = (f'{name}\t0x{found["ram_start"]:08X}\t0x{found["ram_size"]:X}\t'
                f'FlashAlgo/{os.path.basename(found["path"])}')

        try:
            with open(os.path.join(APP_DIR, 'devices.txt'), 'a', encoding='utf-8') as f:
                f.write(('' if self.devices_txt_ends_with_newline() else '\n') + line + '\n')
        except Exception as e:
            print(f'写入 devices.txt 失败：{e}')
            QMessageBox.critical(self, '写入失败', str(e), QMessageBox.Yes)
            return

        print(f'已加入 devices.txt：{line}')

        self.reload_devices(name)

        QMessageBox.information(self, '已添加',
                                f'{chipid.describe(info)}\n\n已添加型号 {name} 并选中。\n\n'
                                f'算法：{os.path.basename(found["path"])}\n'
                                f'RAM ：0x{found["ram_start"]:08X} / {found["ram_size"]//1024} KB',
                                QMessageBox.Yes)

    def devices_txt_ends_with_newline(self):
        try:
            with open(os.path.join(APP_DIR, 'devices.txt'), 'rb') as f:
                f.seek(-1, os.SEEK_END)
                return f.read(1) in (b'\n', b'\r')
        except Exception:
            return True

    def reload_devices(self, select):
        self.parse_devices()

        self.cmbMCU.blockSignals(True)
        self.cmbMCU.clear()
        self.cmbMCU.addItems(device.Devices.keys())
        self.cmbMCU.blockSignals(False)

        self.cmbMCU.setCurrentIndex(zero_if(self.cmbMCU.findText(select)))
        self.on_cmbMCU_currentIndexChanged(self.cmbMCU.currentIndex())

    ''' 地址、大小 '''

    @property
    def addr(self):
        return int(self.cmbAddr.currentText().split()[0]) * 1024

    @property
    def size(self):
        return int(self.cmbSize.currentText().split()[0]) * 1024

    @pyqtSlot(int)
    def on_cmbMCU_currentIndexChanged(self, index):
        try:
            dev = self.device(self.cmbMCU.currentText(), None)
        except Exception as e:
            self.setChipInfo('烧写算法加载失败')
            print(f'加载 {self.cmbMCU.currentText()} 的烧写算法失败：{e}')
            return

        sect = f'{dev.SECT_SIZE//1024} K' if dev.SECT_SIZE >= 1024 else f'{dev.SECT_SIZE} B'
        info = f'0x{dev.CHIP_BASE:08X} + {dev.CHIP_SIZE//1024} K    扇区 {sect}    页 {dev.PAGE_SIZE} B'

        name = dev.falgo.get('device_name', '')     # FLM 里自带的算法名，用来核对选型
        self.setChipInfo(f'{info}    {name}' if name else info)

        addr = self.cmbAddr.currentText()

        self.cmbAddr.clear()
        for i in range(dev.SECT_SKIP // dev.SECT_SIZE, dev.CHIP_SIZE // dev.SECT_SIZE):
            if (dev.SECT_SIZE * i) % 1024 == 0:
                self.cmbAddr.addItem('%d K' %(dev.SECT_SIZE * i     // 1024))

        self.cmbAddr.setCurrentIndex(zero_if(self.cmbAddr.findText(addr)))

        self.btnChipErase.setEnabled(dev.falgo['pc_EraseChip'] <= 0xFFFFFFFF)

    def setChipInfo(self, text):
        self.chipinfo = text

        self.lblChipInfo.setToolTip(text)
        self.lblChipInfo.setText(self.lblChipInfo.fontMetrics().elidedText(text, QtCore.Qt.ElideRight, max(self.lblChipInfo.width() - 8, 0)))

    def resizeEvent(self, evt):
        super(MCUProg, self).resizeEvent(evt)

        if self.chipinfo:
            self.setChipInfo(self.chipinfo)

    @pyqtSlot(int)
    def on_cmbAddr_currentIndexChanged(self, index):
        if self.cmbAddr.currentText() == '': return

        dev = self.device(self.cmbMCU.currentText(), None)

        size = self.cmbSize.currentText()

        self.cmbSize.clear()
        for i in range((dev.CHIP_SIZE - self.addr) // dev.SECT_SIZE):
            if (dev.SECT_SIZE * (i+1)) % 1024 == 0:
                self.cmbSize.addItem('%d K' %(dev.SECT_SIZE * (i+1) // 1024))

        self.cmbSize.setCurrentIndex(zero_if(self.cmbSize.findText(size)))

    ''' 文件选择 '''

    @pyqtSlot()
    def on_btnDLL_clicked(self):
        dllpath, filter = QFileDialog.getOpenFileName(self, 'JLink_x64.dll 路径', filter='动态链接库 (*.dll *.so)', directory=self.cmbDLL.itemText(0))
        if dllpath:
            self.cmbDLL.setItemText(0, dllpath)

    @pyqtSlot()
    def on_btnHEX_clicked(self):
        hexpath, filter = QFileDialog.getOpenFileName(self, '程序文件路径', filter='程序文件 (*.bin *.hex *.ini);;任意文件 (*.*)', directory=self.cmbHEX.currentText())
        if hexpath:
            self.setHexPath(hexpath)

    def setHexPath(self, hexpath):
        index = self.cmbHEX.findText(hexpath)
        if index != -1:
            self.cmbHEX.removeItem(index)

        self.cmbHEX.insertItem(0, hexpath)
        self.cmbHEX.setCurrentIndex(0)

    @pyqtSlot(str)
    def on_cmbHEX_currentIndexChanged(self, text):
        self.cmbHEX.setToolTip(text)

        if not text.lower().endswith('.ini'):
            self.showList(False)
            return

        conf = configparser.ConfigParser(interpolation=None)
        try:
            conf.read(text, encoding='utf-8')
        except Exception as e:
            print(f'读取 {text} 失败：{e}')
            return

        self.loadList([(section, conf.get(section, 'addr', fallback='0x0'), conf.get(section, 'path', fallback=''))
                       for section in conf.sections()])

    ''' Bootloader + App 烧写列表 '''

    @pyqtSlot()
    def on_btnNewList_clicked(self):
        ''' 新建一个 Bootloader + App 列表，地址先给个合理的默认值，再由用户调整 '''
        dev = self.device(self.cmbMCU.currentText(), None)

        boot = min(max(dev.SECT_SIZE, 32 * 1024), dev.CHIP_SIZE // 2)
        boot = boot - boot % dev.SECT_SIZE

        inipath, filter = QFileDialog.getSaveFileName(self, '新建 Bootloader + App 烧写列表', filter='烧写列表 (*.ini)', directory=self.cmbHEX.currentText())
        if not inipath:
            return

        if not inipath.lower().endswith('.ini'):
            inipath += '.ini'

        self.inipath = inipath
        self.saveList([('BOOT', f'0x{dev.CHIP_BASE:08X}', ''),
                       ('APP',  f'0x{dev.CHIP_BASE + boot:08X}', '')])

        self.setHexPath(inipath)

        QMessageBox.information(self, '已新建烧写列表',
                                f'BOOT 从 0x{dev.CHIP_BASE:08X} 开始，APP 从 0x{dev.CHIP_BASE + boot:08X} 开始。\n\n'
                                f'双击「文件」选择各自的 bin/hex，双击「地址」按实际分区修改。',
                                QMessageBox.Yes)

    @pyqtSlot()
    def on_btnAddRow_clicked(self):
        dev = self.device(self.cmbMCU.currentText(), None)

        addr = dev.CHIP_BASE
        for i in range(self.table.rowCount()):       # 默认接在已有条目的后面
            try:
                addr = max(addr, int(self.table.item(i, 1).text(), 16) + dev.SECT_SIZE)
            except ValueError:
                pass

        self.loadList(self.listRows() + [(f'FILE{self.table.rowCount() + 1}', f'0x{addr:08X}', '')])

        self.saveList()

    @pyqtSlot()
    def on_btnDelRow_clicked(self):
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, '删除条目', '请先选中要删除的行', QMessageBox.Yes)
            return

        self.loadList([row for i, row in enumerate(self.listRows()) if i not in rows])

        self.saveList()

    def listRows(self):
        return [(self.table.item(i, 0).text(), self.table.item(i, 1).text(), self.table.item(i, 2).text())
                for i in range(self.table.rowCount())]

    def loadList(self, rows):
        self.loading = True     # 填表过程中不要触发 itemChanged 回写文件

        self.table.setRowCount(len(rows))
        for i, (name, addr, path) in enumerate(rows):
            item = QtWidgets.QTableWidgetItem(name)
            item.setCheckState(QtCore.Qt.Checked)
            self.table.setItem(i, 0, item)

            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(addr))

            item = QtWidgets.QTableWidgetItem(path)
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)     # 双击「文件」是弹文件对话框，不是就地编辑
            item.setToolTip(self.pathTip(path))
            self.table.setItem(i, 2, item)

        self.loading = False

        self.showList(True)

    def saveList(self, rows=None):
        if not self.inipath:
            return

        conf = configparser.ConfigParser(interpolation=None)
        for name, addr, path in (self.listRows() if rows is None else rows):
            conf.add_section(name)
            conf.set(name, 'addr', addr)
            conf.set(name, 'path', path)

        try:
            with open(self.inipath, 'w', encoding='utf-8') as f:
                conf.write(f)
        except Exception as e:
            print(f'保存 {self.inipath} 失败：{e}')

    def showList(self, visible):
        self.inipath = self.cmbHEX.currentText() if visible else ''

        for widget in (self.table, self.lblListHint, self.btnAddRow, self.btnDelRow):
            widget.setVisible(visible)

        if visible:
            height = self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
            for i in range(self.table.rowCount()):
                height += self.table.rowHeight(i)
            self.table.setFixedHeight(min(height + 2, 150))

        self.adjust_height()

    def pathTip(self, path):
        if not path.strip():
            return '双击选择文件'

        if not os.path.exists(path):
            return f'{path}\n文件不存在'

        return f'{path}\n{os.path.getsize(path)} 字节'

    @pyqtSlot(QtWidgets.QTableWidgetItem)
    def on_table_itemChanged(self, item):
        if self.loading:
            return

        if item.column() == 1:      # 地址：统一整理成 0xXXXXXXXX
            self.loading = True
            try:
                item.setText(f'0x{int(item.text(), 16):08X}')
                item.setForeground(QtGui.QBrush())
            except ValueError:
                item.setForeground(QtGui.QBrush(QtGui.QColor('#c0392b')))
            self.loading = False

        self.saveList()

    @pyqtSlot(int, int)
    def on_table_cellDoubleClicked(self, row, column):
        if column != 2: # 名称、地址是就地编辑，只有文件路径需要弹对话框
            return

        hexpath, filter = QFileDialog.getOpenFileName(self, '程序文件路径', filter='程序文件 (*.bin *.hex);;任意文件 (*.*)', directory=self.table.item(row, column).text())
        if hexpath:
            self.loading = True
            self.table.item(row, column).setText(hexpath)
            self.table.item(row, column).setToolTip(self.pathTip(hexpath))
            self.loading = False

            self.saveList()

    ''' 拖放 '''

    def dragEnterEvent(self, evt):
        if self.busy or not evt.mimeData().hasUrls():
            return

        if os.path.splitext(evt.mimeData().urls()[0].toLocalFile())[1].lower() in ('.bin', '.hex', '.ini'):
            evt.acceptProposedAction()

    def dropEvent(self, evt):
        self.setHexPath(evt.mimeData().urls()[0].toLocalFile())

    ''' 调试面板 '''

    @pyqtSlot(bool)
    def on_btnDebug_toggled(self, checked):
        self.grpDebug.setVisible(checked)

        self.adjust_height()

    @pyqtSlot(bool)
    def on_btnConnect_toggled(self, checked):
        if checked:
            if not self.link_open(algo=False):
                self.btnConnect.setChecked(False)
                return

            self.refresh_regs()

        else:
            self.link_close(force=True)

    def update_debug_state(self):
        ''' 按连接状态刷新调试面板。btnConnect 表示的是「用户要求保持连接」，
            烧写任务自己开的连接不能把它勾上，否则任务结束后就断不开了 '''
        if self.btnConnect.isChecked() and not self.linked:     # 连接掉了，把按钮弹回来
            self.btnConnect.blockSignals(True)
            self.btnConnect.setChecked(False)
            self.btnConnect.blockSignals(False)

        self.btnConnect.setText('断开' if self.btnConnect.isChecked() else '连接')

        for widget in (self.btnReset, self.btnHalt, self.btnGo, self.btnStep,
                       self.btnMemRead, self.btnMemWrite):
            widget.setEnabled(self.linked and self.btnConnect.isChecked() and not self.busy)

        self.btnConnect.setEnabled(not self.busy)

        if not self.linked:
            self.lblDebug.setText('未连接')
            self.tblRegs.setRowCount(0)
            return

        try:
            self.lblDebug.setText(f'已连接 · {self.xlk.read_core_type()} · ' +
                                  ('已暂停' if self.xlk.halted() else '运行中'))
        except Exception as e:
            self.lblDebug.setText(f'已连接（读取状态失败：{e}）')

    def debug_action(self, name, func):
        if not self.linked:
            return

        try:
            func()
        except Exception as e:
            print(f'{name}失败：{e}')
            QMessageBox.critical(self, f'{name}失败', str(e), QMessageBox.Yes)
            return

        print(f'{name} OK')

        self.update_debug_state()
        self.refresh_regs()

    @pyqtSlot()
    def on_btnReset_clicked(self):
        self.debug_action('复位', self.xlk.reset_and_halt if self.chkHaltOnReset() else self.xlk.reset)

    def chkHaltOnReset(self):
        return QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.ShiftModifier

    @pyqtSlot()
    def on_btnHalt_clicked(self):
        self.debug_action('暂停', self.xlk.halt)

    @pyqtSlot()
    def on_btnGo_clicked(self):
        self.debug_action('运行', self.xlk.go)

    @pyqtSlot()
    def on_btnStep_clicked(self):
        self.debug_action('单步', self.xlk.step)

    ARM_REGS = [f'r{i}' for i in range(13)] + ['sp', 'lr', 'pc', 'xpsr']
    RV_REGS  = ['ra', 'sp', 'gp', 'tp', 's0', 'a0', 'a1', 'a2', 'a3', 'pc']

    def refresh_regs(self):
        if not self.linked:
            self.tblRegs.setRowCount(0)
            return

        names = self.ARM_REGS if self.xlk.mode.startswith('arm') else self.RV_REGS

        halted = False
        try:
            halted = self.xlk.halted()
        except Exception:
            pass

        self.tblRegs.setRowCount(len(names))
        for i, name in enumerate(names):
            self.tblRegs.setItem(i, 0, QtWidgets.QTableWidgetItem(name.upper()))

            try:
                value = f'0x{self.xlk.read_reg(name):08X}' if halted else '—'
            except Exception:
                value = '?'

            self.tblRegs.setItem(i, 1, QtWidgets.QTableWidgetItem(value))

        self.tblRegs.setColumnWidth(0, 80)

    @pyqtSlot()
    def on_btnMemRead_clicked(self):
        try:
            addr = int(self.edtMemAddr.text().strip(), 16)
            size = int(self.edtMemSize.text().strip(), 0)
        except ValueError:
            QMessageBox.warning(self, '格式错误', '地址请用十六进制，长度请用十进制或 0x 开头', QMessageBox.Yes)
            return

        if not 0 < size <= 64 * 1024:
            QMessageBox.warning(self, '长度超范围', '一次最多读 64 KB', QMessageBox.Yes)
            return

        try:
            data = bytes(bytearray(self.xlk.read_mem_U8(addr, size)))
        except Exception as e:
            print(f'读内存失败：{e}')
            QMessageBox.critical(self, '读内存失败', str(e), QMessageBox.Yes)
            return

        self.txtMem.setPlainText(hexdump(addr, data))

    @pyqtSlot()
    def on_btnMemWrite_clicked(self):
        try:
            addr = int(self.edtMemAddr.text().strip(), 16)
        except ValueError:
            QMessageBox.warning(self, '格式错误', '地址请用十六进制', QMessageBox.Yes)
            return

        try:
            data = bytes.fromhex(re.sub(r'0x|[,\s]', ' ', self.edtMemData.text()).replace(' ', ''))
        except ValueError:
            QMessageBox.warning(self, '格式错误', '写入的数据请用十六进制字节，如 12 34 AB CD', QMessageBox.Yes)
            return

        if not data:
            QMessageBox.warning(self, '没有数据', '请先填写要写入的十六进制字节', QMessageBox.Yes)
            return

        try:
            self.xlk.write_mem_U8(addr, data)
        except Exception as e:
            print(f'写内存失败：{e}')
            QMessageBox.critical(self, '写内存失败', str(e), QMessageBox.Yes)
            return

        print(f'已向 0x{addr:08X} 写入 {len(data)} 字节')

        self.edtMemSize.setText(str(max(len(data), int(self.edtMemSize.text() or 0))))
        self.on_btnMemRead_clicked()

    ''' 日志面板 '''

    @pyqtSlot(bool)
    def on_btnLog_toggled(self, checked):
        self.txtLog.setVisible(checked)

        self.adjust_height()

    def adjust_height(self):
        self.layout().activate()

        self.resize(self.width(), self.sizeHint().height())

    ''' 退出 '''

    def closeEvent(self, evt):
        if self.busy:
            if QMessageBox.question(self, '正在操作', '擦除/烧写/读取尚未结束，确定退出？',
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                evt.ignore()
                return

        self.link_close(force=True)

        sys.stdout, sys.stderr = self.stdout, self.stderr

        self.conf.set('link',  'mode', self.cmbMode.currentText())
        self.conf.set('link',  'speed', self.cmbSpeed.currentText())
        self.conf.set('link',  'jlink', self.cmbDLL.itemText(0))
        self.conf.set('link',  'select', self.cmbDLL.currentText())

        self.conf.set('target', 'mcu', self.cmbMCU.currentText())
        self.conf.set('target', 'addr', self.cmbAddr.currentText())
        self.conf.set('target', 'size', self.cmbSize.currentText())
        self.conf.set('target', 'savpath', self.savPath)
        self.conf.set('target', 'verify', 'yes' if self.chkVerify.isChecked() else 'no')
        self.conf.set('target', 'run', 'yes' if self.chkRun.isChecked() else 'no')

        hexpath = [self.cmbHEX.currentText()] + [self.cmbHEX.itemText(i) for i in range(self.cmbHEX.count())]
        self.conf.set('target', 'hexpath', repr(list(collections.OrderedDict.fromkeys(hexpath))[:20]))    # 保留顺序去重

        self.conf.set('window', 'log', 'yes' if self.btnLog.isChecked() else 'no')
        self.conf.set('window', 'debug', 'yes' if self.btnDebug.isChecked() else 'no')
        self.conf.set('window', 'width', str(self.width()))

        self.conf.set('debug', 'addr', self.edtMemAddr.text())
        self.conf.set('debug', 'size', self.edtMemSize.text())

        try:
            with open(os.path.join(APP_DIR, 'setting.ini'), 'w', encoding='utf-8') as f:
                self.conf.write(f)
        except Exception as e:
            print(f'保存 setting.ini 失败：{e}')


class ThreadAsync(QThread):
    taskFinished = pyqtSignal(str)              # 空字符串表示成功，否则为出错信息
    taskProgress = pyqtSignal(int, int, str)    # done, total, message

    def __init__(self, func, *args):
        super(ThreadAsync, self).__init__()
        self.func = func
        self.args = args
        self.aborted = False

    def run(self):
        try:
            self.func(*self.args)

            self.taskFinished.emit('')

        except device.flash.Aborted as e:
            self.aborted = True

            self.taskFinished.emit(str(e))

        except Exception as e:
            traceback.print_exc()

            self.taskFinished.emit(str(e) or repr(e))


def hexdump(addr, data, width=16):
    ''' 把读到的内存排成 地址 | 十六进制 | 字符 三栏 '''
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i+width]

        hexs = ' '.join(f'{byte:02X}' for byte in chunk).ljust(width * 3 - 1)
        text = ''.join(chr(byte) if 0x20 <= byte < 0x7F else '.' for byte in chunk)

        lines.append(f'{addr + i:08X}  {hexs}  {text}')

    return '\n'.join(lines)


def parseHex(file):
    ''' 解析 .hex 文件，提取出程序代码，没有值的地方填充0xFF '''
    data = ''
    currentAddr = 0
    extSegAddr  = 0     # 扩展段地址
    for line in open(file, 'rb').readlines():
        line = line.strip()
        if len(line) == 0: continue

        len_ = int(line[1:3],16)
        addr = int(line[3:7],16) + extSegAddr
        type = int(line[7:9],16)
        if type == 0x00:
            if currentAddr != addr:
                if currentAddr != 0:
                    data += '\xFF' * (addr - currentAddr)
                currentAddr = addr
            for i in range(len_):
                data += chr(int(line[9+2*i:11+2*i], 16))
            currentAddr += len_
        elif type == 0x02:
            extSegAddr = int(line[9:9+4], 16) * 16
        elif type == 0x04:
            extSegAddr = int(line[9:9+4], 16) * 65536

    return data.encode('latin')


if __name__ == "__main__":
    QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)   # 高分屏下按缩放比例布局
    QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(QSS)

    mcu = MCUProg()
    mcu.show()

    app.exec()
