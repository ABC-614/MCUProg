#! python3
''' 从目标芯片自己身上读型号信息：内核、DBGMCU 的 DEV_ID、Flash 容量、UID。

    这些寄存器是 ST 定义的，GD32、AT32 等兼容芯片大多也照搬了，所以对它们同样有效。
    读不到就如实说读不到，不猜。
'''

CORE_M0 = ('Cortex-M0', 'Cortex-M0+')

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


def identify(xlk):
    ''' 返回一个 dict，读不到的字段为 None。不抛异常，识别不出来也要把已知的部分给出来 '''
    info = {'core': None, 'idcode': None, 'dev_id': None, 'rev': None,
            'family': None, 'name': None, 'flash_kb': None, 'uid': None, 'pack': None}

    try:
        info['core'] = xlk.read_core_type()
    except Exception:
        pass

    ''' DBGMCU 的位置和芯片系列有关，而系列又要靠 DBGMCU 才知道——先按内核类型挑候选地址试 '''
    bases = [0x40015800, 0xE0042000] if info['core'] in CORE_M0 else [0xE0042000, 0x5C001000, 0x40015800]

    for base in bases:
        try:
            idcode = xlk.read_U32(base)
        except Exception:
            continue

        dev_id = idcode & 0xFFF
        if dev_id in (0x000, 0xFFF) or dev_id not in DEVICES:
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

    return info


def part_prefixes(info):
    ''' 把 "STM32F405/407/415/417" 这种描述拆成 ['STM32F405', 'STM32F407', ...]，
        用来在候选词条里挑号段也对得上的那个。拆不出来就返回空表 '''
    import re

    head = re.match(r'(STM32[A-Z])', info.get('name') or '')
    if not head:
        return []

    return [head.group(1) + digits for digits in re.findall(r'(?<!\d)(\d{3})(?!\d)', info['name'])]


def describe(info):
    ''' 拼成一行给人看的说明 '''
    if not info['dev_id']:
        return f'未能识别型号（内核 {info["core"] or "未知"}，没读到 DBGMCU_IDCODE）'

    parts = [info['name']]
    if info['flash_kb']:
        parts.append(f'{info["flash_kb"]} KB Flash')
    if info['core']:
        parts.append(info['core'])
    parts.append(f'DEV_ID 0x{info["dev_id"]:03X} REV 0x{info["rev"]:04X}')
    if info['uid']:
        parts.append(f'UID {info["uid"]}')

    return '，'.join(parts)
