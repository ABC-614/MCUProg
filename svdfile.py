#! python3
''' 读 CMSIS-SVD 里的外设寄存器定义。

    各家的 pack 里都带 .svd，把每个外设、每个寄存器、每个位域的名字和位置写清楚了。
    有它就不用再翻手册算偏移、手工拆位段。

    这里只管"读定义"，不碰目标芯片；谁去读寄存器的值是调用方的事。

    不依赖 cmsis_svd 包：这个项目是 PyInstaller 打包发出去的，能少一个外部依赖
    就少一个。SVD 的结构本身也不复杂，值得自己解。
'''
import re
import xml.etree.ElementTree as ET


def text(node, tag, default=None):
    if node is None:
        return default

    found = node.findtext(tag)

    return default if found is None else found.strip()


def number(value, default=None):
    ''' SVD 里的数字有 0x1A、42、#01x1 好几种写法 '''
    if value is None:
        return default

    value = value.strip()

    try:
        if value.startswith('#'):       # 二进制写法，x 是"任意位"，按 0 算
            return int(re.sub(r'[xX]', '0', value[1:]), 2)

        return int(value, 0)

    except ValueError:
        return default


''' 读了就会产生副作用的寄存器：读 USART_DAT 会把收到的字节从 FIFO 里取走，
    读某些状态寄存器会清掉标志位。SVD 本该用 readAction 标明，但不少厂商
    （NSING 这份就是）压根不写，只能按名字认个大概——所以这是启发式，不是判据，
    界面上要说清楚是"默认不读"而不是"不能读"。 '''
SIDE_EFFECT = re.compile(r'(^|_)(DR|DAT|DATA|RDR|TDR|RDATA|TDATA|RXD|TXD|FIFO|RBR|THR)$', re.I)


class Field(object):
    def __init__(self, name, offset, width, access, description, enums):
        self.name        = name
        self.offset      = offset
        self.width       = width
        self.access      = access
        self.description = description
        self.enums       = enums        # [(值, 名字, 说明), ...]

    @property
    def mask(self):
        return ((1 << self.width) - 1) << self.offset

    @property
    def bits(self):
        return f'{self.offset}' if self.width == 1 else f'{self.offset + self.width - 1}:{self.offset}'

    def extract(self, value):
        return (value >> self.offset) & ((1 << self.width) - 1)

    def explain(self, value):
        ''' 位域取值对应的枚举名，没有就返回空 '''
        raw = self.extract(value)

        for enum_value, name, _ in self.enums:
            if enum_value == raw:
                return name

        return ''


class Register(object):
    def __init__(self, name, offset, size, access, reset, description, fields, base=0):
        self.name        = name
        self.offset      = offset
        self.size        = size or 32
        self.access      = access or 'read-write'
        self.reset       = reset
        self.description = description
        self.fields      = fields
        self.base        = base

    @property
    def address(self):
        return self.base + self.offset

    @property
    def readable(self):
        return 'write-only' not in (self.access or '')

    @property
    def sensitive(self):
        ''' 读它可能有副作用，默认不主动读 '''
        return bool(SIDE_EFFECT.search(self.name))


class Peripheral(object):
    def __init__(self, name, base, group, description, registers):
        self.name        = name
        self.base        = base
        self.group       = group
        self.description = description
        self.registers   = registers

    def __repr__(self):
        return f'<Peripheral {self.name} @ 0x{self.base:08X}, {len(self.registers)} regs>'


class Device(object):
    def __init__(self, name, description, peripherals):
        self.name        = name
        self.description = description
        self.peripherals = peripherals


def field_bits(node):
    ''' 位段有三种写法，都得认：bitOffset+bitWidth、bitRange "[7:4]"、lsb/msb '''
    offset = number(text(node, 'bitOffset'))
    if offset is not None:
        return offset, number(text(node, 'bitWidth'), 1)

    span = text(node, 'bitRange')
    if span:
        match = re.match(r'\[?(\d+)\s*:\s*(\d+)\]?', span)
        if match:
            msb, lsb = int(match.group(1)), int(match.group(2))
            return lsb, msb - lsb + 1

    lsb, msb = number(text(node, 'lsb')), number(text(node, 'msb'))
    if lsb is not None and msb is not None:
        return lsb, msb - lsb + 1

    return None, None


def parse_fields(node, default_access):
    out = []

    for element in node.findall('fields/field'):
        offset, width = field_bits(element)
        if offset is None or not width:
            continue

        enums = []
        for item in element.findall('enumeratedValues/enumeratedValue'):
            value = number(text(item, 'value'))
            if value is not None:
                enums.append((value, text(item, 'name', ''), text(item, 'description', '')))

        out.append(Field(text(element, 'name', '?'), offset, width,
                         text(element, 'access', default_access),
                         text(element, 'description', ''), enums))

    out.sort(key=lambda f: f.offset)

    return out


def dim_names(name, count, increment, index_spec):
    ''' 寄存器数组：名字里的 %s 换成下标，地址按 dimIncrement 递推。
        dimIndex 可以是 "0-3" 或 "A,B,C" '''
    if index_spec:
        match = re.match(r'(\w+)\s*-\s*(\w+)$', index_spec.strip())
        if match:
            start, stop = match.group(1), match.group(2)
            if start.isdigit() and stop.isdigit():
                labels = [str(i) for i in range(int(start), int(stop) + 1)]
            else:
                labels = [chr(c) for c in range(ord(start), ord(stop) + 1)]
        else:
            labels = [part.strip() for part in index_spec.split(',')]
    else:
        labels = [str(i) for i in range(count)]

    labels = (labels + [str(i) for i in range(len(labels), count)])[:count]

    return [(name.replace('%s', label), i * increment) for i, label in enumerate(labels)]


def parse_registers(container, base, defaults, prefix_offset=0):
    ''' 把一个 <registers> 或 <cluster> 下的寄存器铺平成一张表 '''
    out = []

    for element in container:
        if element.tag == 'cluster':
            offset = number(text(element, 'addressOffset'), 0)

            out += parse_registers(element, base, defaults, prefix_offset + offset)
            continue

        if element.tag != 'register':
            continue

        name   = text(element, 'name', '?')
        offset = number(text(element, 'addressOffset'), 0) + prefix_offset
        size   = number(text(element, 'size'), defaults.get('size'))
        access = text(element, 'access', defaults.get('access'))
        reset  = number(text(element, 'resetValue'), defaults.get('reset'))
        desc   = text(element, 'description', '')

        fields = parse_fields(element, access)

        count = number(text(element, 'dim'))
        if count:
            step = number(text(element, 'dimIncrement'), 4)

            for each_name, delta in dim_names(name, count, step, text(element, 'dimIndex')):
                out.append(Register(each_name, offset + delta, size, access, reset, desc, fields, base))

        else:
            out.append(Register(name, offset, size, access, reset, desc, fields, base))

    out.sort(key=lambda r: r.offset)

    return out


def load(path):
    ''' 解析一个 .svd，返回 Device '''
    root = ET.parse(path).getroot()

    defaults = {'size'  : number(text(root, 'size'), 32),
                'access': text(root, 'access', 'read-write'),
                'reset' : number(text(root, 'resetValue'), 0)}

    nodes = {}
    for element in root.findall('peripherals/peripheral'):
        nodes[text(element, 'name', '?')] = element

    ''' derivedFrom 的外设只写了自己的基址，寄存器表要从被继承的那个拿 '''
    def registers_of(element, base, seen=()):
        container = element.find('registers')

        if container is not None:
            own = {'size'  : number(text(element, 'size'), defaults['size']),
                   'access': text(element, 'access', defaults['access']),
                   'reset' : number(text(element, 'resetValue'), defaults['reset'])}

            return parse_registers(container, base, own)

        parent = element.get('derivedFrom')
        if parent and parent in nodes and parent not in seen:
            return registers_of(nodes[parent], base, seen + (parent,))

        return []

    peripherals = []
    for name, element in nodes.items():
        base = number(text(element, 'baseAddress'), 0)

        parent = nodes.get(element.get('derivedFrom'))

        peripherals.append(Peripheral(
            name, base,
            text(element, 'groupName', text(parent, 'groupName', '') if parent is not None else ''),
            text(element, 'description', text(parent, 'description', '') if parent is not None else ''),
            registers_of(element, base)))

    peripherals.sort(key=lambda p: p.name)

    return Device(text(root, 'name', '?'), text(root, 'description', ''), peripherals)


if __name__ == '__main__':
    import sys

    device = load(sys.argv[1])

    print(f'{device.name}：{len(device.peripherals)} 个外设')

    for peripheral in device.peripherals:
        print(f'  {peripheral.name:<8} 0x{peripheral.base:08X}  {len(peripheral.registers):>3} 个寄存器  {peripheral.description[:40]}')

        if len(sys.argv) > 2 and peripheral.name == sys.argv[2]:
            for register in peripheral.registers:
                flag = '（读有副作用）' if register.sensitive else ''
                print(f'      +0x{register.offset:03X} {register.name:<16} {register.access:<12}{flag}')

                for field in register.fields:
                    print(f'            [{field.bits:>5}] {field.name}')
