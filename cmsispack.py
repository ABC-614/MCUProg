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
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


BASE = 'https://www.keil.com/pack/'

''' 用户明确放行过证书的主机。只在本次运行内有效，不落盘——
    下次再下还得再点一次头 '''
TRUSTED = set()


class CertExpired(Exception):
    ''' 站点的 HTTPS 证书验不过。单独立一个异常，是因为这个错误用户点一下头就能过，
        而别的错误（没这个文件之类）点头也没用，两者得分开处理 '''

    def __init__(self, host, detail):
        super(CertExpired, self).__init__(f'{host} 的 HTTPS 证书验证不通过：{detail}')

        self.host   = host
        self.detail = detail


def url_open(req, timeout=30):
    ''' 证书验不过不默默放行：取回来的 .FLM 是要在目标芯片上跑的二进制，
        跳过校验就等于没人担保它没在路上被换过。要放行，得用户自己说 '''
    host = urllib.parse.urlsplit(req.full_url).hostname

    try:
        if host in TRUSTED:
            return urllib.request.urlopen(req, timeout=timeout,
                                          context=ssl._create_unverified_context())

        return urllib.request.urlopen(req, timeout=timeout)

    except urllib.error.URLError as e:
        if isinstance(getattr(e, 'reason', None), ssl.SSLCertVerificationError):
            raise CertExpired(host, e.reason.verify_message or str(e.reason))

        raise

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
            with url_open(req, timeout) as r:
                return r.read(), r.status, r.headers

        except CertExpired:
            raise                       # 重试多少次都一样，等用户表态

        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:     # 没有这个文件，重试多少次都是一样的结果
                raise Exception(f'下载失败：{url}\nHTTP {e.code} {e.reason}')

            last = e
            time.sleep(0.5 * (attempt + 1))

        except Exception as e:
            last = e
            time.sleep(0.5 * (attempt + 1))

    raise Exception(f'下载失败：{url}\n{type(last).__name__}: {last}')


def content_length(url, timeout=30):
    req = urllib.request.Request(url, method='HEAD', headers=UA)
    with url_open(req, timeout) as r:
        return int(r.headers.get('Content-Length', 0))


''' ---------------- pdsc ---------------- '''


def load_pdsc(pack, progress=None, refresh=False, base=None):
    ''' pack 形如 Keil.STM32F4xx_DFP。返回 (ElementTree 根, 版本号) '''
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f'{pack}.pdsc')

    if refresh or not os.path.exists(path):
        if progress: progress(f'下载 {pack}.pdsc …')

        data = try_sources(pack_sources(pack, base, progress)[0],
                           lambda src: fetch(f'{src}{pack}.pdsc')[0], progress)

        with open(path, 'wb') as f:
            f.write(data)

    root = ET.parse(path).getroot()

    release = root.find('releases/release')
    version = release.get('version') if release is not None else None

    return root, version


def try_sources(sources, grab, progress=None):
    ''' 挨个源试过去。全都不行时优先报证书问题——那个用户点个头就能解决，
        而排在后面的源多半只是"根本没有这个文件"，报出来没用 '''
    cert = None

    for i, src in enumerate(sources):
        try:
            return grab(src)

        except CertExpired as e:
            cert = cert or e

            if progress: progress(f'{src} 取不到：{e}')

        except Exception as e:
            if progress: progress(f'{src} 取不到：{e}')

            if i == len(sources) - 1 and cert is None:
                raise

    raise cert or Exception('没有可用的下载源')


def is_ram(mem):
    ''' 新写法用 access="rwx"，老 pack（比如 STM32F1xx_DFP）用 id="IRAM1" '''
    if mem.get('access'):
        return 'w' in mem.get('access')

    return (mem.get('id') or '').upper().startswith('IRAM')


def is_rom(mem):
    if mem.get('access'):
        return 'w' not in mem.get('access') and 'r' in mem.get('access')

    return (mem.get('id') or '').upper().startswith('IROM')


def inherited(root):
    ''' pdsc 是继承式的，memory/algorithm 可能写在 family 或 subFamily 上，
        要一路往上找，所以先把父子关系建出来 '''
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

    return collect


def device_entries(root, prefix=None):
    ''' 列出 pdsc 里所有带片上 Flash 烧写算法的器件。

        几个坑：
        - Flash 基址各家不一样：ST/GD32/N32 在 0x08000000，HC32 在 0x00000000，
          所以基址得从器件自己的 memory 里取，不能写死；
        - H7 的 Flash 拆成 FLASH_Bank1 + FLASH_Bank2 两块，要加起来算总容量；
        - 算法条目的 size 是这个算法能覆盖的范围，不等于器件容量
          （STM32F103C6 只有 32 KB，用的却是 STM32F10x_128.FLM），
          所以容量看 memory、算法看 algorithm。 '''
    collect = inherited(root)

    for dev in root.iter('device'):
        name = dev.get('Dname') or ''
        if not name:
            continue
        if prefix and not name.upper().startswith(prefix.upper()):
            continue

        memories = collect(dev, 'memory')

        roms = [m for m in memories if is_rom(m)]
        if not roms:
            continue

        ''' 片上主 Flash 取基址最低的那块，同一片区内的多个 bank 累加 '''
        flash_start = min(int(m.get('start'), 0) for m in roms)
        total = sum(int(m.get('size'), 0) for m in roms
                    if flash_start <= int(m.get('start'), 0) < flash_start + 0x2000000)

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
            if not rams:
                continue

            '''挑最大的一块。不能按文档顺序取第一块——有些 pack 把一小块辅助 RAM
                排在前面，装不下算法；也不能认死 0x20000000 段——HC32F460 的主 SRAM
                在 0x1FFF8000，0x200F0000 反倒是块 4 KB 的小 RAM '''
            ram = max(rams, key=lambda m: int(m.get('size'), 0))

            ram_start, ram_size = int(ram.get('start'), 0), int(ram.get('size'), 0)

        yield {
            'name'       : name,
            'algorithm'  : algo.get('name').replace('\\', '/'),
            'flash_start': flash_start,
            'flash_size' : total,
            'ram_start'  : ram_start,
            'ram_size'   : min(ram_size, 0x8000),   # 算法用不了那么多，给 32 KB 封顶
        }


def find_device(root, prefix, flash_kb, flash_start=None):
    ''' 在 pdsc 里找一个器件：名字以 prefix 开头、片上 Flash 总容量对得上 '''
    best = None
    for dev in device_entries(root, prefix):
        if dev['flash_size'] != flash_kb * 1024:
            continue

        if flash_start is not None and dev['flash_start'] != flash_start:
            continue

        ''' 同容量的器件有好几个封装，取名字最短的那个当代表 '''
        if best is None or len(dev['name']) < len(best['name']):
            best = dev

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


''' ---------------- pack 索引 ---------------- '''

''' 所有已发布 pack 的总目录，各家厂商都在里面——GigaDevice、Nations、
    HDSC/XHSC 这些国产 MCU 的 pack 也是从这儿找 '''
INDEX = 'https://www.keil.com/pack/index.pidx'

INDEX_MAX_AGE = 7 * 24 * 3600


def load_index(progress=None, refresh=False):
    ''' 返回 [{vendor, name, pack, version, url}, ...]。索引不大（1 MB 上下），
        缓存一周，过期或手动刷新时才重新下载 '''
    say = progress or (lambda msg: None)

    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, 'index.pidx')

    stale = not os.path.exists(path) or time.time() - os.path.getmtime(path) > INDEX_MAX_AGE

    if refresh or stale:
        say('下载 pack 索引 …')

        data, status, _ = fetch(INDEX, timeout=60)
        with open(path, 'wb') as f:
            f.write(data)

    packs = []
    for pdsc in ET.parse(path).getroot().iter('pdsc'):
        vendor, name = pdsc.get('vendor'), pdsc.get('name')
        if not vendor or not name:
            continue

        name = name[:-5] if name.lower().endswith('.pdsc') else name

        packs.append({'vendor' : vendor,
                      'name'   : name,
                      'pack'   : f'{vendor}.{name}',
                      'version': pdsc.get('version'),
                      'url'    : pdsc.get('url')})

    return packs


def pack_index_entry(pack, progress=None):
    ''' 在索引里查这个 pack 的登记信息，查不到（或索引拉不下来）返回 None '''
    try:
        return next((p for p in load_index(progress) if p['pack'] == pack), None)

    except Exception as e:
        if progress: progress(f'读 pack 索引失败：{e}')

        return None


def pack_sources(pack, url=None, progress=None):
    ''' 一个 pack 该去哪儿下，按优先级排。

        第三方 pack 在 keil.com 上只镜像了 .pdsc，.pack 本体得回厂商自己的服务器取
        （NSING 在 nsing.com.sg，兆易在 gd32mcu.com，华大干脆放在 GitHub 上），
        所以厂商源要排在 keil.com 前面。 '''
    entry = pack_index_entry(pack, progress)

    sources = [url, entry and entry['url'], BASE]
    versions = [entry and entry['version']]

    ''' 去重，保持顺序 '''
    return (list(dict.fromkeys([s for s in sources if s])),
            list(dict.fromkeys([v for v in versions if v])))


def search_packs(keyword, packs=None, progress=None):
    ''' 按关键字在厂商名和 pack 名里找。输入 N32G45 能找到 Nations.N32G45x_DFP，
        输入 GD32 能把 GigaDevice 的一串 pack 都列出来 '''
    packs = packs if packs is not None else load_index(progress)

    key = keyword.strip().upper()
    if not key:
        return []

    hit = [p for p in packs if key in p['pack'].upper()]

    ''' 型号越长越具体的排前面，同名的按厂商字母序 '''
    hit.sort(key=lambda p: (not p['name'].upper().startswith(key), p['pack']))

    return hit


def list_devices(pack, url=None, progress=None, refresh=False):
    ''' 列出一个 pack 里所有带烧写算法的器件，返回 (器件表, pack 版本号) '''
    root, version = load_pdsc(pack, progress, refresh, base=url)

    devices = sorted(device_entries(root), key=lambda d: d['name'])

    return devices, version


''' ---------------- 对外 ---------------- '''


def grab_algorithm(pack, version, dev, algo_dir, url=None, progress=None):
    ''' 把 dev 指定的那个 .FLM 取下来，返回补上 path 字段的 dev '''
    say = progress or (lambda msg: None)

    os.makedirs(algo_dir, exist_ok=True)
    path = os.path.join(algo_dir, os.path.basename(dev['algorithm']))

    ''' 绝不覆盖已有的算法文件：本地那份可能是用户自己改过、或者正在被程序占用的 '''
    if os.path.exists(path):
        say(f'本地已有 {os.path.basename(path)}，直接用它，不重新下载')

        dev['path'] = path

        return dev

    sources, versions = pack_sources(pack, url, say)

    ''' pdsc 里写的版本和索引里登记的偶尔对不上（本地 pdsc 缓存旧了），两个都试 '''
    versions = list(dict.fromkeys([v for v in [version] + versions if v]))

    def grab(src):
        return try_sources([f'{src}{pack}.{v}.pack' for v in versions],
                           lambda u: remote_zip_member(u, dev['algorithm'], say))

    data = try_sources(sources, grab, say)

    with open(path, 'wb') as f:
        f.write(data)

    say(f'算法已保存：{os.path.basename(path)}（{len(data)} 字节）')

    dev['path'] = path

    return dev


def download_algorithm(pack, prefix, flash_kb, algo_dir, url=None, flash_start=None, progress=None):
    ''' 按 系列pack + 器件名前缀 + Flash 容量 找到算法并下载到 algo_dir。
        返回 dict：name / path / ram_start / ram_size / flash_start / flash_size '''
    say = progress or (lambda msg: None)

    root, version = load_pdsc(pack, say, base=url)

    dev = find_device(root, prefix, flash_kb, flash_start)
    if dev is None:
        raise Exception(f'{pack} 里没有找到 {prefix}* 且 Flash 为 {flash_kb} KB 的器件')

    say(f'匹配到 {dev["name"]}，算法 {dev["algorithm"]}')

    return grab_algorithm(pack, version, dev, algo_dir, url, say)


def find_svd(root, dname):
    ''' 器件的 .svd 在 pack 里的路径。<debug svd=...> 可能挂在器件上
        （NSING、华大），也可能挂在 subFamily 上（Keil 的 STM32 包），所以要顺着
        继承链往上找 '''
    collect = inherited(root)

    for dev in root.iter('device'):
        if dev.get('Dname') != dname:
            continue

        for dbg in collect(dev, 'debug'):
            if dbg.get('svd'):
                return dbg.get('svd').replace('\\', '/')

    return None


def download_svd(pack, dname, out_dir, url=None, progress=None):
    ''' 取器件的外设寄存器定义，返回本地路径 '''
    say = progress or (lambda msg: None)

    root, version = load_pdsc(pack, say, base=url)

    member = find_svd(root, dname)
    if member is None:
        raise Exception(f'{pack} 里没有登记 {dname} 的 .svd')

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.basename(member))

    if os.path.exists(path):
        say(f'本地已有 {os.path.basename(path)}')

        return path

    sources, versions = pack_sources(pack, url, say)
    versions = list(dict.fromkeys([v for v in [version] + versions if v]))

    def grab(src):
        return try_sources([f'{src}{pack}.{v}.pack' for v in versions],
                           lambda u: remote_zip_member(u, member, say))

    data = try_sources(sources, grab, say)

    with open(path, 'wb') as f:
        f.write(data)

    say(f'外设定义已保存：{os.path.basename(path)}（{len(data)//1024} KB）')

    return path


def download_algorithm_named(pack, dname, algo_dir, url=None, progress=None):
    ''' 按 pdsc 里的器件全名取算法，给"搜索型号"那条路用 '''
    say = progress or (lambda msg: None)

    root, version = load_pdsc(pack, say, base=url)

    dev = next((d for d in device_entries(root) if d['name'] == dname), None)
    if dev is None:
        raise Exception(f'{pack} 里没有器件 {dname}')

    say(f'{dev["name"]}，算法 {dev["algorithm"]}')

    return grab_algorithm(pack, version, dev, algo_dir, url, say)


if __name__ == '__main__':
    import sys

    algo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'FlashAlgo')

    if len(sys.argv) > 1 and sys.argv[1] == 'search':
        for p in search_packs(sys.argv[2], progress=print)[:40]:
            print(f'{p["pack"]:<48} {p["version"]:<12} {p["url"]}')

    elif len(sys.argv) > 1 and sys.argv[1] == 'devices':
        devices, version = list_devices(sys.argv[2], progress=print)
        print(f'{sys.argv[2]} {version}：{len(devices)} 个器件')
        for d in devices:
            print(f'  {d["name"]:<24} 0x{d["flash_start"]:08X} + {d["flash_size"]//1024:>5} KB  {d["algorithm"]}')

    else:
        pack, prefix, kb = (sys.argv + ['Keil.STM32F4xx_DFP', 'STM32F407', '512'])[1:4]

        print(download_algorithm(pack, prefix, int(kb), algo_dir, progress=print))
