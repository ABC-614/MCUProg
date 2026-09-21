#! python3
''' ST-Link V2 / V2-1 / V3 驱动，对外接口与 jlink.JLink、openocd.OpenOCD 保持一致

    Windows 下需要探测器的 USB 接口绑定在 WinUSB/libusb 上：ST-Link V2-1 和 V3 用 ST 官方
    驱动通常就能直接打开，老的 ST-Link V2 如果打不开，用 Zadig 把它换成 WinUSB 即可。
'''
import time
import struct

import usb.core
import usb.util


class STLink(object):
    VENDOR = 0x0483

    ''' PID: (名称, OUT 端点) '''
    PRODUCTS = {
        0x3748: ('ST-Link V2',      0x02),
        0x374A: ('ST-Link V2-1',    0x01),
        0x374B: ('ST-Link V2-1',    0x01),
        0x374E: ('ST-Link V3E',     0x01),
        0x374F: ('ST-Link V3S',     0x01),
        0x3752: ('ST-Link V2-1',    0x01),
        0x3753: ('ST-Link V3',      0x01),
        0x3754: ('ST-Link V3',      0x01),
        0x3755: ('ST-Link V3',      0x01),
        0x3757: ('ST-Link V3',      0x01),
    }
    EP_IN = 0x81

    ''' 命令 '''
    GET_VERSION      = 0xF1
    GET_VERSION_EX   = 0xFB     # V3
    DEBUG_COMMAND    = 0xF2
    DFU_COMMAND      = 0xF3
    GET_CURRENT_MODE = 0xF5

    DFU_EXIT             = 0x07
    DEBUG_EXIT           = 0x21
    DEBUG_FORCEDEBUG     = 0x02
    DEBUG_RUNCORE        = 0x09
    DEBUG_STEPCORE       = 0x0A
    DEBUG_READMEM_32BIT  = 0x07
    DEBUG_WRITEMEM_32BIT = 0x08
    DEBUG_WRITEMEM_8BIT  = 0x0D

    APIV2_ENTER          = 0x30
    APIV2_READ_IDCODES   = 0x31
    APIV2_RESETSYS       = 0x32
    APIV2_READREG        = 0x33
    APIV2_WRITEREG       = 0x34
    APIV2_WRITEDEBUGREG  = 0x35
    APIV2_READDEBUGREG   = 0x36
    APIV2_READMEM_8BIT   = 0x0C
    APIV2_GETLASTRWSTATUS  = 0x3B
    APIV2_GETLASTRWSTATUS2 = 0x3E
    APIV2_DRIVE_NRST     = 0x3C
    APIV2_SWD_SET_FREQ   = 0x43
    APIV2_JTAG_SET_FREQ  = 0x44
    APIV3_SET_COM_FREQ   = 0x61
    APIV3_GET_COM_FREQ   = 0x62

    ENTER_SWD  = 0xA3
    ENTER_JTAG = 0x00

    MODE_DFU   = 0x00
    MODE_MASS  = 0x01
    MODE_DEBUG = 0x02
    MODE_SWIM  = 0x03

    DEBUG_OK    = 0x80
    DEBUG_FAULT = 0x81

    ERRORS = {
        0x04: 'JTAG 链读不出来',
        0x05: '没有检测到目标芯片',
        0x08: '命令出错',
        0x09: '读不到目标芯片的 IDCODE，请检查 SWD/JTAG 接线、目标板供电和复位脚',
        0x0A: 'DMA 出错',
        0x0B: '调试电源没有就绪',
        0x0C: '写目标出错',
        0x0D: '写校验出错',
        0x0E: 'AHB 总线出错',
        0x10: 'SWD AP 一直忙（WAIT）',
        0x11: 'SWD AP FAULT',
        0x12: 'SWD AP 出错',
        0x13: 'SWD AP 校验位出错',
        0x14: 'SWD DP 一直忙（WAIT）',
        0x15: 'SWD DP FAULT',
        0x16: 'SWD DP 出错',
        0x17: 'SWD DP 校验位出错',
        0x18: 'SWD AP WDATA 出错',
        0x19: 'SWD AP 粘滞错误',
        0x1A: 'SWD AP 被 STICKYORUN 锁住',
        0x81: '目标访问异常（DEBUG FAULT）',
    }

    ''' SWD 频率档位（kHz -> 分频索引），ST-Link V2 用 '''
    SWD_FREQ = [(4000, 0), (1800, 1), (1200, 2), (950, 3), (480, 7), (240, 15),
                (125, 31), (100, 40), (50, 79), (25, 158), (15, 265), (5, 798)]
    JTAG_FREQ = [(18000, 2), (9000, 4), (4500, 8), (2250, 16), (1120, 32),
                 (560, 64), (280, 128), (140, 256)]

    ''' 传输分片上限，取保守值以兼容老固件 '''
    MAX_RW_32 = 4096
    MAX_RW_8  = 64

    ''' Cortex-M 内核寄存器编号（APIv2 READREG/WRITEREG） '''
    CORE_REGS = {**{f'r{i}': i for i in range(16)},
                 'sp': 13, 'lr': 14, 'pc': 15, 'xpsr': 16, 'msp': 17, 'psp': 18, 'cfbp': 20}

    # Debug Halting Control and Status Register
    DHCSR = 0xE000EDF0
    DBGKEY      = 0xA05F0000
    C_DEBUGEN   = (1 <<  0)
    C_HALT      = (1 <<  1)
    C_STEP      = (1 <<  2)
    C_MASKINTS  = (1 <<  3)
    S_HALT      = (1 << 17)
    S_RESET_ST  = (1 << 25)

    # Debug Exception and Monitor Control Register
    DEMCR = 0xE000EDFC
    DEMCR_VC_CORERESET = (1 << 0)

    # Application Interrupt and Reset Control Register
    AIRCR = 0xE000ED0C
    AIRCR_VECTKEY    = 0x05FA0000
    AIRCR_SYSRESETREQ = (1 << 2)

    @classmethod
    def get_all_connected(cls):
        ''' 返回 [(serial, name), ...] '''
        probes = []
        try:
            for dev in usb.core.find(find_all=True, idVendor=cls.VENDOR):
                if dev.idProduct not in cls.PRODUCTS:
                    continue

                try:
                    serial = dev.serial_number or ''
                except Exception:
                    serial = ''

                probes.append((serial, cls.PRODUCTS[dev.idProduct][0]))

        except usb.core.NoBackendError:
            pass

        return probes

    def __init__(self, serial=None, mode='arm', core='Cortex-M0', speed=4000):
        self.dev = None

        for dev in usb.core.find(find_all=True, idVendor=self.VENDOR):
            if dev.idProduct not in self.PRODUCTS:
                continue

            if serial:
                try:
                    if dev.serial_number != serial: continue
                except Exception:
                    continue

            self.dev = dev
            self.name, self.ep_out = self.PRODUCTS[dev.idProduct]
            break

        if self.dev is None:
            raise Exception('未找到 ST-Link' if not serial else f'未找到序列号为 {serial} 的 ST-Link')

        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            raise Exception(f'打开 {self.name} 失败：{e}\n'
                            f'Windows 下可能需要用 Zadig 把它的接口驱动换成 WinUSB')

        self.open(mode, core, speed)

    ''' 底层收发 '''

    def _xfer(self, cmd, rx_len=0, tx_data=None):
        packet = bytearray(16)
        packet[:len(cmd)] = bytes(cmd)

        self.dev.write(self.ep_out, packet, 1000)

        if tx_data is not None:
            self.dev.write(self.ep_out, bytes(tx_data), 3000)

        if rx_len:
            return bytes(self.dev.read(self.EP_IN, rx_len, 3000))

        return b''

    def _check_status(self, res, what):
        if len(res) >= 1 and res[0] != self.DEBUG_OK:
            reason = self.ERRORS.get(res[0], f'状态码 0x{res[0]:02X}')

            raise Exception(f'{self.name} {what}失败：{reason}')

    def _check_rw(self, what):
        ''' 读写内存后确认没有出错 '''
        if self.has_rw_status2:
            res = self._xfer([self.DEBUG_COMMAND, self.APIV2_GETLASTRWSTATUS2], 12)
        else:
            res = self._xfer([self.DEBUG_COMMAND, self.APIV2_GETLASTRWSTATUS], 2)

        self._check_status(res, what)

    ''' 连接 '''

    def open(self, mode='arm', core='Cortex-M0', speed=4000):
        self.mode = mode.lower()

        if self.mode.startswith('rv'):
            raise Exception('ST-Link 只支持 ARM 目标，RISC-V 请改用 J-Link 或 OpenOCD')

        version = self._xfer([self.GET_VERSION], 6)
        v = (version[0] << 8) | version[1]
        self.stlink_ver = (v >> 12) & 0x0F
        self.jtag_ver   = (v >>  6) & 0x3F

        if self.stlink_ver >= 3:        # V3 的版本号另有一条扩展命令，GET_VERSION 里是 0
            version = self._xfer([self.GET_VERSION_EX], 12)
            self.stlink_ver, self.jtag_ver = version[0], version[2]

        if self.stlink_ver < 2:
            raise Exception(f'{self.name}（V{self.stlink_ver}）太旧，只支持 ST-Link V2 及以上')

        ''' V3 的 JTAG 版本号是另起的一套，不能拿 V2 的门限去比 '''
        self.api_v3         = self.stlink_ver >= 3
        self.has_set_freq   = self.api_v3 or self.jtag_ver >= 24
        self.has_rw_status2 = self.api_v3 or self.jtag_ver >= 28

        current = self._xfer([self.GET_CURRENT_MODE], 2)
        if current[0] == self.MODE_DFU:
            self._xfer([self.DFU_COMMAND, self.DFU_EXIT])
        elif current[0] == self.MODE_DEBUG:
            self._xfer([self.DEBUG_COMMAND, self.DEBUG_EXIT])

        self.set_speed(speed)

        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_ENTER,
                          self.ENTER_JTAG if self.mode == 'armj' else self.ENTER_SWD], 2)
        self._check_status(res, '连接目标芯片')

        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_READ_IDCODES], 12)
        self.idcode = struct.unpack_from('<I', res, 4)[0]
        if self.idcode in (0x00000000, 0xFFFFFFFF):
            raise Exception('读不到目标芯片的 IDCODE，请检查接线、供电和复位脚')

        self.core_regs = dict(self.CORE_REGS)

    def set_speed(self, speed):
        ''' speed 单位 kHz。设不上不致命，按探测器当前频率继续就是了 '''
        jtag = self.mode == 'armj'

        try:
            if self.api_v3:
                ''' V3 只接受它自己报出来的那几档频率，取不超过目标值的最大一档 '''
                res = self._xfer([self.DEBUG_COMMAND, self.APIV3_GET_COM_FREQ, int(jtag)], 52)
                self._check_status(res, '读取可用频率')

                count = min(res[8], 10)
                avail = sorted((struct.unpack_from('<I', res, 12 + 4 * i)[0] for i in range(count)), reverse=True)

                self.speed = next((khz for khz in avail if khz <= speed), avail[-1]) if avail else speed

                res = self._xfer([self.DEBUG_COMMAND, self.APIV3_SET_COM_FREQ, int(jtag), 0]
                                 + list(struct.pack('<I', self.speed)), 8)
                self._check_status(res, '设置频率')

            elif self.has_set_freq:
                table = self.JTAG_FREQ if jtag else self.SWD_FREQ

                self.speed, divisor = next(((khz, div) for khz, div in table if khz <= speed), table[-1])

                cmd = self.APIV2_JTAG_SET_FREQ if jtag else self.APIV2_SWD_SET_FREQ
                res = self._xfer([self.DEBUG_COMMAND, cmd] + list(struct.pack('<H', divisor)), 2)
                self._check_status(res, '设置频率')

            else:
                self.speed = 0      # 固件不支持调速，用默认频率

        except Exception as e:
            self.speed = 0
            print(f'{self.name} 设置速度失败，沿用当前频率：{e}')

    def close(self):
        try:
            self._xfer([self.DEBUG_COMMAND, self.DEBUG_EXIT])
        except Exception:
            pass

        usb.util.dispose_resources(self.dev)

    ''' 内存读写 '''

    def read_mem_U8(self, addr, count):
        data = bytearray()

        while count:
            if addr % 4 == 0 and count >= 4:
                size = min(count - count % 4, self.MAX_RW_32)
                res = self._xfer([self.DEBUG_COMMAND, self.DEBUG_READMEM_32BIT]
                                 + list(struct.pack('<IH', addr, size)), size)
            else:
                size = min(count, self.MAX_RW_8)
                res = self._xfer([self.DEBUG_COMMAND, self.APIV2_READMEM_8BIT]
                                 + list(struct.pack('<IH', addr, size)), max(size, 2))
                res = res[:size]

            data += res
            addr  += size
            count -= size

        return list(data)

    def read_mem_U16(self, addr, count):
        data = bytes(self.read_mem_U8(addr, count * 2))

        return list(struct.unpack(f'<{count}H', data))

    def read_mem_U32(self, addr, count):
        data = bytes(self.read_mem_U8(addr, count * 4))

        return list(struct.unpack(f'<{count}I', data))

    def read_U32(self, addr):
        return self.read_mem_U32(addr, 1)[0]

    def read_U16(self, addr):
        return self.read_mem_U16(addr, 1)[0]

    def read_U8(self, addr):
        return self.read_mem_U8(addr, 1)[0]

    def write_mem_U8(self, addr, data):
        data = bytes(bytearray(data))

        while data:
            if addr % 4 == 0 and len(data) >= 4:
                size = min(len(data) - len(data) % 4, self.MAX_RW_32)
                cmd  = self.DEBUG_WRITEMEM_32BIT
            else:
                size = min(len(data), self.MAX_RW_8)
                cmd  = self.DEBUG_WRITEMEM_8BIT

            self._xfer([self.DEBUG_COMMAND, cmd] + list(struct.pack('<IH', addr, size)),
                       tx_data=data[:size])

            addr += size
            data  = data[size:]

        self._check_rw('写内存')

    def write_mem_U32(self, addr, data):
        self.write_mem_U8(addr, struct.pack(f'<{len(data)}I', *data))

    def write_U32(self, addr, val):
        self.write_mem_U8(addr, struct.pack('<I', val))

    def write_U16(self, addr, val):
        self.write_mem_U8(addr, struct.pack('<H', val))

    def write_U8(self, addr, val):
        self.write_mem_U8(addr, struct.pack('<B', val))

    def write_debug_reg(self, addr, val):
        ''' 调试寄存器要走专门的命令，核心 halt 住时普通写内存可能不通 '''
        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_WRITEDEBUGREG]
                         + list(struct.pack('<II', addr, val)), 2)
        self._check_status(res, '写调试寄存器')

    def read_debug_reg(self, addr):
        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_READDEBUGREG]
                         + list(struct.pack('<I', addr)), 8)
        self._check_status(res, '读调试寄存器')

        return struct.unpack_from('<I', res, 4)[0]

    ''' 内核寄存器 '''

    def read_reg(self, reg):
        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_READREG, self.core_regs[reg]], 8)
        self._check_status(res, f'读寄存器 {reg} ')

        return struct.unpack_from('<I', res, 4)[0]

    def read_regs(self, rlist):
        return {reg: self.read_reg(reg) for reg in rlist}

    def write_reg(self, reg, val):
        res = self._xfer([self.DEBUG_COMMAND, self.APIV2_WRITEREG, self.core_regs[reg]]
                         + list(struct.pack('<I', val & 0xFFFFFFFF)), 2)
        self._check_status(res, f'写寄存器 {reg} ')

    ''' 运行控制 '''

    def halt(self):
        self.write_debug_reg(self.DHCSR, self.DBGKEY | self.C_DEBUGEN | self.C_HALT)

    def go(self):
        self.write_debug_reg(self.DHCSR, self.DBGKEY | self.C_DEBUGEN)

    def step(self):
        self.write_debug_reg(self.DHCSR, self.DBGKEY | self.C_DEBUGEN | self.C_HALT | self.C_MASKINTS)
        self.write_debug_reg(self.DHCSR, self.DBGKEY | self.C_DEBUGEN | self.C_STEP | self.C_MASKINTS)
        self.write_debug_reg(self.DHCSR, self.DBGKEY | self.C_DEBUGEN | self.C_HALT)

    def halted(self):
        return bool(self.read_debug_reg(self.DHCSR) & self.S_HALT)

    def reset(self):
        try:
            self._xfer([self.DEBUG_COMMAND, self.APIV2_DRIVE_NRST, 0x02], 2)     # 拉低复位脚再释放
        except Exception:
            self.write_debug_reg(self.AIRCR, self.AIRCR_VECTKEY | self.AIRCR_SYSRESETREQ)

        self.wait_reset()

    def reset_and_halt(self):
        ''' 复位并停在复位向量上。

            这里必须用 AIRCR 的 SYSRESETREQ 而不是 ST-Link 的 RESETSYS：RESETSYS 会拉 nRST，
            把调试域连同 DEMCR 里刚设好的 vector catch 一起复位掉，结果核心一路跑飞，
            等我们再去 halt 时 PC 已经停在随便哪里了。 '''
        self.halt()

        demcr = self.read_debug_reg(self.DEMCR)
        self.write_debug_reg(self.DEMCR, demcr | self.DEMCR_VC_CORERESET)

        self.read_debug_reg(self.DHCSR)     # S_RESET_ST 是读清的，先清掉

        try:
            self.write_debug_reg(self.AIRCR, self.AIRCR_VECTKEY | self.AIRCR_SYSRESETREQ)
        except Exception:
            pass                            # 复位那一下目标来不及应答是正常的

        start = time.time()
        reset_seen = False
        while time.time() - start < 2.0:
            try:
                dhcsr = self.read_debug_reg(self.DHCSR)
            except Exception:
                time.sleep(0.01)
                continue

            reset_seen = reset_seen or bool(dhcsr & self.S_RESET_ST)

            if reset_seen and dhcsr & self.S_HALT:
                break

            time.sleep(0.001)

        else:
            raise Exception('复位后核心没有停在复位向量上，请检查目标板供电和复位脚')

        self.write_debug_reg(self.DEMCR, demcr)

        self.write_reg('xpsr', 0x01000000)  # 复位向量可能指向 ARM 地址，这里补上 thumb 位

    def wait_reset(self):
        start = time.time()
        while time.time() - start < 2.0:
            try:
                if (self.read_debug_reg(self.DHCSR) & self.S_RESET_ST) == 0:
                    return
            except Exception:
                time.sleep(0.01)


if __name__ == '__main__':
    print('connected:', STLink.get_all_connected())

    stl = STLink()
    print(f'{stl.name}, JTAG API v{stl.jtag_ver}, IDCODE 0x{stl.idcode:08X}')
    print(f'CPUID 0x{stl.read_U32(0xE000ED00):08X}')
    stl.halt()
    print('halted:', stl.halted())
    print('pc = 0x%08X' % stl.read_reg('r15'))
    stl.go()
    stl.close()
