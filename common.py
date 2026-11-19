"""Shared helpers for the checks: config loading, read-only command runner, result lines.

Exit codes follow the monitoring plugin convention: 0 ok, 1 warning, 2 critical.
"""
import json, os, subprocess, sys

OK, WARN, CRIT = 0, 1, 2
LABEL = {OK: 'ok', WARN: 'WARN', CRIT: 'CRIT'}
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.environ.get('OPS_CONFIG') or os.path.join(HERE, 'config.json')
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f'no config at {path}: copy config.example.json to config.json and edit it')


def sh(cmd, timeout=30):
    """Run a command and return (exit code, stdout). A missing tool is 127 and a timeout is 124, never a crash."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, ''


def worst(results):
    return max((level for level, _ in results), default=OK)


def report(title, results):
    """Print one line per finding. Returns the worst level, which is also the exit code."""
    print(f'== {title}')
    for level, text in results:
        print(f'  {LABEL[level]:<5} {text}')
    return worst(results)


def main(title, run, selftest):
    """Entry point shared by every check: `script.py` runs it, `script.py selftest` checks its logic."""
    if sys.argv[1:] == ['selftest']:
        selftest()
        print('selftest ok')
        return
    sys.exit(report(title, run(load_config())))
