#!/usr/bin/env python3
"""E-ink status display: draws the state of the server as a PNG sized for an e-ink reader and serves it over HTTP.

  eink_monitor.py                serve /frame.png (bind and port come from the "eink" section of config.json)
                                   /frame.png?w=&h=&v=&theme=&every=&bat=   w, h: screen size; v: variant; theme;
                                   every: the reader's refresh interval, printed in the corner; bat: its battery %
  eink_monitor.py sample FILE    draw one frame from invented data (no server state is read)
  eink_monitor.py selftest       check the logic, no network and no device

Service, disk and backup state come from check_services, check_disk and check_backup in the folder above.
Needs Pillow (pip install pillow). The checks themselves do not.
"""
import glob, io, json, math, os, shutil, socket, sys, threading, time
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import check_backup, check_disk, check_services
from common import OK, CRIT, failed_units, load_config, sh

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit('eink_monitor.py needs Pillow: pip install pillow')

GB = 1024 ** 3
VARIANTS = ('config', 'glance', 'top')
# role -> colour. dark = light text on a dark ground; paper = black ink on white, what an e-ink panel does best.
THEMES = {
    'dark': {'bg': '#141312', 'fg': '#ece6da', 'dim': '#8d877c', 'mute': '#5a564f', 'accent': '#e3b26b', 'ok': '#a9c4a0', 'warn': '#e3b26b', 'bad': '#d98f86'},
    'paper': {'bg': '#ffffff', 'fg': '#000000', 'dim': '#555555', 'mute': '#a8a8a8', 'accent': '#000000', 'ok': '#00897b', 'warn': '#c97800', 'bad': '#c4241b'},
}
# ponytail: module state set once from the config by configure(); a class would only carry the same two values around.
LIMITS, FONTS = [70, 90], {}
_cpu = {'prev': None, 'pct': 0.0}


def configure(e):
    LIMITS[:] = [e['warn_pct'], e['crit_pct']]
    FONTS.update(e['font'])
    font.cache_clear()


# ---- data ------------------------------------------------------------------------------------------------------------

def cpu_pct():
    """Busy % since the previous call, so callers should space calls at least a few seconds apart."""
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    idle, total = v[3] + v[4], sum(v)
    prev, _cpu['prev'] = _cpu['prev'], (idle, total)
    if prev and total > prev[1]:
        _cpu['pct'] = 100 * (1 - (idle - prev[0]) / (total - prev[1]))
    return _cpu['pct']


def sysfs(path):
    """A sysfs number, or None when the file is missing or the driver refuses to answer."""
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def parse_stats(text):
    """`docker stats` lines of name<TAB>cpu%<TAB>memory into {name: (cpu, memory in use)}."""
    out = {}
    for line in text.splitlines():
        name, cpu, mem = line.split('\t')
        out[name] = (float(cpu.rstrip('%')), mem.split(' / ')[0])
    return out


def service_rows(shown, manifest, containers, stats, units):
    """One row per service to show: name, broken or not, CPU % and memory (None for a systemd unit)."""
    kind = {s['name']: s['type'] for s in manifest}
    rows = []
    for name in shown:
        svc = {'name': name, 'type': kind.get(name, 'container')}
        broken = any(level == CRIT for level, _ in check_services.evaluate([svc], containers, units))
        cpu, mem = stats.get(name, (None, ''))
        rows.append({'name': name, 'bad': broken, 'cpu': cpu, 'mem': mem})
    return rows


def collect(cfg):
    """Everything one frame needs, as plain data. Findings come from the same functions the check scripts use."""
    e, manifest = cfg['eink'], cfg['services']
    with open('/proc/meminfo') as f:
        mem = {line.split(':')[0]: int(line.split()[1]) for line in f}
    with open('/proc/uptime') as f:
        up_days = int(float(f.read().split()[0])) // 86400
    drives = []
    for m in e['mounts']:
        u = shutil.disk_usage(m) if os.path.ismount(m) else None  # an unmounted folder would report its parent disk
        drives.append({'mount': m, 'pct': check_disk.used_pct(u.used, u.free) if u else None,
                       'free': u.free / GB if u else 0, 'total': (u.used + u.free) / GB if u else 0})
    rc, ps = sh(['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.State}}\t{{.Status}}'])
    stats = parse_stats(sh(['docker', 'stats', '--no-stream', '--format', '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'], timeout=20)[1])
    units = {s['name']: sh(['systemctl', 'is-active', s['name']])[1].strip() or 'unknown' for s in manifest if s['type'] == 'systemd'}
    findings = (check_disk.run({'disk': {**cfg['disk'], 'mounts': e['mounts'], 'top': 0}}) + check_services.run(cfg)
                + check_backup.run(cfg) + [(CRIT, 'failed systemd unit: ' + u) for u in failed_units()])
    g = e['gpu']
    if g:
        base, temps = g['path'], glob.glob(g['path'] + 'hwmon/hwmon*/temp1_input')  # the hwmon number changes between boots
        temp = sysfs(temps[0]) if temps else None
        g = {'name': g['name'], 'busy': sysfs(base + 'gpu_busy_percent'), 'vram': sysfs(base + 'mem_info_vram_used'),
             'vram_total': sysfs(base + 'mem_info_vram_total'), 'temp': temp / 1000 if temp else None}
    temp = sysfs('/sys/class/thermal/thermal_zone0/temp')
    return {'host': socket.gethostname(), 'up_days': up_days, 'load': '%.2f' % os.getloadavg()[0], 'cpus': os.cpu_count(),
            'cpu': cpu_pct(), 'ram_pct': 100 * (mem['MemTotal'] - mem['MemAvailable']) // mem['MemTotal'],
            'ram_gb': '%.0f' % ((mem['MemTotal'] - mem['MemAvailable']) / 2 ** 20), 'ram_total_gb': '%.0f' % (mem['MemTotal'] / 2 ** 20),
            'temp_cpu': temp / 1000 if temp else None, 'gpu': g, 'drives': drives,
            'services': service_rows(e['services'] or [s['name'] for s in manifest], manifest,
                                     check_services.parse_ps(ps) if rc == 0 else {}, stats, units),
            'alerts': [text for level, text in findings if level != OK]}


def sample_data():
    """Invented numbers and names, for the README picture and for trying the layout without a server."""
    names = ['web', 'db', 'media', 'cache', 'proxy', 'worker', 'search', 'queue', 'mail', 'metrics', 'git', 'files']
    svcs = [{'name': n, 'bad': n == 'worker', 'cpu': c, 'mem': '%d MiB' % m}
            for n, c, m in zip(names, (12.4, 3.1, 48.0, 0.6, 1.9, 0.0, 6.2, 0.4, 0.2, 2.8, 0.3, 1.1), (900, 1400, 2200, 80, 40, 0, 600, 120, 60, 300, 90, 150))]
    return {'host': 'homelab', 'up_days': 47, 'load': '1.67', 'cpus': 16, 'cpu': 23.0, 'ram_pct': 34, 'ram_gb': '22', 'ram_total_gb': '63',
            'temp_cpu': 52.0, 'gpu': {'name': 'GPU', 'busy': 4, 'vram': 2.1 * GB, 'vram_total': 8 * GB, 'temp': 47.0},
            'drives': [{'mount': '/', 'pct': 91, 'free': 41.0, 'total': 458.0}, {'mount': '/data', 'pct': 57, 'free': 1550.0, 'total': 3600.0}],
            'services': svcs,
            'alerts': ['/: 91% used, 41 GB free', 'worker: container is exited (Exited (137) 2 hours ago)']}


# ---- layout: everything sits on a monospace character grid -----------------------------------------------------------
# A line is (scale, [segments]); a segment is (col, text, role, bold), a bar (col, cells, pct) or a block ('fill', col, cells, role).
# Scale N draws the line N rows tall with N-times type, and its columns count in N-times cells.

def tone(p):
    return 'bad' if p >= LIMITS[1] else 'warn' if p >= LIMITS[0] else 'ok'


def clip(s, n):
    return s if len(s) <= n else s[:max(0, n - 1)] + '~'


def T(col, text, role='fg', bold=False):
    return (col, str(text), role, bold)


def R(cols, text, role='fg', bold=False):
    return (cols - len(text), text, role, bold)


def L(*segs, scale=1):
    return (scale, list(segs))


def rule(cols):
    return L(T(0, '-' * cols, 'mute'))


def used(lines):
    return sum(k for k, _ in lines)


def head(c, name, note='', col=0, num=None):
    if num is None:  # sections number themselves in the order they are built; pass num for one built out of reading order
        c.n += 1
        num = c.n
    return [T(col, '[', 'dim'), T(col + 2, '%02d' % num, 'accent', True), T(col + 5, '/', 'dim'), T(col + 7, name, 'fg', True),
            T(col + 8 + len(name), ']', 'dim'), T(col + 11 + len(name), note, 'dim')]


def cells(rows, room, by_cpu=False):
    """Services as (mark, name, cpu, mem, broken), broken first. When they do not fit, the last cell counts the rest."""
    on = sorted(rows, key=lambda r: (not r['bad'], -(r['cpu'] or 0) if by_cpu else 0, r['name']))
    out = [('!' if r['bad'] else '+', r['name'], r['cpu'], r['mem'], r['bad']) for r in on]
    if len(out) > room:
        out = out[:max(0, room - 1)] + [('~', '+%d more' % (len(out) - room + 1), None, '', False)] if room > 0 else []
    return out


def gpu_text(c):
    g, vram = c.d['gpu'], c.d['gpu']['vram'] is not None and c.d['gpu']['vram_total']
    t = lambda v: 'no reading' if v is None else '%d C' % v
    return SimpleNamespace(busy=g['busy'], vram_pct=100 * g['vram'] / g['vram_total'] if vram else None,
                           vram='%.1f / %.1f GB' % (g['vram'] / GB, g['vram_total'] / GB) if vram else 'no reading',
                           temps='cpu %s   gpu %s' % (t(c.d['temp_cpu']), t(g['temp'])))


def main_meters(c):
    """(label, pct or None, detail) for cpu, ram and each drive."""
    d = c.d
    return [('cpu', d['cpu'], 'load %s / %s cores' % (d['load'], d['cpus'])), ('ram', d['ram_pct'], '%s / %s GB' % (d['ram_gb'], d['ram_total_gb']))] + \
           [(v['mount'], v['pct'], '%d GB free of %d' % (v['free'], v['total'])) for v in d['drives']]


def clock(t):
    return t.strftime('%I:%M %p').lstrip('0')  # 12-hour, always


def b_header(c, cols):
    return [L(T(0, c.e['title'], 'fg', True), R(cols, clock(c.now), 'accent', True)),
            L(T(0, '%s | up %s d' % (c.d['host'], c.d['up_days']), 'dim'), R(cols, c.now.strftime('%a %d %b').upper(), 'dim')), rule(cols)]


def meter(cols, label, pct, detail):  # pct None = no reading: empty bar and "--", never a fake 0
    nb = max(6, cols - 41)
    return L(T(2, label, 'dim'), T(9, '[', 'dim'), (10, nb, pct), T(10 + nb, ']', 'dim'),
             T(12 + nb, ' -- %' if pct is None else '%3d %%' % pct, 'dim' if pct is None else tone(pct), True), T(19 + nb, clip(detail, cols - 19 - nb), 'dim'))


def gpu_meters(c, cols):
    g = gpu_text(c)
    name = c.d['gpu']['name']
    return [meter(cols, 'gpu', g.busy, name if g.busy is not None else (name + ', no reading').strip(', ')), meter(cols, 'vram', g.vram_pct, g.vram)]


def temp_row(c):
    return L(T(2, 'temp', 'dim'), T(9, gpu_text(c).temps if c.d['gpu'] else 'cpu %s' % ('no reading' if c.d['temp_cpu'] is None else '%d C' % c.d['temp_cpu'])))


def b_config(c, cols):
    """The default look: config-file rows, one type size."""
    m = main_meters(c)
    return [L(*head(c, 'SYSTEM')), meter(cols, *m[0]), meter(cols, *m[1]), *(gpu_meters(c, cols) if c.d['gpu'] else []), temp_row(c),
            rule(cols), L(*head(c, 'STORAGE')), *[meter(cols, *x) for x in m[2:]], rule(cols)]


def b_alert_list(c, cols, num):
    rows = [L(T(2, '!', 'bad', True), T(4, clip(a, cols - 4), 'bad')) for a in c.d['alerts'][:4]] or [L(T(2, 'ok', 'ok', True), T(5, 'nothing needs you', 'dim'))]
    return [rule(cols), L(*head(c, 'ALERTS', str(len(c.d['alerts'])) if c.d['alerts'] else '', num=num))] + rows


def b_alert_block(c, cols):
    """Trouble goes on top as a solid block: a shape, so it still reads when the panel washes the colour out."""
    al = c.d['alerts']
    if not al:
        return [L(T(0, '[ ok ]', 'ok', True), T(7, 'nothing needs you', 'dim')), rule(cols)]
    return [L(('fill', 0, cols, 'bad'), T(1, '!! %d ALERT%s' % (len(al), 'S' if len(al) > 1 else ''), 'bg', True))] + \
           [L(T(1, '!', 'bad', True), T(3, clip(a, cols - 3))) for a in al[:4]] + [rule(cols)]


def b_big(c, cols):
    """Hero numbers, two per row, a few rows tall. A number in trouble sits on a solid block."""
    half, out, m, k = cols // 2, [], main_meters(c) + ([('gpu', gpu_text(c).busy, 'vram ' + gpu_text(c).vram)] if c.d['gpu'] else []), 4 if cols >= 48 else 3
    for i in range(0, len(m), 2):
        labels, nums, bars, notes = [], [], [], []
        for j, (label, pct, detail) in enumerate(m[i:i + 2]):
            c0, txt = j * half, '--' if pct is None else '%d%%' % pct
            labels += head(c, label.upper() if label[0] != '/' else 'DISK ' + label, col=c0)
            if pct is not None and pct >= LIMITS[0]:
                nums += [('fill', c0 / k, len(txt), tone(pct)), T(c0 / k, txt, 'bg', True)]
            else:
                nums.append(T(c0 / k, txt, 'fg' if pct is not None else 'dim', True))
            bars += [T(c0, '[', 'dim'), (c0 + 1, half - 5, pct), T(c0 + half - 4, ']', 'dim')]
            notes.append(T(c0, clip(detail, half - 1), 'dim'))
        out += [L(*labels), L(*nums, scale=k), L(*bars), L(*notes), L()]
    return out[:-1] + [rule(cols), temp_row(c), rule(cols)]


def b_compact(c, cols):
    """Meters in as few rows as they fit, values in plain ink, colour only in the bars."""
    m = [x[:2] for x in main_meters(c)] + ([('gpu', gpu_text(c).busy), ('vram', gpu_text(c).vram_pct)] if c.d['gpu'] else [])
    per = 2 if cols >= 60 else 1
    half, out = cols // per, [L(*head(c, 'SYSTEM'))]
    nb = half - 17
    for i in range(0, len(m), per):
        segs = []
        for j, (label, pct) in enumerate(m[i:i + per]):
            c0 = j * half
            segs += [T(c0 + 2, label, 'dim'), T(c0 + 8, ' -- %' if pct is None else '%3d %%' % pct, 'dim' if pct is None else 'fg', True),
                     T(c0 + 14, '[', 'dim'), (c0 + 15, nb, pct), T(c0 + 15 + nb, ']', 'dim')]
        out.append(L(*segs))
    return out + [temp_row(c), rule(cols)]


def services_head(c, cols):
    n = sum(not r['bad'] for r in c.d['services'])
    return L(*head(c, 'SERVICES', '%d up / %d failing' % (n, len(c.d['services']) - n)))


def b_grid(c, cols, rows, by_cpu=False):
    """Names + CPU % in as many columns as fit, filled down then across."""
    ncol = max(1, cols // 32)
    cwid, out = cols // ncol, [services_head(c, cols)]
    cl = cells(c.d['services'], (rows - 1) * ncol, by_cpu)
    per = math.ceil(len(cl) / ncol)
    for r in range(per):
        segs = []
        for k in range(ncol):
            if k * per + r < len(cl):
                mark, name, cpu, _, isbad = cl[k * per + r]
                c0, nw = k * cwid, cwid - 9
                segs += [T(c0 + 2, mark, 'bad' if isbad else 'ok', True), T(c0 + 4, clip(name, nw), 'bad' if isbad else 'fg')]
                if cpu is not None:
                    segs.append(T(c0 + nw + 5, ('%4.1f' if cpu < 100 else '%4.0f') % cpu, 'warn' if cpu >= 50 else 'dim'))
        out.append(L(*segs))
    return out


def b_table(c, cols, rows):
    """One service per row, busiest first: name, CPU %, memory."""
    nw, out = cols - 21, [services_head(c, cols)]
    for mark, name, cpu, mem, isbad in cells(c.d['services'], rows - 1, by_cpu=True):
        segs = [T(2, mark, 'bad' if isbad else 'ok', True), T(4, clip(name, nw), 'bad' if isbad else 'fg')]
        if cpu is not None:
            segs += [T(5 + nw, '%5.1f%%' % cpu), R(cols, mem, 'dim')]
        out.append(L(*segs))
    return out


def compose(c, v, cols, rows, land):
    """Panes as (col offset, row offset, lines). Portrait stacks everything; landscape puts the numbers left and services right."""
    hd = b_header(c, cols)
    body = rows - used(hd)
    lw = (4 if v == 'top' else 5) * cols // 9 if land else cols  # landscape split: the table wants width, the big numbers want it more
    rw = cols - lw - 3 if land else cols
    if v == 'config':
        stats = b_config(c, lw)
        tail = b_alert_list(c, lw, c.n + 2)  # built before the services (their height depends on it) but read after them
        dock = lambda r: b_grid(c, rw, r)
    else:
        stats, tail = b_alert_block(c, lw) + (b_big(c, lw) if v == 'glance' else b_compact(c, lw)), []
        dock = (lambda r: b_grid(c, rw, r, by_cpu=True)) if v == 'glance' else (lambda r: b_table(c, rw, r))
    if land:
        left = stats[:-1] + [L()] * (body - used(stats[:-1]) - used(tail)) + tail  # drop the trailing rule, pin the tail to the bottom
        return [(0, 0, hd), (0, used(hd), left), (lw + 1, used(hd), [L(T(0, '|', 'mute'))] * body), (lw + 3, used(hd), dock(body))]
    mid = stats + dock(body - used(stats) - used(tail))
    return [(0, 0, hd + mid + [L()] * (body - used(mid) - used(tail)) + tail)]


# ---- drawing ---------------------------------------------------------------------------------------------------------

@lru_cache(maxsize=16)
def font(size, bold):
    path = FONTS['bold' if bold else 'regular']
    try:
        return ImageFont.truetype(path, size)
    except OSError as err:  # a proportional fallback keeps the screen readable, but the columns will not line up
        print(f'font {path}: {err}; using the built-in font (set "font" in config.json)', file=sys.stderr)
        return ImageFont.load_default(size)


def grid(w, h):
    size = max(8, min(min(w, h) // 42, max(w, h) // 56))
    cw, lh = font(size, False).getlength('M'), round(size * 1.4)
    cols, rows = int((w - 2 * size) // cw), int((h - 2 * size) // lh)
    return SimpleNamespace(size=size, cw=cw, lh=lh, cols=cols, rows=rows, x0=(w - cols * cw) / 2, y0=(h - rows * lh) / 2)


def paint(dr, th, g, panes, y0):
    for pc, pr, lines in panes:
        y = y0 + pr * g.lh
        for k, segs in lines:
            ax, u, ht = g.x0 + pc * g.cw, g.cw * k, g.lh * k  # pane origin, cell width and row height at this line's scale
            for seg in segs:
                if seg[0] == 'fill':
                    _, col, n, role = seg
                    dr.rectangle([ax + col * u - g.cw * .3, y + ht * .06, ax + (col + n) * u + g.cw * .3, y + ht * .94], fill=th[role])
                elif len(seg) == 3:
                    col, n, pct = seg
                    for i in range(n):
                        x = ax + (col + i) * u
                        if pct and i < round(n * pct / 100):
                            dr.rectangle([x + u * .12, y + ht * .22, x + u * .88, y + ht * .78], fill=th[tone(pct)])
                        else:
                            dr.rectangle([x + u / 2 - 2, y + ht / 2 - 2, x + u / 2 + 1, y + ht / 2 + 1], fill=th['mute'])
                else:
                    col, text, role, bold = seg
                    dr.text((ax + col * u, y + round(ht * (.7 if k == 1 else .76))), text, font=font(g.size * k, bold), fill=th[role], anchor='ls')
            y += g.lh * k


def footer(every, cols, bat=None):
    """The bottom row, right-aligned under the clock: how often this screen redraws. A plain fact, so it is never stale."""
    text = 'refreshes every %s' % ('%d sec' % every if every < 120 else '%d min' % (every // 60))
    return [L(R(cols, ('battery %d%%   ' % bat if bat is not None else '') + text, 'dim'))]


def render(d, e, w, h, v, theme, every=None, bat=None):  # every = the asker's refresh interval; None = no footer
    th, g = THEMES[theme], grid(w, h)
    c = SimpleNamespace(d=d, e=e, now=datetime.now().astimezone(), n=0)
    img = Image.new('RGB', (w, h), th['bg'])
    panes = []
    for pc, pr, lines in compose(c, v, g.cols, g.rows - 1, w > h):  # a screen too small for a layout loses its bottom, never the footer
        while lines and pr + used(lines) > g.rows - 1:
            lines = lines[:-1]
        panes.append((pc, pr, lines))
    paint(ImageDraw.Draw(img), th, g, panes + ([(0, g.rows - 1, footer(every, g.cols, bat))] if every else []), g.y0)
    return img


# ---- server ----------------------------------------------------------------------------------------------------------
_lock, _snap, _img = threading.Lock(), {'at': 0, 'd': None}, {}


def frame(cfg, *key):  # key = (w, h, variant, theme, every, bat)
    with _lock:
        if time.time() - _snap['at'] > 10:  # one data snapshot per 10 s, so CPU % stays a real average however many readers ask
            _snap.update(at=time.time(), d=collect(cfg))
            _img.clear()
        if key not in _img:
            if len(_img) >= 40:  # arbitrary sizes can be asked for; never let the cache grow without bound
                _img.clear()
            buf = io.BytesIO()
            render(_snap['d'], cfg['eink'], *key).save(buf, 'PNG')
            _img[key] = buf.getvalue()
        return _img[key]


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u, e = urlparse(self.path), self.server.cfg['eink']
        if u.path != '/frame.png':
            return self.send_error(404)
        q = parse_qs(u.query)
        try:
            w, h, every, bat = (int(q.get(k, [v])[0]) for k, v in (('w', e['width']), ('h', e['height']), ('every', 0), ('bat', -1)))
        except ValueError:
            return self.send_error(400)
        v, theme = q.get('v', [e['variant']])[0], q.get('theme', [e['theme']])[0]
        if v not in VARIANTS or theme not in THEMES or not (320 <= w <= 4000 and 320 <= h <= 4000 and 0 <= every <= 86400 and -1 <= bat <= 100):
            return self.send_error(400)
        body = frame(self.server.cfg, w, h, v, theme, every or None, None if bat < 0 else bat)
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Refresh', str(e['refresh_seconds']))  # the reader waits this long before asking again
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # one request every few seconds forever would only fill the journal; errors still print
        pass


def selftest():
    rows = [{'name': 'c%02d' % i, 'bad': False, 'cpu': float(i), 'mem': '1MiB'} for i in range(40)] + [{'name': 'zz', 'bad': True, 'cpu': None, 'mem': ''}]
    got = cells(rows, 10)
    assert len(got) == 10 and got[0][1] == 'zz' and got[1][1] == 'c00' and got[-1][1] == '+32 more', got
    assert cells(rows, 0) == [] and len(cells(rows, 99)) == 41 and cells(rows, 9, by_cpu=True)[1][1] == 'c39'
    assert parse_stats('web\t12.50%\t90MiB / 1GiB\ndb\t0.00%\t1.2GiB / 4GiB\n') == {'web': (12.5, '90MiB'), 'db': (0.0, '1.2GiB')}
    manifest = [{'name': 'web', 'type': 'container'}, {'name': 'db', 'type': 'container'}, {'name': 'ssh', 'type': 'systemd'}]
    ps = check_services.parse_ps('web\trunning\tUp 3 days (healthy)\ndb\trunning\tUp 3 days (unhealthy)\n')
    got = service_rows(['web', 'db', 'ssh', 'gone'], manifest, ps, {'web': (3.0, '10MiB')}, {'ssh': 'active'})
    assert [(r['name'], r['bad'], r['cpu']) for r in got] == [('web', False, 3.0), ('db', True, None), ('ssh', False, None), ('gone', True, None)], got
    e = load_example()
    configure(e)
    loud, calm = sample_data(), dict(sample_data(), alerts=[], gpu=None, drives=[dict(sample_data()['drives'][0], pct=None, free=0, total=0)])
    for v in VARIANTS:  # every variant, both themes, both orientations, loud and calm data, plus a screen too small to fit
        for theme in THEMES:
            for wh in ((1264, 1680), (1680, 1264), (320, 320)):
                assert render(loud, e, *wh, v, theme, 30).size == wh and render(calm, e, *wh, v, theme).size == wh
    said = lambda every: footer(every, 65)[0][1][0][1]
    assert said(30) == 'refreshes every 30 sec' and said(300) == 'refreshes every 5 min'
    assert footer(30, 65, 81)[0][1][0][1] == 'battery 81%   refreshes every 30 sec'
    bare, with_foot = render(calm, e, 1264, 1680, 'config', 'dark'), render(calm, e, 1264, 1680, 'config', 'dark', 30)  # no interval given = no claim in the corner
    assert bare.crop((0, 1590, 1264, 1680)).getcolors() == [(1264 * 90, bare.getpixel((0, 0)))] and with_foot.crop((0, 1590, 1264, 1680)).getcolors(999) != bare.crop((0, 1590, 1264, 1680)).getcolors(999)
    assert clock(datetime(2026, 1, 1, 0, 5)) == '12:05 AM' and clock(datetime(2026, 1, 1, 19, 2)) == '7:02 PM'
    assert [tone(p) for p in (69, 70, 89, 90)] == ['ok', 'warn', 'warn', 'bad']


def load_example():
    with open(os.path.join(ROOT, 'config.example.json')) as f:
        return json.load(f)['eink']


if __name__ == '__main__':
    if sys.argv[1:] == ['selftest']:
        selftest()
        print('selftest ok')
    elif sys.argv[1:2] == ['sample'] and len(sys.argv) == 3:
        e = load_example()
        configure(e)
        render(sample_data(), e, e['width'], e['height'], e['variant'], e['theme'], e['refresh_seconds']).save(sys.argv[2])
    elif len(sys.argv) == 1:
        cfg = load_config()
        configure(cfg['eink'])
        cpu_pct()
        time.sleep(0.3)  # so the first frame has a CPU reading
        srv = ThreadingHTTPServer((cfg['eink']['bind'], cfg['eink']['port']), H)
        srv.cfg = cfg
        print(f"serving /frame.png on {cfg['eink']['bind']}:{cfg['eink']['port']}", flush=True)
        srv.serve_forever()
    else:
        sys.exit(__doc__)
