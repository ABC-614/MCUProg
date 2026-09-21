import importlib

from . import flash
from . import flashAlgo


class Chip(object):
    CHIP_CORE = 'Cortex-M0'

    def __init__(self, xlink, falgo):
        super(Chip, self).__init__()

        self.progress = None    # callable(done, total, message)，total 为 0 表示进度未知
        self.abort = False      # 置 True 后当前操作会在最近的安全点停下来

        if isinstance(falgo, tuple):
            name, addr, size, path = falgo

            self.falgo = flashAlgo.FlashAlgo(path, addr, size).flash_algo
            if self.falgo['arch'] == 'RISC-V':  self.CHIP_CORE = 'RISC-V'

        else:
            self.falgo = importlib.import_module(f'.{falgo}', 'FlashAlgo').flash_algo

        if not xlink:   # used for CHIP_SIZE, SECT_SIZE, PAGE_SIZE access
            return

        self.xlink = xlink

        self.flash = flash.Flash(self.xlink, self.falgo, aborted=lambda: self.abort)

    def report(self, done, total, message=''):
        if self.abort:
            raise flash.Aborted('已中断')

        if self.progress:
            self.progress(done, total, message)

    @property
    def BLANK_PAGE(self):
        ''' 擦除后一页应有的内容，用来跳过空白页 '''
        return bytes([self.falgo.get('flash_empty', 0xFF)]) * self.PAGE_SIZE

    @property
    def CHIP_BASE(self):
        return self.falgo['flash_start']

    @property
    def CHIP_SIZE(self):
        return self.falgo['flash_size']

    @property
    def SECT_SKIP(self):    # 有些 Flash 前几个扇区不能使用
        return 0

    @property
    def SECT_SIZE(self):
        starts, sizes = zip(*self.falgo['sector_sizes'])

        return min(sizes)   # 简化处理，大扇区多擦几次也不会出错

    @property
    def PAGE_SIZE(self):
        return self.falgo['flash_page_size']

    def chip_erase(self):
        self.report(0, 0, '整片擦除')

        self.flash.Init(0, 0, 1)
        self.flash.EraseChip()
        self.flash.UnInit(1)

        self.report(1, 1, '整片擦除完成')

    def sect_erase(self, addr, size):
        count = size // self.SECT_SIZE

        self.flash.Init(0, 0, 1)
        for i in range(count):
            self.report(i, count, f'擦除 0x{self.CHIP_BASE + addr + self.SECT_SIZE * i:08X}')
            self.flash.EraseSector(self.CHIP_BASE + addr + self.SECT_SIZE * i)
        self.flash.UnInit(1)

        self.report(count, count, '擦除完成')

    def chip_write(self, addr, data, verify=True):
        data += b'\xFF' * (-len(data) % self.PAGE_SIZE)

        n_sect = (len(data) + self.SECT_SIZE - 1) // self.SECT_SIZE
        n_page = len(data) // self.PAGE_SIZE
        total  = n_sect + n_page + (n_page if verify else 0)
        step   = 0

        self.flash.Init(0, 0, 1)
        for i in range(n_sect):
            self.report(step, total, f'擦除 0x{self.CHIP_BASE + addr + self.SECT_SIZE * i:08X}')
            self.flash.EraseSector(self.CHIP_BASE + addr + self.SECT_SIZE * i)
            step += 1
        self.flash.UnInit(1)

        blank   = self.BLANK_PAGE
        skipped = 0

        self.flash.Init(0, 0, 2)
        for i in range(n_page):
            page = data[self.PAGE_SIZE*i : self.PAGE_SIZE*(i+1)]

            self.report(step, total, f'烧写 0x{self.CHIP_BASE + addr + self.PAGE_SIZE * i:08X}')
            step += 1

            if page == blank:   # 刚擦过的扇区本来就是这个值，不用再写一遍
                skipped += 1
                continue

            self.flash.ProgramPage(self.CHIP_BASE + addr + self.PAGE_SIZE * i, page)
        self.flash.UnInit(2)

        if skipped:
            print(f'跳过 {skipped}/{n_page} 个空白页')

        if not verify:
            self.report(total, total, '烧写完成（未校验）')
            return

        error = None
        self.flash.Init(0, 0, 3)
        if self.falgo['pc_Verify'] >= 0xFFFFFFFF:   # 算法不带 Verify，回读比对
            for i in range(n_page):
                page = self.CHIP_BASE + addr + self.PAGE_SIZE * i
                self.report(step, total, f'校验 0x{page:08X}')
                step += 1

                print(f'Verify @ 0x{page:08X}')
                rdata = bytes(self.xlink.read_mem_U8(page, self.PAGE_SIZE))
                wdata = data[self.PAGE_SIZE*i : self.PAGE_SIZE*(i+1)]
                if rdata != wdata:
                    for j in range(self.PAGE_SIZE):
                        if rdata[j] != wdata[j]:
                            error = f'校验失败：0x{page + j:08X} 读到 0x{rdata[j]:02X}，应为 0x{wdata[j]:02X}'
                            print(error)
                            break
                    break
            else:
                print('Verify OK')

        else:
            for i in range(n_page):
                page = self.CHIP_BASE + addr + self.PAGE_SIZE * i
                self.report(step, total, f'校验 0x{page:08X}')
                step += 1

                self.flash.Verify(page, data[self.PAGE_SIZE*i : self.PAGE_SIZE*(i+1)])
        self.flash.UnInit(3)

        if error:
            raise Exception(error)

        self.report(total, total, '烧写校验完成')

    def chip_read(self, addr, size, buff):
        count = size // self.PAGE_SIZE

        if self.falgo['pc_Read'] >= 0xFFFFFFFF:     # 算法不带 Read，直接读内存
            for i in range(count):
                page = self.CHIP_BASE + addr + self.PAGE_SIZE * i
                self.report(i, count, f'读取 0x{page:08X}')

                print(f'Read @ 0x{page:08X}')
                buff.extend(self.xlink.read_mem_U8(page, self.PAGE_SIZE))

        else:
            for i in range(count):
                page = self.CHIP_BASE + addr + self.PAGE_SIZE * i
                self.report(i, count, f'读取 0x{page:08X}')

                self.flash.Read(page, self.PAGE_SIZE)

                buff.extend(self.xlink.read_mem_U8(self.falgo['begin_data'], self.PAGE_SIZE))

        self.report(count, count, '读取完成')
