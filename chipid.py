#! python3
''' 从目标芯片自己身上读型号信息：内核、识别寄存器、Flash 容量、UID。

    麻烦在于各家的识别寄存器并不统一，有的还撞在同一个地址上：N32 的 DBG_ID 和
    STM32 的 DBGMCU_IDCODE 都是 0xE0042000，位域排布却完全不同。所以这里按厂商
    逐条规则去试，把所有讲得通的解释都作为候选列出来，交给人拍板。

    拿不准的一律如实说拿不准，绝不替人猜——认错型号会选错烧写算法，那是要擦坏芯片的。
'''

CORE_M0 = ('Cortex-M0', 'Cortex-M0+')


''' ================ ST 及其兼容芯片（GD32、AT32 等） ================ '''

''' 每个系列的寄存器地址不一样：
    (DBGMCU_IDCODE, Flash 容量寄存器, UID 起始, Keil pack 名, 器件名前缀) '''
FAMILIES = {
    'F0': (0x40015800, 0x1FFFF7CC, 0x1FFFF7AC, 'Keil.STM32F0xx_DFP', 'STM32F0'),
    'F1': (0xE0042000, 0x1FFFF7E0, 0x1FFFF7E8, 'Keil.STM32F1xx_DFP', 'STM32F1'),
    'F2': (0xE0042000, 0x1FFF7A22, 0x1FFF7A10, 'Keil.STM32F2xx_DFP', 'STM32F2'),
    'F3': (0xE0042000, 0x1FFFF7CC, 0x1FFFF7AC, 'Keil.STM32F3xx_DFP', 'STM32F3'),
    'F4': (0xE0042000, 0x1FFF7A22, 0x1FFF7A10, 'Keil.STM32F4xx_DFP', 'STM32F4'),
    'F7': (0xE0042000, 0x1FF0F442, 0x1FF0F420, 'Keil.STM32F7xx_DFP', 'STM32F7'),
    'H7': (0x5C001000, 0x1FF1E880, 0x1FF1E800, 'Keil.STM32H7xx_DFP', 'STM32H7'),
    'L0': (0x40015800, 0x1FF8007C, 0x1FF80050, 'Keil.STM32L0xx_DFP', 'STM32L0'),
    'L1': (0xE0042000, 0x1FF800CC, 0x1FF80050, 'Keil.STM32L1xx_DFP', 'STM32L1'),
    'L4': (0xE0042000, 0x1FFF75E0, 0x1FFF7590, 'Keil.STM32L4xx_DFP', 'STM32L4'),
    'G0': (0x40015800, 0x1FFF75E0, 0x1FFF7590, 'Keil.STM32G0xx_DFP', 'STM32G0'),
    'G4': (0xE0042000, 0x1FFF75E0, 0x1FFF7590, 'Keil.STM32G4xx_DFP', 'STM32G4'),
    'WB': (0xE0042000, 0x1FFF75E0, 0x1FFF7590, 'Keil.STM32WBxx_DFP', 'STM32WB'),
    'WL': (0xE0042000, 0x1FFF75E0, 0x1FFF7590, 'Keil.STM32WLxx_DFP', 'STM32WL'),
}

''' DBGMCU_IDCODE 低 12 位 -> (系列, 型号描述) '''
DEVICES = {
    0x440: ('F0', 'STM32F030x8/F051'),   0x442: ('F0', 'STM32F030xC/F09x'),
    0x444: ('F0', 'STM32F030x4/x6/F03x'),0x445: ('F0', 'STM32F04x/F070x6'),
    0x448: ('F0', 'STM32F070xB/F071/F072'),

    0x412: ('F1', 'STM32F10x 小容量'),   0x410: ('F1', 'STM32F10x 中容量'),
    0x414: ('F1', 'STM32F10x 大容量'),   0x430: ('F1', 'STM32F10x 超大容量'),
    0x418: ('F1', 'STM32F105/107'),      0x420: ('F1', 'STM32F100 中容量'),
    0x428: ('F1', 'STM32F100 大容量'),

    0x411: ('F2', 'STM32F2xx'),

    0x432: ('F3', 'STM32F373/378'),      0x422: ('F3', 'STM32F302xB/303xB'),
    0x438: ('F3', 'STM32F303x4/334/328'),0x439: ('F3', 'STM32F301/302x4'),
    0x446: ('F3', 'STM32F302xE/303xE'),

    0x413: ('F4', 'STM32F405/407/415/417'), 0x419: ('F4', 'STM32F42x/43x'),
    0x421: ('F4', 'STM32F446'),          0x423: ('F4', 'STM32F401xB/C'),
    0x431: ('F4', 'STM32F411'),          0x433: ('F4', 'STM32F401xD/E'),
    0x434: ('F4', 'STM32F469/479'),      0x441: ('F4', 'STM32F412'),
    0x458: ('F4', 'STM32F410'),          0x463: ('F4', 'STM32F413/423'),

    0x449: ('F7', 'STM32F74x/75x'),      0x451: ('F7', 'STM32F76x/77x'),
    0x452: ('F7', 'STM32F72x/73x'),

    0x450: ('H7', 'STM32H742/743/750/753'), 0x480: ('H7', 'STM32H7A3/7B3'),
    0x483: ('H7', 'STM32H72x/73x'),

    0x417: ('L0', 'STM32L05x/06x'),      0x425: ('L0', 'STM32L031/041'),
    0x447: ('L0', 'STM32L07x/08x'),      0x457: ('L0', 'STM32L01x/02x'),

    0x416: ('L1', 'STM32L1xx Cat.1/2'),  0x429: ('L1', 'STM32L1xx Cat.2'),
    0x427: ('L1', 'STM32L1xx Cat.3'),    0x436: ('L1', 'STM32L1xx Cat.4/3'),
    0x437: ('L1', 'STM32L1xx Cat.5/6'),

    0x415: ('L4', 'STM32L4x1/475/476/486'), 0x435: ('L4', 'STM32L43x/44x'),
    0x462: ('L4', 'STM32L45x/46x'),      0x464: ('L4', 'STM32L41x/42x'),
    0x470: ('L4', 'STM32L4Rx/4Sx'),      0x471: ('L4', 'STM32L4P5/Q5'),

    0x460: ('G0', 'STM32G07x/G08x'),     0x466: ('G0', 'STM32G03x/G04x'),
    0x456: ('G0', 'STM32G05x/G06x'),     0x467: ('G0', 'STM32G0Bx/G0Cx'),

    0x468: ('G4', 'STM32G431/441'),      0x469: ('G4', 'STM32G47x/48x'),
    0x479: ('G4', 'STM32G491/4A1'),

    0x495: ('WB', 'STM32WB55/35'),       0x496: ('WB', 'STM32WB5M'),
    0x497: ('WL', 'STM32WLE5/WL55'),
}

''' ST 自家芯片常见的 REV_ID。兼容厂商照搬了 DEV_ID，版本号却是自己编的，
    所以 REV 落在这张表之外，多半说明这不是 ST 原厂片子。只作提示，不作判据。 '''
ST_REVS = {0x0000, 0x1000, 0x1001, 0x1003, 0x1004, 0x1007, 0x100F,
           0x2000, 0x2001, 0x2003, 0x2007, 0x3000, 0x3001, 0x3003,
           0x4000, 0x4001, 0x5000, 0x6000}

''' 见过的兼容厂商版本号 -> 厂商名 '''
ALT_REVS = {
    0x1303: 'GigaDevice',   # GD32F1x0/F3x0 实测值
}

''' DEV_ID -> 兼容厂商对应的 (pack, 器件名前缀)。只放能对上号的，
    对不上的走"搜索型号"那条路，不在这里瞎编。 '''
GD32_EQUIV = {
    0x410: ('GigaDevice.GD32F10x_DFP', 'GD32F103'),
}


''' ================ Nations N32 ================ '''

''' N32 的 DBG_ID 和 STM32 的 DBGMCU_IDCODE 同址，位域却是散开的：
        [31:28] SRAM 容量，16 KB 为单位，减一
        [23:20] 型号低 4 位
        [19:16] Flash 容量，64 KB 为单位
        [15:12] 型号高 4 位
        [11:8]  型号中 4 位
        [7:4]   版本高 4 位
        [3:0]   版本低 4 位
    依据 OpenOCD 的 Nations N32G45x 驱动（对应其用户手册 29.4.2 节）。

    要命的是：按 STM32 的规矩取低 12 位，在 N32 上读到的其实是
    "型号中 4 位 + 版本号"，有概率正好撞上某个 STM32 的 DEV_ID。
    所以 N32 这条规则必须排在 ST 那条前面先试。 '''
N32_DBG_ID = 0xE0042000

N32_DEVICES = {
    0x452: 'N32G452',
    0x455: 'N32G455',
    0x457: 'N32G457',
}

N32_PACK = 'NSING.N32G45x_DFP'   # 厂商在 pack 索引里叫 NSING，不是 Nations


def decode_n32(dbg_id):
    ''' 按 N32 的位域解一遍 DBG_ID，解不通返回 None '''
    dev_num = (((dbg_id >> 12) & 0xF) << 8) | (((dbg_id >> 8) & 0xF) << 4) | ((dbg_id >> 20) & 0xF)

    if dev_num not in N32_DEVICES:
        return None

    flash_kb = ((dbg_id >> 16) & 0xF) * 64
    sram_kb  = (((dbg_id >> 28) & 0xF) + 1) * 16

    if not flash_kb:        # 容量为 0 说明这个位域根本不是容量，解错了
        return None

    return {
        'dev_num' : dev_num,
        'name'    : N32_DEVICES[dev_num],
        'rev'     : (((dbg_id >> 4) & 0xF) << 4) | (dbg_id & 0xF),
        'flash_kb': flash_kb,
        'sram_kb' : sram_kb,
    }


''' ================ 识别 ================ '''


def candidate(vendor, name, flash_kb, **kw):
    ''' 一个候选解释。pack/prefix 为空表示没法直接定位算法，得靠"搜索型号"那条路 '''
    cand = {'vendor': vendor, 'name': name, 'flash_kb': flash_kb,
            'flash_start': 0x08000000, 'ram_start': None, 'ram_size': None,
            'sram_size': None,      # 芯片自报的 SRAM 总量，未截断；ram_size 是给算法用的窗口
            'pack': None, 'prefix': None, 'search': None, 'why': '', 'sure': False}
    cand.update(kw)

    return cand


def identify(xlk):
    ''' 返回一个 dict，读不到的字段为 None。不抛异常，识别不出来也要把已知的部分给出来。

        candidates 里是所有讲得通的解释，确定的排前面；notes 是给人看的提醒。 '''
    info = {'core': None, 'idcode': None, 'dev_id': None, 'rev': None,
            'family': None, 'name': None, 'flash_kb': None, 'uid': None,
            'pack': None, 'candidates': [], 'notes': []}

    try:
        info['core'] = xlk.read_core_type()
    except Exception:
        pass

    ''' 识别寄存器的位置和芯片系列有关，而系列又要靠它才知道——先按内核类型挑候选地址试 '''
    bases = [0x40015800, 0xE0042000] if info['core'] in CORE_M0 else [0xE0042000, 0x5C001000, 0x40015800]

    for base in bases:
        try:
            idcode = xlk.read_U32(base)
        except Exception:
            continue

        if idcode in (0x00000000, 0xFFFFFFFF):
            continue

        ''' 规则一：Nations N32。和 ST 同址、位域不同，必须先试 '''
        if base == N32_DBG_ID:
            n32 = decode_n32(idcode)
            if n32:
                info['candidates'].append(candidate(
                    'Nations', n32['name'], n32['flash_kb'],
                    pack=N32_PACK, prefix=n32['name'],
                    ram_start=0x20000000, ram_size=min(n32['sram_kb'] * 1024, 0x8000),
                    sram_size=n32['sram_kb'] * 1024,
                    sure=True,
                    why=f'DBG_ID 0x{idcode:08X} 按 N32 位域解出型号 0x{n32["dev_num"]:03X}、'
                        f'Flash {n32["flash_kb"]} KB、SRAM {n32["sram_kb"]} KB'))

        ''' 规则二：ST 及照搬了 ST 寄存器的兼容厂商 '''
        dev_id = idcode & 0xFFF
        if dev_id not in DEVICES:
            continue

        family, name = DEVICES[dev_id]

        ''' DBGMCU 在很多系列里是镜像映射的，读到的地址未必是该系列的正规地址，以表为准 '''
        if FAMILIES[family][0] != base:
            try:
                idcode = xlk.read_U32(FAMILIES[family][0])
                if idcode & 0xFFF != dev_id:
                    continue
            except Exception:
                pass

        info.update(idcode=idcode, dev_id=dev_id, rev=(idcode >> 16) & 0xFFFF,
                    family=family, name=name, pack=FAMILIES[family][3])
        break

    if info['family']:
        dbgmcu, fsize_reg, uid_reg, pack, prefix = FAMILIES[info['family']]

        try:
            flash_kb = xlk.read_mem_U16(fsize_reg, 1)[0]

            ''' 容量寄存器读错位置时会读出 0xFFFF 之类的鬼值，做个常识检查 '''
            if 1 <= flash_kb <= 8192:
                info['flash_kb'] = flash_kb
        except Exception:
            pass

        try:
            uid = xlk.read_mem_U32(uid_reg, 3)
            if any(word not in (0x00000000, 0xFFFFFFFF) for word in uid):
                info['uid'] = f'{uid[2]:08X}{uid[1]:08X}{uid[0]:08X}'
        except Exception:
            pass

        rev = info['rev']

        info['candidates'].append(candidate(
            'ST', info['name'], info['flash_kb'], pack=pack, prefix=prefix,
            sure=rev in ST_REVS,
            why=f'DBGMCU_IDCODE 0x{info["idcode"]:08X}，DEV_ID 0x{info["dev_id"]:03X}'
                + (f'，Flash 容量寄存器 {info["flash_kb"]} KB' if info['flash_kb'] else '')))

        ''' DEV_ID 是照搬的，光凭它分不出 ST 和 GD32/AT32。版本号能给点线索，
            但也只是线索——两边都列出来，让人自己定 '''
        alt = ALT_REVS.get(rev)

        if alt or rev not in ST_REVS:
            equiv = GD32_EQUIV.get(info['dev_id'])

            info['candidates'].append(candidate(
                alt or '兼容厂商',
                equiv[1] if equiv else f'GD32/AT32 等兼容芯片（与 {info["name"]} 同 ID）',
                info['flash_kb'],
                pack=equiv[0] if equiv else None,
                prefix=equiv[1] if equiv else None,
                search=None if equiv else 'GD32',
                why=(f'REV 0x{rev:04X} 是 {alt} 的版本号' if alt else
                     f'REV 0x{rev:04X} 不在 ST 常见版本号之列，多半不是 ST 原厂片')))

            info['notes'].append(
                f'DEV_ID 0x{info["dev_id"]:03X} 被 ST 和多家兼容厂商共用，光靠它分不出是谁家的。'
                f'各家的烧写算法并不通用，选错会擦坏芯片，请对照芯片丝印确认。')

    if not info['candidates']:
        info['notes'].append(
            '没认出型号。只有 STM32 以及照搬了它寄存器的芯片（GD32、AT32、N32 等）才有这组 ID 寄存器；'
            'HC32 这类芯片没有，请用"搜索型号"按厂商和型号直接找算法。')

    ''' 确定的排前面 '''
    info['candidates'].sort(key=lambda c: not c['sure'])

    return info


def part_prefixes(cand):
    ''' 把 "STM32F405/407/415/417" 这种描述拆成 ['STM32F405', 'STM32F407', ...]，
        用来在候选词条里挑号段也对得上的那个。拆不出来就返回空表 '''
    import re

    name = (cand.get('name') or '').upper()

    ''' 头部是 "字母 + 系列数字 + 子系列字母"：
        STM32F405 -> STM32F，GD32F103 -> GD32F，N32G452 -> N32G '''
    head = re.match(r'([A-Z]+\d*[A-Z])', name)
    if not head:
        return []

    return [head.group(1) + digits for digits in re.findall(r'(?<!\d)(\d{3})(?!\d)', name)]


def describe(info):
    ''' 拼成一行给人看的说明 '''
    if not info['candidates']:
        return f'未能识别型号（内核 {info["core"] or "未知"}，没读到认识的 ID 寄存器）'

    top = info['candidates'][0]

    parts = [top['name']]
    if top['flash_kb']:
        parts.append(f'{top["flash_kb"]} KB Flash')
    if info['core']:
        parts.append(info['core'])
    if info['dev_id'] is not None:
        parts.append(f'DEV_ID 0x{info["dev_id"]:03X} REV 0x{info["rev"]:04X}')
    if info['uid']:
        parts.append(f'UID {info["uid"]}')
    if len(info['candidates']) > 1:
        parts.append(f'另有 {len(info["candidates"]) - 1} 个可能')

    return '，'.join(parts)
