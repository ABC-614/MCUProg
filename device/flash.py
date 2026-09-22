"""
 mbed CMSIS-DAP debugger
 Copyright (c) 2006-2015 ARM Limited
"""
import os
import sys
import time
import struct


class Aborted(Exception):
    ''' 用户中途点了停止 '''


class Flash(object):
    def __init__(self, xlink, falgo, aborted=None):
        self.xlink = xlink

        self.falgo = falgo

        self.aborted = aborted or (lambda: False)

        # perform a reset and stop the core on the reset handler
        try:
            self.xlink.reset_and_halt()

        except Exception as e:
            raise Exception(f'复位并停住目标核失败（{e}）。'
                            f'烧写前必须把核停在复位处，这一步过不去通常是调试模式或速度不合适、'
                            f'复位脚没接上，或者目标芯片上了读保护')

        self.check_ram()

        if self.xlink.mode.startswith('arm'):
            self.xlink.write_reg('r9', self.falgo['static_base'])
            self.xlink.write_reg('sp', self.falgo['begin_stack'])

        elif self.xlink.mode.startswith('rv'):
            self.xlink.write_reg('gp', self.falgo['static_base'])
            self.xlink.write_reg('sp', self.falgo['begin_stack'])

            dcsr = self.xlink.read_reg('dcsr')
            self.xlink.write_reg('dcsr', dcsr | (1 << 15))  # when ebreak execute, enter debug mode

        # 将Flash算法下载到RAM
        self.xlink.write_mem_U32(self.falgo['load_address'], self.falgo['instructions'])

        self.check_algo()

    def check_ram(self):
        ''' 烧写算法要在目标 RAM 里跑。RAM 地址配错（devices.txt 里不写就默认 0x20000000）时，
            以前的表现是算法静默跑飞、擦不动也写不进，这里先花两次读写把它挡下来 '''
        addr = self.falgo['load_address']

        try:
            saved = self.xlink.read_mem_U32(addr, 2)

            for pattern in ([0xA5A5A5A5, 0x5A5A5A5A], [0x5A5A5A5A, 0xA5A5A5A5]):
                self.xlink.write_mem_U32(addr, pattern)

                if list(self.xlink.read_mem_U32(addr, 2)) != pattern:
                    raise Exception('读回的数据不对')

            self.xlink.write_mem_U32(addr, saved)

        except Exception as e:
            raise Exception(f'目标 RAM 0x{addr:08X} 读写不正常（{e}）。\n'
                            f'请确认芯片型号选对了，以及 devices.txt 里这颗芯片的 RAM 地址和大小是否正确\n'
                            f'（不写的话默认是 0x20000000 / 4 KB，对很多芯片并不适用）')

    def check_algo(self):
        ''' 确认算法真的落到 RAM 里了，避免算法没下全就开始擦片 '''
        size = min(len(self.falgo['instructions']), 16)

        back = self.xlink.read_mem_U32(self.falgo['load_address'], size)

        if list(back) != list(self.falgo['instructions'][:size]):
            raise Exception(f'烧写算法没能正确写入目标 RAM 0x{self.falgo["load_address"]:08X}，'
                            f'请检查 devices.txt 里配置的 RAM 大小是否够用')

    def Init(self, addr, clk, func):    # func: 1 - Erase, 2 - Program, 3 - Verify
        print(f'Init {func}')
        
        res = self.callFunctionAndWait(self.falgo['pc_Init'], addr, clk, func)
        
        if res != 0: print(f'Init() error: {res}')

    def UnInit(self, func):
        print(f'UnInit {func}')

        res = self.callFunctionAndWait(self.falgo['pc_UnInit'], func)
        
        if res != 0: print(f'UnInit() error: {res}')

    def EraseSector(self, addr):
        print(f'Erase @ 0x{addr:08X}')

        res = self.callFunctionAndWait(self.falgo['pc_EraseSector'], addr)

        if res != 0: print(f'EraseSector({addr:08X}) error: {res}')

    def ProgramPage(self, addr, data):
        print(f'Write @ 0x{addr:08X}')

        self.xlink.write_mem_U8(self.falgo['begin_data'], data) # 将要烧写的数据传入单片机RAM

        res = self.callFunctionAndWait(self.falgo['pc_ProgramPage'], addr, len(data), self.falgo['begin_data'])

        if res != 0: print(f'ProgramPage({addr:08X}) error: {res}')

    def Verify(self, addr, data):
        print(f'Verify @ 0x{addr:08X}')

        self.xlink.write_mem_U8(self.falgo['begin_data'], data) # 将要校验的数据传入单片机RAM

        res = self.callFunctionAndWait(self.falgo['pc_Verify'], addr, len(data), self.falgo['begin_data'])

        if res != addr+len(data): print(f'Verify({addr:08X}) error: {res}')

    def EraseChip(self):
        res = self.callFunctionAndWait(self.falgo['pc_EraseChip'], timeout=120.0)   # 整片擦除慢得多

        if res != 0: print(f'EraseChip() error: {res}')

    def BlankCheck(self, addr, size, value):
        res = self.callFunctionAndWait(self.falgo['pc_BlankCheck'], addr, size, value)

        if res != 0: print(f'BlankCheck({addr:08X}) error: {res}')

    def Read(self, addr, size):
        print(f'Read @ 0x{addr:08X}')

        res = self.callFunctionAndWait(self.falgo['pc_Read'], addr, size, self.falgo['begin_data'])

        if res != addr+size: print(f'Read({addr:08X}) error: {res}')

    def callFunction(self, pc, r0=None, r1=None, r2=None, r3=None):
        if self.xlink.mode.startswith('arm'):
            if r0 is not None: self.xlink.write_reg('r0', r0)
            if r1 is not None: self.xlink.write_reg('r1', r1)
            if r2 is not None: self.xlink.write_reg('r2', r2)
            if r3 is not None: self.xlink.write_reg('r3', r3)

            self.xlink.write_reg('pc', pc)
            self.xlink.write_reg('lr', self.falgo['load_address'] + 1)

        elif self.xlink.mode.startswith('rv'):
            if r0 is not None: self.xlink.write_reg('a0', r0)
            if r1 is not None: self.xlink.write_reg('a1', r1)
            if r2 is not None: self.xlink.write_reg('a2', r2)
            if r3 is not None: self.xlink.write_reg('a3', r3)

            self.xlink.write_reg('pc', pc)      # OpenOCD: resume from current code position.
            self.xlink.write_reg('dpc', pc)     # When resuming, PC is updated to value in dpc.
            self.xlink.write_reg('ra', self.falgo['load_address'])
        
        self.xlink.go()

    def callFunctionAndWait(self, pc, r0=None, r1=None, r2=None, r3=None, timeout=30.0):
        self.callFunction(pc, r0, r1, r2, r3)

        ''' 等算法跑完。一页程序通常一两毫秒就结束，固定 sleep(0.01) 会让烧写时间
            几乎全花在等待上，所以先连着查、再指数退避 '''
        delay = 0
        start = time.time()
        while not self.xlink.halted():
            if self.aborted():
                self.xlink.halt()

                raise Aborted('已中断')

            if time.time() - start > timeout:
                raise Exception(f'烧写算法执行超时（{timeout:.0f} s），目标芯片可能已经跑飞')

            if delay:
                time.sleep(delay)

            delay = min(delay * 2 if delay else 0.0002, 0.01)

        if self.xlink.mode.startswith('arm'):
            return self.xlink.read_reg('r0')

        elif self.xlink.mode.startswith('rv'):
            return self.xlink.read_reg('a0')
