import os
import ctypes
import operator


import jlink
import openocd

try:
    import stlink
except ImportError as e:      # pyusb 没装时仍然可以用 J-Link / OpenOCD
    stlink = None
    print(f'ST-Link support unavailable: {e}')

''' 这几种探测器共用一套接口，pyocd 的 CortexM 是另一套 '''
DIRECT = tuple(cls for cls in (jlink.JLink, openocd.OpenOCD, stlink and stlink.STLink) if cls)


class XLink(object):
    def __init__(self, xlk):
        self.xlk = xlk

        if isinstance(self.xlk, DIRECT):
            self.reg_add_alias()

    def open(self, mode, core, speed):
        if isinstance(self.xlk, DIRECT):
            self.xlk.open(mode, core, speed)

            self.reg_add_alias()
            
        else:
            self.xlk.ap.dp.link.open()

    def reg_add_alias(self):
        def add_alias(regs, name1, name2, name3=None):
            if name1 in regs:
                regs[name2] = regs[name1]
                regs[name3] = regs[name1]
            elif name2 in regs:
                regs[name1] = regs[name2]
                regs[name3] = regs[name2]
            elif name3 and name3 in regs:
                regs[name1] = regs[name3]
                regs[name2] = regs[name3]

        self.xlk.core_regs = {k.lower() : v for k, v in self.xlk.core_regs.items()}

        if self.mode.startswith('arm'):
            add_alias(self.xlk.core_regs, 'r13', 'sp', 'r13 (sp)')
            add_alias(self.xlk.core_regs, 'r14', 'lr', 'r14 (lr)')
            add_alias(self.xlk.core_regs, 'r15', 'pc', 'r15 (pc)')

        elif self.mode.startswith('rv'):
            add_alias(self.xlk.core_regs, 'x1',  'ra')
            add_alias(self.xlk.core_regs, 'x2',  'sp')
            add_alias(self.xlk.core_regs, 'x3',  'gp')
            add_alias(self.xlk.core_regs, 'x4',  'tp')
            add_alias(self.xlk.core_regs, 'x5',  't0')
            add_alias(self.xlk.core_regs, 'x6',  't1')
            add_alias(self.xlk.core_regs, 'x7',  't2')
            add_alias(self.xlk.core_regs, 'x8',  's0', 'fp')
            add_alias(self.xlk.core_regs, 'x9',  's1')
            add_alias(self.xlk.core_regs, 'x10', 'a0')
            add_alias(self.xlk.core_regs, 'x11', 'a1')
            add_alias(self.xlk.core_regs, 'x12', 'a2')
            add_alias(self.xlk.core_regs, 'x13', 'a3')
            add_alias(self.xlk.core_regs, 'x14', 'a4')
            add_alias(self.xlk.core_regs, 'x15', 'a5')
            add_alias(self.xlk.core_regs, 'x16', 'a6')
            add_alias(self.xlk.core_regs, 'x17', 'a7')
            add_alias(self.xlk.core_regs, 'x18', 's2')
            add_alias(self.xlk.core_regs, 'x19', 's3')
            add_alias(self.xlk.core_regs, 'x20', 's4')
            add_alias(self.xlk.core_regs, 'x21', 's5')
            add_alias(self.xlk.core_regs, 'x22', 's6')
            add_alias(self.xlk.core_regs, 'x23', 's7')
            add_alias(self.xlk.core_regs, 'x24', 's8')
            add_alias(self.xlk.core_regs, 'x25', 's9')
            add_alias(self.xlk.core_regs, 'x26', 's10')
            add_alias(self.xlk.core_regs, 'x27', 's11')
            add_alias(self.xlk.core_regs, 'x28', 't3')
            add_alias(self.xlk.core_regs, 'x29', 't4')
            add_alias(self.xlk.core_regs, 'x30', 't5')
            add_alias(self.xlk.core_regs, 'x31', 't6')

    @property
    def mode(self):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.mode
        else:
            return 'arm'
    
    def write_U8(self, addr, val):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_U8(addr, val)
        else:
            self.xlk.write8(addr, val)

    def write_U16(self, addr, val):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_U16(addr, val)
        else:
            self.xlk.write16(addr, val)

    def write_U32(self, addr, val):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_U32(addr, val)
        else:
            self.xlk.write32(addr, val)

    def write_mem_U8(self, addr, data):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_mem_U8(addr, data)
        else:
            self.xlk.write_memory_block8(addr, data)

    def write_mem_U32(self, addr, data):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_mem_U32(addr, data)
        else:
            self.xlk.write_memory_block32(addr, data)

    def read_mem_U8(self, addr, count):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.read_mem_U8(addr, count)
        else:
            return self.xlk.read_memory_block8(addr, count)

    def read_mem_U16(self, addr, count):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.read_mem_U16(addr, count)
        else:
            return [self.xlk.read16(addr+i*2) for i in range(count)]

    def read_mem_U32(self, addr, count):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.read_mem_U32(addr, count)
        else:
            return self.xlk.read_memory_block32(addr, count)

    def read_U32(self, addr):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.read_U32(addr)
        else:
            return self.xlk.read32(addr)

    def read_reg(self, reg):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.read_reg(reg.lower())
        else:
            return self.xlk.read_core_register_raw(reg)

    def read_regs(self, rlist):
        if isinstance(self.xlk, DIRECT):
            return dict(zip(rlist, self.xlk.read_regs([reg.lower() for reg in rlist]).values()))
        else:
            return dict(zip(rlist, self.xlk.read_core_registers_raw(rlist)))

    def write_reg(self, reg, val):
        if isinstance(self.xlk, DIRECT):
            self.xlk.write_reg(reg.lower(), val)
        else:
            self.xlk.write_core_register_raw(reg, val)

    def reset(self):
        self.xlk.reset()
    
    ''' Cortex-M 的调试异常与监视控制寄存器，bit0 = 复位后停在复位向量 '''
    DEMCR        = 0xE000EDFC
    VC_CORERESET = 1 << 0

    def reset_and_halt(self):
        if isinstance(self.xlk, DIRECT):
            self.xlk.reset_and_halt()

        else:
            self.pyocd_reset_and_halt()

    def pyocd_reconnect(self):
        ''' 复位往往把调试端口一起带下电，CDBGPWRUPREQ 被清掉，之后每一笔传输
            都是 TransferError。pyocd 的自动恢复逻辑在 session/target 那一层，
            而这里是手工搭起来的 CortexM，走不到，只好自己把 DP 和 AP 重来一遍。

            dp.init() 本身就是完整的重连序列：连接、swj 时序、读 DP IDCODE、清粘滞错误 '''
        try:
            dp = self.xlk.ap.dp

            dp.init(getattr(self.xlk, 'mcuprog_protocol', None))
            dp.power_up_debug()

            self.xlk.ap.init()

            return True

        except Exception as e:
            print(f'重连调试端口失败：{e}')

            return False

    def pyocd_halted(self):
        ''' 链路断着的时候问"停住了吗"本身也会抛，这种情况按没停住算 '''
        try:
            return self.halted()

        except Exception:
            return False

    def pyocd_reset_and_halt(self):
        ''' pyocd 默认用 SYSRESETREQ 复位，再靠 DEMCR 的向量捕获把核停在复位处。

            两个坑叠在一起：一是复位会把调试端口带下电，之后所有传输都报 TransferError；
            二是 pyocd 不检查核到底停没停，径直去读 xpsr，而在没停住的核上读寄存器
            S_REGRDY 永远不置位，撞上 cortex_m.py 里一句不带消息的 assert——
            报出来就是"失败（）"，一点线索都没有。

            要紧的是：传输报错不等于复位没成功。向量捕获很可能已经把核停在复位向量上了，
            只是这会儿没人读得到。所以每次失败都先把链路接回来，再判断核的状态。 '''
        from pyocd.core.target import Target

        attempts = [(None,                          'SYSRESETREQ'),
                    (Target.ResetType.SW_VECTRESET, 'VECTRESET'),
                    (Target.ResetType.HW,           '硬件复位')]

        last = None
        for reset_type, name in attempts:
            failure = None

            try:
                self.xlk.reset_and_halt(reset_type)

            except Exception as e:
                failure = f'{name} 出错（{str(e) or type(e).__name__}）'

                self.pyocd_reconnect()

            if self.pyocd_halted():
                if failure:
                    ''' 这颗芯片每次复位都会走到这儿，措辞上别用出错/失败那几个词——
                        日志的着色规则会把它标成红色，看着像出了事，其实是正常现象 '''
                    print(f'{name} 之后调试端口掉电，重连后核已停在复位处，继续'
                          f'（这颗芯片复位会把调试端口一起带下去，属正常现象）')

                    ''' pyocd 是半路抛出去的，没来得及把 DEMCR 恢复回去。
                        向量捕获要是留在开着的状态，之后每次复位核都会停住，
                        烧完就不会自动运行了。核这会儿已经停住，清掉是安全的 '''
                    try:
                        self.write_U32(self.DEMCR, self.read_U32(self.DEMCR) & ~self.VC_CORERESET)
                    except Exception:
                        pass

                return

            last = failure or f'{name} 之后核没有停住'

            print(f'{name} 没能把核停在复位处：{last}')

            ''' 上一次可能半路退出，把向量捕获留在开着的状态。不清掉的话
                以后每次复位都会停住，烧完不会自动运行 '''
            try:
                self.write_U32(self.DEMCR, self.read_U32(self.DEMCR) & ~self.VC_CORERESET)
            except Exception:
                pass

        ''' 复位全都不成，退一步：烧写算法要的其实只是"核停着"。
            停住照样能干活，只是没能从复位状态开始，照实说一声 '''
        try:
            self.halt()
        except Exception:
            pass

        if not self.pyocd_halted():
            raise Exception(last or '没能停住目标核')

        print('警告：没能复位目标芯片，只把核停住了。目标程序若开了看门狗或改过时钟，'
              '烧写可能不稳；接上复位脚会更可靠')

    def halt(self):
        self.xlk.halt()

    def step(self):
        self.xlk.step()

    def go(self):
        if hasattr(self.xlk, 'go'):     # OpenOCD 和 pyocd 叫 resume
            self.xlk.go()
        else:
            self.xlk.resume()

    def halted(self):
        if isinstance(self.xlk, DIRECT):
            return self.xlk.halted()
        else:
            return self.xlk.is_halted()

    def close(self):
        if isinstance(self.xlk, DIRECT):
            self.xlk.close()
        else:
            self.xlk.ap.dp.link.close()

    CORE_TYPE_NAME = {
        0xC20: "Cortex-M0",
        0xC21: "Cortex-M1",
        0xC23: "Cortex-M3",
        0xC24: "Cortex-M4",
        0xC27: "Cortex-M7",
        0xC60: "Cortex-M0+",
        0xD20: "Cortex-M23",
        0xD21: "Cortex-M33",
        0xD22: "Cortex-M55",
        0xD23: "Cortex-M85",
        0x132: "Star-MC1"
    }

    def read_core_type(self):
        if self.mode.startswith('arm'):
            CPUID = 0xE000ED00
            CPUID_PARTNO_Pos = 4
            CPUID_PARTNO_Msk = 0x0000FFF0
            
            cpuid = self.read_U32(CPUID)

            core_type = (cpuid & CPUID_PARTNO_Msk) >> CPUID_PARTNO_Pos

            return self.CORE_TYPE_NAME.get(core_type, f'未知内核 (CPUID 0x{cpuid:08X})')

        elif self.mode.startswith('rv'):
            halted = self.halted()
            if not halted: self.halt()
            isa = self.read_reg('misa')
            if not halted: self.go()

            if ((isa >> 30) & 3) == 1:
                name = 'RV32'
            elif ((isa >> 62) & 3) == 2:
                name = 'RV64'
            else:
                return 'RISC-V'

            indx = lambda chr: ord(chr) - ord('A')

            if isa & (1 << indx('I')):
                name += 'I'
            else:
                name += 'E'

            if isa & (1 << indx('M')):
                name += 'M'

            if isa & (1 << indx('A')):
                name += 'A'

            if isa & (1 << indx('F')):
                name += 'F'

            if isa & (1 << indx('D')):
                name += 'D'

            if isa & (1 << indx('C')):
                name += 'C'

            if isa & (1 << indx('B')):
                name += 'B'

            name = name.replace('IMAFD', 'G')

            return name
