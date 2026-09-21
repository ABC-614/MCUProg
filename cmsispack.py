#! python3
''' 从 Keil 的 CMSIS-Pack 服务器取烧写算法。

    整包动辄几 MB 到上百 MB，而我们只要里面一个几百字节的 .FLM，所以这里用 HTTP Range
    请求按 ZIP 的结构定位：先取末尾的中央目录，找到那一个成员，再只下它那几百字节。
    服务器不支持 Range 时退回整包下载。
'''
import io
import os
import time
import struct
import zlib
import socket
import urllib.request
import xml.etree.ElementTree as ET


BASE = 'https://www.keil.com/pack/'

UA = {'User-Agent': 'Mozilla/5.0 (MCUProg)'}

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FlashAlgo', '.packcache')


def fetch(url, byte_range=None, timeout=30, retries=3):
    ''' keil.com 偶尔会在握手阶段断开，重试几次 '''
    headers = dict(UA)
    if byte_range:
        headers['Range'] = f'bytes={byte_range[0]}-{byte_range[1]}'

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status, r.headers

        except Exception as e:
            last = e
            time.sleep(0.5 * (attempt + 1))

    raise Exception(f'下载失败：{url}\n{type(last).__name__}: {last}')


def content_length(url, timeout=30):
    req = urllib.request.Request(url, method='HEAD', headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers.get('Content-Length', 0))


''' ---------------- pdsc ---------------- '''


def load_pdsc(pack, progress=None, refresh=False):
    ''' pack 形如 Keil.STM32F4xx_DFP。返回 (ElementTree 根, 版本号) '''
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{pack}.pdsc')

    if refresh or not os.path.exists(path):
        if progress: progress(f'下载 {pack}.pdsc …')

        data, status, _ = fetch(f'{BASE}{pack}.pdsc')
        with open(path, 'wb') as f:
            f.write(data)

    root = ET.parse(path).getroot()

    release = root.find('releases/release')
    version = release.get('version') if release is not None else None

    return root, version


def is_ram(mem):
    ''' 新写法用 access="rwx"，老 pack（比如 STM32F1xx_DFP）用 id="IRAM1" '''
    if mem.get('access'):
        return 'w' in mem.get('access')

    return (mem.get('id') or '').upper().startswith('IRAM')


def is_rom(mem):
    if mem.get('access'):
        return 'w' not in mem.get('access') and 'r' in mem.get('access')

    return (mem.get('id') or '').upper().startswith('IROM')


def find_device(root, prefix, flash_kb, flash_start=0x08000000):
    ''' 在 pdsc 里找一个器件：名字以 prefix 开头、片上 Flash 总容量对得上。

        几个坑：
        - H7 的 Flash 拆成 FLASH_Bank1 + FLASH_Bank2 两块，要加起来算总容量；
        - 算法条目的 size 是这个算法能覆盖的范围，不等于器件容量
          （STM32F103C6 只有 32 KB，用的却是 STM32F10x_128.FLM），所以容量看 memory、算法看 algorithm；
        - pdsc 是继承式的，memory/algorithm 可能写在 family 或 subFamily 上，要一路往上找。 '''
    parent = {child: node for node in root.iter() for child in node}

    def chain(node):
        out = []
        while node is not None:
            out.insert(0, node)
            node = parent.get(node)
        return out

    def collect(dev, tag):
        found = []
        for node in chain(dev):
            found += [el for el in node if el.tag == tag]
        return found

    best = None
    for dev in root.iter('device'):
        name = dev.get('Dname', '')
        if not name.startswith(prefix):
            continue

        memories = collect(dev, 'memory')

        total = sum(int(m.get('size'), 0) for m in memories
                    if is_rom(m) and flash_start <= int(m.get('start'), 0) < flash_start + 0x2000000)
        if total != flash_kb * 1024:
            continue

        algos = [a for a in collect(dev, 'algorithm')
                 if int(a.get('start'), 0) == flash_start and a.get('default', '1') == '1']
        if not algos:
            continue

        ''' 有多个候选时，取能覆盖住整片 Flash 且范围最小的那个 '''
        algos.sort(key=lambda a: (int(a.get('size'), 0) < total, int(a.get('size'), 0)))
        algo = algos[0]

        if algo.get('RAMstart'):        # H7 这类 pack 直接在算法上写明了要用哪块 RAM
            ram_start = int(algo.get('RAMstart'), 0)
            ram_size  = int(algo.get('RAMsize'), 0)

        else:
            rams = [m for m in memories if is_ram(m)]
            ram = next((m for m in rams if int(m.get('start'), 0) >> 24 == 0x20), None) or (rams[0] if rams else None)
            if ram is None:
                continue

            ram_start, ram_size = int(ram.get('start'), 0), int(ram.get('size'), 0)

        candidate = {
            'name'       : name,
            'algorithm'  : algo.get('name').replace('\\', '/'),
            'flash_start': flash_start,
            'flash_size' : total,
            'ram_start'  : ram_start,
            'ram_size'   : min(ram_size, 0x8000),   # 算法用不了那么多，给 32 KB 封顶
        }

        ''' 同容量的器件有好几个封装，取名字最短的那个当代表 '''
        if best is None or len(name) < len(best['name']):
            best = candidate

    return best


''' ---------------- 从远端 zip 里只取一个成员 ---------------- '''


def remote_zip_member(url, member, progress=None):
    ''' 用 Range 请求从远端 zip 中取出 member 的内容 '''
    size = content_length(url)

    if progress: progress(f'整包 {size/1024/1024:.1f} MB，只取其中的 {os.path.basename(member)}')

    tail_len = min(size, 66 * 1024)     # 中央目录在末尾，注释最长 64 KB
    tail, status, _ = fetch(url, (size - tail_len, size - 1))

    if status != 206:                   # 服务器不认 Range，只能整包下
        if progress: progress('服务器不支持分段下载，改为下载整包…')

        whole, _, _ = fetch(url, timeout=300)

        import zipfile
        with zipfile.ZipFile(io.BytesIO(whole)) as z:
            return z.read(member)

    eocd = tail.rfind(b'PK\x05\x06')
    if eocd == -1:
        raise Exception('这个 pack 不是正常的 zip（找不到中央目录）')

    cd_size, cd_offset = struct.unpack_from('<II', tail, eocd + 12)

    if cd_offset == 0xFFFFFFFF:         # zip64
        z64 = tail.rfind(b'PK\x06\x06')
        if z64 == -1:
            raise Exception('zip64 格式但找不到 zip64 中央目录')
        cd_size, cd_offset = struct.unpack_from('<QQ', tail, z64 + 40)

    if size - tail_len <= cd_offset:
        cd = tail[cd_offset - (size - tail_len):][:cd_size]
    else:
        cd, _, _ = fetch(url, (cd_offset, cd_offset + cd_size - 1))

    entry = find_entry(cd, member)
    if entry is None:
        raise Exception(f'这个 pack 里没有 {member}')

    method, comp_size, local_offset = entry

    ''' 本地文件头里的 extra 字段长度不固定（有的包有好几百字节），不能靠猜，
        先把 30 字节定长头取下来，算出准确的数据偏移再取数据 '''
    head, _, _ = fetch(url, (local_offset, local_offset + 29))

    if head[:4] != b'PK\x03\x04':
        raise Exception('本地文件头不对，pack 可能损坏')

    name_len, extra_len = struct.unpack_from('<HH', head, 26)

    data_offset = local_offset + 30 + name_len + extra_len
    raw, _, _ = fetch(url, (data_offset, data_offset + comp_size - 1))

    if len(raw) != comp_size:
        raise Exception(f'取到的数据长度不对：{len(raw)} != {comp_size}')

    if method == 0:
        return raw

    return zlib.decompress(raw, -15)


def find_entry(cd, member):
    ''' 在中央目录里找成员，返回 (压缩方法, 压缩后长度, 本地头偏移) '''
    member = member.replace('\\', '/').lower()

    pos = 0
    while True:
        pos = cd.find(b'PK\x01\x02', pos)
        if pos == -1:
            return None

        method    = struct.unpack_from('<H', cd, pos + 10)[0]
        orig_size = struct.unpack_from('<I', cd, pos + 24)[0]
        comp_size = struct.unpack_from('<I', cd, pos + 20)[0]
        name_len, extra_len, comment_len = struct.unpack_from('<HHH', cd, pos + 28)
        local_off = struct.unpack_from('<I', cd, pos + 42)[0]
        name      = cd[pos + 46 : pos + 46 + name_len].decode('utf-8', 'replace')

        if name.replace('\\', '/').lower() == member:
            if 0xFFFFFFFF in (orig_size, comp_size, local_off):     # zip64：真实数值在 extra 里
                extra = cd[pos + 46 + name_len : pos + 46 + name_len + extra_len]
                comp_size, local_off = zip64_sizes(extra, orig_size, comp_size, local_off)

            return method, comp_size, local_off

        pos += 46 + name_len + extra_len + comment_len


def zip64_sizes(extra, orig_size, comp_size, local_off):
    ''' zip64 扩展字段：按 未压缩长度、压缩长度、本地头偏移 的顺序，
        只存放那些在定长区里被写成 0xFFFFFFFF 的项 '''
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from('<HH', extra, pos)
        body = extra[pos + 4 : pos + 4 + size]
        pos += 4 + size

        if tag != 0x0001:
            continue

        at = 0
        for name in ('orig', 'comp', 'off'):
            value = {'orig': orig_size, 'comp': comp_size, 'off': local_off}[name]
            if value != 0xFFFFFFFF or at + 8 > len(body):
                continue

            got = struct.unpack_from('<Q', body, at)[0]
            at += 8

            if name == 'comp': comp_size = got
            elif name == 'off': local_off = got

        break

    return comp_size, local_off


''' ---------------- 对外 ---------------- '''


def download_algorithm(pack, prefix, flash_kb, algo_dir, progress=None):
    ''' 按 系列pack + 器件名前缀 + Flash 容量 找到算法并下载到 algo_dir。
        返回 dict：name / path / ram_start / ram_size / flash_start / flash_size '''
    say = progress or (lambda msg: None)

    root, version = load_pdsc(pack, say)

    dev = find_device(root, prefix, flash_kb)
    if dev is None:
        raise Exception(f'{pack} 里没有找到 {prefix}* 且 Flash 为 {flash_kb} KB 的器件')

    say(f'匹配到 {dev["name"]}，算法 {dev["algorithm"]}')

    os.makedirs(algo_dir, exist_ok=True)
    path = os.path.join(algo_dir, os.path.basename(dev['algorithm']))

    ''' 绝不覆盖已有的算法文件：本地那份可能是用户自己改过、或者正在被程序占用的 '''
    if os.path.exists(path):
        say(f'本地已有 {os.path.basename(path)}，直接用它，不重新下载')

        dev['path'] = path

        return dev

    url = f'{BASE}{pack}.{version}.pack'

    data = remote_zip_member(url, dev['algorithm'], say)

    with open(path, 'wb') as f:
        f.write(data)

    say(f'算法已保存：{os.path.basename(path)}（{len(data)} 字节）')

    dev['path'] = path

    return dev


if __name__ == '__main__':
    import sys

    pack, prefix, kb = (sys.argv + ['Keil.STM32F4xx_DFP', 'STM32F407', '512'])[1:4]

    info = download_algorithm(pack, prefix, int(kb),
                              os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FlashAlgo'),
                              progress=print)
    print(info)
