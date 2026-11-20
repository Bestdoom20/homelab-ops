#!/usr/bin/env python3
"""Status snapshot: ten lines that answer "is the server fine right now?". Exit code 0 ok, 1 warning, 2 critical."""
import os, socket, sys, time
import check_backup, check_disk, check_services
from common import OK, WARN, CRIT, LABEL, failed_units, load_config, worst


def fold(results):
    """One check's findings as one phrase: problems first, each tagged with its level."""
    return '; '.join([f'{LABEL[lv]} {text}' for lv, text in results if lv != OK] + [text for lv, text in results if lv == OK])


def render(f):
    """The ten lines, and the worst level found. f holds plain facts so this can be tested without a server."""
    units = [(WARN, 'failed systemd units: ' + ', '.join(f['failed']))] if f['failed'] else [(OK, 'no failed systemd units')]
    results = f['disk'] + f['services'] + f['backup'] + units
    used = f['mem_total_kb'] - f['mem_avail_kb']
    crit, warn = (sum(lv == level for lv, _ in results) for level in (CRIT, WARN))
    rows = [('host', f"{f['host']}  {f['time']}"),
            ('uptime', f"{f['uptime_s'] // 86400} days {f['uptime_s'] % 86400 // 3600} h"),
            ('load', f"{f['load']} on {f['cores']} cores"),
            ('memory', f"{used / 1048576:.0f} of {f['mem_total_kb'] / 1048576:.0f} GB used ({100 * used // f['mem_total_kb']}%)"),
            ('disk', fold(f['disk'])),
            ('services', fold(f['services'])),
            ('backup', fold(f['backup'])),
            ('units', fold(units)),
            ('reboot', 'required by an installed update' if f['reboot_required'] else 'not required'),
            ('verdict', f'{LABEL[worst(results)].upper()}: {crit} critical, {warn} warning(s)')]
    return [f'{name:<10}{text}' for name, text in rows], worst(results)


def collect(cfg):
    with open('/proc/meminfo') as m:
        mem = {line.split(':')[0]: int(line.split()[1]) for line in m}
    with open('/proc/uptime') as u:
        uptime = int(float(u.read().split()[0]))
    return {'host': socket.gethostname(), 'time': time.strftime('%Y-%m-%d %H:%M %Z'), 'uptime_s': uptime,
            'load': ' '.join(f'{x:.2f}' for x in os.getloadavg()), 'cores': os.cpu_count(),
            'mem_total_kb': mem['MemTotal'], 'mem_avail_kb': mem['MemAvailable'],
            'disk': check_disk.run({'disk': {**cfg['disk'], 'top': 0}}),
            'services': check_services.run(cfg), 'backup': check_backup.run(cfg),
            'failed': failed_units(), 'reboot_required': os.path.exists('/var/run/reboot-required')}


def selftest():
    facts = {'host': 'box', 'time': '2026-01-01 09:00 UTC', 'uptime_s': 47 * 86400 + 3 * 3600 + 59, 'load': '0.10 0.20 0.30',
             'cores': 4, 'mem_total_kb': 8 * 1048576, 'mem_avail_kb': 6 * 1048576,
             'disk': [(OK, '/: 40% used, 100 GB free')], 'services': [(OK, '3 of 3 expected services up')],
             'backup': [(OK, 'nightly: newest good backup is 9 h old')], 'failed': [], 'reboot_required': False}
    lines, level = render(facts)
    assert len(lines) == 10 and level == OK, lines
    assert lines[1] == 'uptime    47 days 3 h' and lines[3] == 'memory    2 of 8 GB used (25%)'
    assert lines[9] == 'verdict   OK: 0 critical, 0 warning(s)'
    bad = dict(facts, disk=[(CRIT, '/: 96% used, 17 GB free'), (OK, '/data: 57% used, 91 GB free')],
               failed=['x.service'], reboot_required=True)
    lines, level = render(bad)
    assert len(lines) == 10 and level == CRIT
    assert lines[4] == 'disk      CRIT /: 96% used, 17 GB free; /data: 57% used, 91 GB free', 'problems come first'
    assert lines[7] == 'units     WARN failed systemd units: x.service'
    assert lines[9] == 'verdict   CRIT: 1 critical, 1 warning(s)'


if __name__ == '__main__':
    if sys.argv[1:] == ['selftest']:
        selftest()
        print('selftest ok')
    else:
        lines, level = render(collect(load_config()))
        print('\n'.join(lines))
        sys.exit(level)
