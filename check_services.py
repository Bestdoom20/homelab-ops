#!/usr/bin/env python3
"""Service check: is everything in the expected-services manifest running and healthy?

The manifest (the "services" list in config.json) is the one place that says what should be up.
Anything running that is not in it gets a warning, so the list cannot quietly go stale.
"""
from common import OK, WARN, CRIT, sh, main


def parse_ps(text):
    """`docker ps -a` lines of name<TAB>state<TAB>status into {name: (state, status)}."""
    return {name: (state, status) for name, state, status in (line.split('\t') for line in text.splitlines())}


def evaluate(expected, containers, units):
    """expected: manifest entries. containers: parse_ps output. units: {name: word from `systemctl is-active`}."""
    problems, up = [], 0
    for svc in expected:
        name = svc['name']
        if svc['type'] == 'systemd':
            state = units.get(name, 'unknown')
            bad = None if state == 'active' else f'{name}: systemd unit is {state}'
        elif name not in containers:
            bad = f'{name}: no container with this name'
        else:
            state, status = containers[name]
            if state != 'running':
                bad = f'{name}: container is {state} ({status})'
            else:  # docker restarts a container that exits, not one that is up but stuck: that only shows here
                bad = f'{name}: running but unhealthy' if '(unhealthy)' in status else None
        if bad:
            problems.append((CRIT, bad))
        else:
            up += 1
    listed = {s['name'] for s in expected}
    extra = sorted(n for n, (state, _) in containers.items() if state == 'running' and n not in listed)
    if extra:
        problems.append((WARN, 'running but not in the manifest: ' + ', '.join(extra)))
    return problems + [(OK, f'{up} of {len(expected)} expected services up')]


def run(cfg):
    expected = cfg['services']
    rc, out = sh(['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.State}}\t{{.Status}}'])
    units = {s['name']: sh(['systemctl', 'is-active', s['name']])[1].strip() or 'unknown'
             for s in expected if s['type'] == 'systemd'}
    if rc != 0:  # do not report every container as missing when the real problem is docker itself
        systemd_only = [s for s in expected if s['type'] == 'systemd']
        return [(CRIT, 'cannot read `docker ps` (docker down, or no permission)')] + evaluate(systemd_only, {}, units)
    return evaluate(expected, parse_ps(out), units)


def selftest():
    expected = [{'name': n, 'type': 'container'} for n in ('web', 'db', 'cache', 'worker')] + [{'name': 'ssh', 'type': 'systemd'}]
    ps = parse_ps('web\trunning\tUp 3 days (healthy)\n'
                  'db\trunning\tUp 3 days (unhealthy)\n'
                  'cache\texited\tExited (137) 2 hours ago\n'
                  'stray\trunning\tUp 1 hour\n'
                  'old\texited\tExited (0) 5 weeks ago\n')
    got = evaluate(expected, ps, {'ssh': 'active'})
    assert got == [(CRIT, 'db: running but unhealthy'),
                   (CRIT, 'cache: container is exited (Exited (137) 2 hours ago)'),
                   (CRIT, 'worker: no container with this name'),
                   (WARN, 'running but not in the manifest: stray'),
                   (OK, '2 of 5 expected services up')], got
    assert evaluate(expected[4:], {}, {'ssh': 'failed'})[0] == (CRIT, 'ssh: systemd unit is failed')
    assert evaluate(expected[:1], ps, {}) == [(WARN, 'running but not in the manifest: db, stray'), (OK, '1 of 1 expected services up')]


if __name__ == '__main__':
    main('services', run, selftest)
