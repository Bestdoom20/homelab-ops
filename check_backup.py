#!/usr/bin/env python3
"""Backup freshness: is the newest GOOD backup of each target younger than the limit?

It reads the time of the last success, not the time the backup job last ran.
A job that runs every night and fails every night still touches its files.
"""
import json, os, tempfile, time
from datetime import datetime
from common import OK, CRIT, main


def last_success(target):
    """Unix time of the newest good backup for one target, or None when there has never been one.

    state_file + key: the time the backup job wrote down for its last success (ISO text or Unix seconds).
    newest_in:        the newest file in a folder of dumps or archives.
    """
    if 'newest_in' in target:
        return max((e.stat().st_mtime for e in os.scandir(target['newest_in']) if e.is_file()), default=None)
    with open(target['state_file']) as f:
        value = json.load(f)
    for part in target['key'].split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None or isinstance(value, (int, float)):
        return value
    return datetime.fromisoformat(value).timestamp()  # a time with no zone is read as this machine's local time


def evaluate(name, ts, now, max_hours):
    if ts is None:
        return CRIT, f'{name}: no good backup on record'
    age = (now - ts) / 3600
    if age > max_hours:
        return CRIT, f'{name}: newest good backup is {age:.0f} h old (limit {max_hours} h)'
    return OK, f'{name}: newest good backup is {age:.0f} h old'


def run(cfg):
    b, results = cfg['backup'], []
    for t in b['targets']:
        try:
            results.append(evaluate(t['name'], last_success(t), time.time(), b['max_age_hours']))
        except (OSError, ValueError) as e:  # unreadable state is a failed check, not a crash
            results.append((CRIT, f"{t['name']}: cannot read backup state ({e})"))
    return results


def selftest():
    now = time.time()
    iso = lambda hours: datetime.fromtimestamp(now - hours * 3600).isoformat(timespec='seconds')
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, 'state.json')
        with open(state, 'w') as f:  # written just now, like a job that ran tonight and failed
            json.dump({'usb': {'last_success': iso(10)}, 'offsite': {'last_success': iso(66), 'last_error': 'permission denied'},
                       'cloud': {}, 'epoch': {'last_success': now - 3600}}, f)
        age = lambda key: evaluate(key, last_success({'state_file': state, 'key': key + '.last_success'}), now, 48)
        assert age('usb') == (OK, 'usb: newest good backup is 10 h old')
        assert age('offsite') == (CRIT, 'offsite: newest good backup is 66 h old (limit 48 h)'), 'fresh file, stale backup'
        assert age('cloud') == (CRIT, 'cloud: no good backup on record')
        assert age('epoch')[0] == OK
        dumps = os.path.join(d, 'dumps')
        os.mkdir(dumps)
        assert last_success({'newest_in': dumps}) is None
        for name, hours in (('mon.sql.gz', 80), ('tue.sql.gz', 30)):
            open(os.path.join(dumps, name), 'w').close()
            os.utime(os.path.join(dumps, name), (now - hours * 3600,) * 2)
        assert evaluate('dumps', last_success({'newest_in': dumps}), now, 48) == (OK, 'dumps: newest good backup is 30 h old')
        missing = run({'backup': {'max_age_hours': 48, 'targets': [{'name': 'gone', 'state_file': os.path.join(d, 'nope.json'), 'key': 'a'}]}})
        assert missing[0][0] == CRIT and 'cannot read backup state' in missing[0][1]


if __name__ == '__main__':
    main('backup', run, selftest)
