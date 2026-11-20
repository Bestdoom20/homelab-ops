#!/usr/bin/env python3
"""Disk guard: warning and critical thresholds per mount, plus the largest directories once a mount is filling up."""
import math, os, shutil
from common import OK, WARN, CRIT, sh, main

GB = 1024 ** 3


def used_pct(used, free):
    """Percent used the way `df` shows it: out of the space a normal user can use, rounded up."""
    return math.ceil(100 * used / (used + free))


def level(pct, warn, crit):
    return CRIT if pct >= crit else WARN if pct >= warn else OK


def parse_du(text, top):
    """`du -d 1 --block-size=1M` output into the `top` largest (MB, path) pairs. The last line is the total, so it is dropped."""
    rows = [(int(mb), path) for mb, path in (line.split('\t', 1) for line in text.splitlines())]
    return sorted(rows[:-1], reverse=True)[:top]


def largest(mount, top):
    # one level deep and on this filesystem only (-x), so other mounts are not counted twice
    rc, out = sh(['du', '-x', '-d', '1', '--block-size=1M', mount], timeout=120)
    if rc == 124:
        return 'largest directories: scan gave up after 120 s'
    if not out:  # du exits 1 when it could not read some folder, but still prints the rest
        return 'largest directories: du returned nothing'
    return 'largest: ' + ', '.join(f'{path} {mb // 1024} GB' for mb, path in parse_du(out, top))


def run(cfg):
    d, results = cfg['disk'], []
    for mount in d['mounts']:
        if not os.path.ismount(mount):  # without this, a missing mount silently reports the parent disk's numbers
            results.append((CRIT, f'{mount}: not mounted'))
            continue
        u = shutil.disk_usage(mount)
        pct = used_pct(u.used, u.free)
        lv = level(pct, d['warn_pct'], d['crit_pct'])
        text = f'{mount}: {pct}% used, {u.free // GB} GB free'
        if lv != OK and d['top']:  # top 0 skips the scan, which is the slow part
            text += '\n        ' + largest(mount, d['top'])
        results.append((lv, text))
    return results


def selftest():
    assert used_pct(399, 17) == 96 and used_pct(118, 91) == 57, 'matches what df printed for the same numbers'
    assert [level(p, 85, 95) for p in (84, 85, 94, 95, 100)] == [OK, WARN, WARN, CRIT, CRIT]
    du = '120\t/srv/a\n90000\t/srv/media\n4096\t/srv/db\n7\t/srv/tmp\n94223\t/srv\n'
    assert parse_du(du, 2) == [(90000, '/srv/media'), (4096, '/srv/db')], 'total line dropped, biggest first'
    assert run({'disk': {'mounts': ['/definitely/not/a/mount'], 'warn_pct': 85, 'crit_pct': 95, 'top': 5}}) == \
        [(CRIT, '/definitely/not/a/mount: not mounted')]


if __name__ == '__main__':
    main('disk', run, selftest)
