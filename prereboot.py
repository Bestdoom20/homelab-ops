#!/usr/bin/env python3
"""Pre-reboot GO / NO-GO: runs the other checks, then the things that decide whether the machine comes back with SSH.

It changes nothing and it never reboots anything. Run it as root, and use it as:  ./prereboot.py && reboot
"""
import os, re, sys
import check_backup, check_disk, check_services
from common import OK, WARN, CRIT, failed_units, load_config, report, sh


def kernels(files):
    """Every installed kernel needs its initramfs or it cannot boot. A single kernel means no fallback in the boot menu."""
    names = sorted(f[len('vmlinuz-'):] for f in files if f.startswith('vmlinuz-'))
    results = [(OK, f'kernel {k} has an initramfs') if f'initrd.img-{k}' in files else (CRIT, f'kernel {k} has NO initramfs')
               for k in names]
    if len(names) < 2:
        results.append((WARN, f'{len(names)} kernel(s) installed: no older one to fall back to'))
    return results


def fstab_errors(verify_output):
    """Error count from the summary line of `findmnt --verify`, or None when that line is missing."""
    m = re.search(r'(\d+) parse errors?, (\d+) errors?', verify_output)
    return int(m[1]) + int(m[2]) if m else None


def risky_mounts(fstab):
    """Mount points that drop the boot to an emergency shell when their device is missing (no nofail, no noauto)."""
    out = []
    for line in fstab.splitlines():
        f = line.split()
        if len(f) >= 4 and not f[0].startswith('#') and f[1] not in ('/', 'none', 'swap') and not f[1].startswith('/boot'):
            if not {'nofail', 'noauto'} & set(f[3].split(',')):
                out.append(f[1])
    return out


def no_restart(lines):
    """Running containers that stay down after a reboot. Lines are '/name<TAB>policy' from `docker inspect`."""
    pairs = (line.partition('\t') for line in lines)
    return sorted(name.lstrip('/') for name, _, policy in pairs if policy not in ('always', 'unless-stopped'))


def verdict(results):
    fails = sum(level == CRIT for level, _ in results)
    warns = sum(level == WARN for level, _ in results)
    if fails:
        return f'NO-GO: {fails} failing check(s), {warns} warning(s)', 1
    return f'GO: all hard checks pass ({warns} warning(s))', 0


def boot():
    results = kernels(os.listdir('/boot'))
    errors = fstab_errors(sh(['findmnt', '--verify'])[1])
    results.append((OK, 'fstab verifies') if errors == 0 else (CRIT, f'fstab does not verify (errors: {errors})'))
    with open('/etc/fstab') as f:
        results += [(CRIT, f'fstab mount {m} has neither nofail nor noauto: a missing device blocks the boot')
                    for m in risky_mounts(f.read())]
    results.append((OK, 'sshd config valid') if sh(['sshd', '-t'])[0] == 0 else (CRIT, 'sshd config invalid (run: sshd -t)'))
    enabled = any(sh(['systemctl', 'is-enabled', unit])[1].strip() == 'enabled' for unit in ('ssh', 'sshd'))
    results.append((OK, 'ssh starts at boot') if enabled else (CRIT, 'ssh is not enabled at boot'))
    return results


def recovery(cfg):
    results = []
    busy = sh(['pgrep', '-a', cfg['prereboot']['busy_patterns']])[1].splitlines()  # matched on the process name
    results.append((CRIT, 'jobs running that a reboot would cut off: ' + '; '.join(line[:60] for line in busy[:3])) if busy
                   else (OK, 'no backup, copy or package job running'))
    ids = sh(['docker', 'ps', '-q'])[1].split()
    if ids:
        out = sh(['docker', 'inspect', '-f', '{{.Name}}\t{{.HostConfig.RestartPolicy.Name}}', *ids])[1]
        down = no_restart(out.splitlines())
        results.append((WARN, 'containers that will NOT come back by themselves: ' + ', '.join(down)) if down
                       else (OK, f'{len(ids)} running containers all restart by themselves'))
    failed = failed_units()
    results.append((WARN, 'failed systemd units: ' + ', '.join(failed)) if failed else (OK, 'no failed systemd units'))
    return results


def selftest():
    boot_dir = ['vmlinuz-6.8.0-1', 'initrd.img-6.8.0-1', 'vmlinuz-6.8.0-2', 'config-6.8.0-2', 'grub']
    assert kernels(boot_dir) == [(OK, 'kernel 6.8.0-1 has an initramfs'), (CRIT, 'kernel 6.8.0-2 has NO initramfs')]
    assert kernels(boot_dir[:2])[-1] == (WARN, '1 kernel(s) installed: no older one to fall back to')
    assert fstab_errors('\n0 parse errors, 0 errors, 1 warning\n') == 0
    assert fstab_errors('1 parse error, 10 errors, 0 warnings') == 11, '"10 errors" must not read as "0 errors"'
    assert fstab_errors('findmnt: command not found') is None
    fstab = ('# <file system> <mount point> <type> <options>\n'
             'UUID=aa / ext4 defaults 0 1\n'
             'UUID=bb /boot/efi vfat defaults 0 1\n'
             '/swap.img none swap sw 0 0\n'
             'UUID=cc /data ext4 defaults,noatime,nofail 0 2\n'
             'UUID=dd /mnt/usb ext4 defaults 0 2\n'
             'host:/share /mnt/share fuse.sshfs noauto,_netdev 0 0\n')
    assert risky_mounts(fstab) == ['/mnt/usb']
    assert no_restart(['/web\tunless-stopped', '/db\talways', '/scratch\tno', '/job\ton-failure']) == ['job', 'scratch']
    assert verdict([(OK, 'a'), (WARN, 'b')]) == ('GO: all hard checks pass (1 warning(s))', 0)
    assert verdict([(OK, 'a'), (WARN, 'b'), (CRIT, 'c')]) == ('NO-GO: 1 failing check(s), 1 warning(s)', 1)


def main():
    if sys.argv[1:] == ['selftest']:
        selftest()
        return print('selftest ok')
    cfg, everything = load_config(), []
    for title, results in (('services', check_services.run(cfg)), ('disk', check_disk.run(cfg)),
                           ('backup', check_backup.run(cfg)), ('boot', boot()), ('jobs and recovery', recovery(cfg))):
        report(title, results)
        everything += results
    text, code = verdict(everything)
    print('\n' + text)
    sys.exit(code)


if __name__ == '__main__':
    main()
