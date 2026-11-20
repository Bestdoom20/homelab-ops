# homelab-ops

Five small checks for a Linux server that runs Docker: are the right services up, is the disk filling, is the backup fresh, is it safe to reboot, and one ten-line status snapshot.

They come from my home server (Ubuntu 24.04, 16 CPU threads, 24 running containers on the day of the run below). Each check exists because something went wrong once. The originals are shell scripts tied to my own paths and service names. These are simplified ports: same checks, generic, with every path and threshold in a config file.

Python 3, standard library only. Every check is read-only. Nothing here restarts, deletes or reboots anything.

## The checks

| Script | Question it answers | Why it exists |
|---|---|---|
| `check_services.py` | Is everything in the expected-services manifest running and healthy? Is anything running that is not on the list? | Before 2026-05-12 the list of expected services lived in two scripts and had drifted apart. The watchdog was raising 444 false "container does not exist" warnings a day. Since that day there is one list. |
| `check_disk.py` | Is any mount past the warning or critical threshold? If so, what are the largest directories? | On 2026-11-03 the root disk reached 99% with 6.9 GB free. |
| `check_backup.py` | Is the newest good backup younger than the limit? | On 2026-07-08 I found the nightly backup had not run for about two months. |
| `prereboot.py` | GO or NO-GO: runs the three checks above, then the boot chain, fstab, SSH and running jobs. | The server once hung on a reboot and I had to plug in a monitor to get it back. |
| `status.py` | Ten lines: host, uptime, load, memory, disk, services, backup, failed units, pending reboot, verdict. | The first thing to look at before touching anything. |

Exit codes follow the monitoring plugin convention: 0 ok, 1 warning, 2 critical. `prereboot.py` exits 0 for GO and 1 for NO-GO, so it can gate a command:

```bash
./prereboot.py && reboot
```

## Setup

```bash
git clone https://github.com/Bestdoom20/homelab-ops.git
cd homelab-ops
cp config.example.json config.json    # then edit it
./status.py
```

`config.json` holds four things:

- `services`: the manifest. Each entry is a name and a type, `container` or `systemd`.
- `disk`: the mounts to watch, `warn_pct`, `crit_pct`, and `top`, the number of largest directories to list.
- `backup`: `max_age_hours`, and one entry per backup target. A target is either a JSON state file plus the key that holds the time of the last success, or a folder whose newest file is the backup.
- `prereboot`: `busy_patterns`, the process names that must not be running when you reboot.

Run the checks as root. `sshd -t` and `docker ps` need it.

## A real run

This is the output from my server on 2026-11-20. Host, service and folder names are changed. The numbers and the findings are not.

```
$ ./status.py
host      homeserver  2026-11-20 01:43 UTC
uptime    47 days 6 h
load      1.67 1.49 1.46 on 16 CPUs
memory    22 of 63 GB used (34%)
disk      CRIT /: 97% used, 16 GB free; /data: 57% used, 90 GB free
services  CRIT chat-bot: systemd unit is inactive; 35 of 36 expected services up
backup    CRIT offsite-usb: newest good backup is 70 h old (limit 48 h); db-dumps: newest good backup is 22 h old
units     WARN failed systemd units: mnt-usb.mount, man-db.service
reboot    required by an installed update
verdict   CRIT: 3 critical, 1 warning(s)
```

It found three real problems that day. That is what it is for.

- The root disk is at 97%. It was cleaned down to 78% on 2026-11-03 and filled again in 17 days.
- The backup job runs every night and writes its database dumps (22 hours old), but the copy to the backup drive last succeeded 70 hours ago.
- One service on the expected list is not running. Either it should be, or the list is out of date. Both are worth knowing.

The pre-reboot check on the same day says NO-GO, which is the right answer with a stale backup and a full disk:

```
$ ./prereboot.py
== services
  CRIT  chat-bot: systemd unit is inactive
  ok    35 of 36 expected services up
== disk
  CRIT  /: 97% used, 16 GB free
        largest: /srv/projects 208 GB, /root 48 GB, /var 44 GB, /tmp 42 GB, /usr 22 GB
  ok    /data: 57% used, 90 GB free
== backup
  CRIT  offsite-usb: newest good backup is 70 h old (limit 48 h)
  ok    db-dumps: newest good backup is 22 h old
== boot
  ok    kernel 6.8.0-111-generic has an initramfs
  ok    kernel 6.8.0-138-generic has an initramfs
  ok    fstab verifies
  ok    sshd config valid
  ok    ssh starts at boot
== jobs and recovery
  ok    no backup, copy or package job running
  ok    24 running containers all restart by themselves
  WARN  failed systemd units: mnt-usb.mount, man-db.service

NO-GO: 3 failing check(s), 1 warning(s)
```

The manifest for this run was built from the server's own lists: the units named in its health check and its pre-reboot script, plus every running container that is set to restart by itself.

What to do about each failing line is in [RUNBOOK.md](RUNBOOK.md). Three write-ups of real incidents are in [INCIDENTS.md](INCIDENTS.md).

## Design notes

- **One list of what should be running.** A service is expected because it is in the manifest, and for no other reason. A container that runs without being listed gets a warning, so the list cannot quietly go stale.
- **Running is not the same as healthy.** Docker restarts a container that exits. It does not restart one that is up and stuck. The service check reads the health status too.
- **Last success, not last run.** A backup job that fails every night still touches its files every night. On the run above the state file was 22 hours old and the last good backup was 70 hours old. The check reads the time of the last success.
- **A missing mount must not look healthy.** If `/data` is not mounted, asking for its disk usage silently returns the numbers of the parent disk. The disk guard checks that the mount is real first.
- **Logic is separate from the commands that feed it.** Each script has small functions that take plain text or numbers and return findings. That is what the selftests exercise, so they run anywhere, with no server and no Docker.

## Selftests

```
$ for s in check_services.py check_disk.py check_backup.py prereboot.py status.py; do python3 $s selftest; done
selftest ok
selftest ok
selftest ok
selftest ok
selftest ok
```

Each selftest feeds fixed input to the logic and asserts the findings: a container that is unhealthy, exited or missing; the threshold boundaries; a state file that is fresh while the backup is stale; a kernel with no initramfs; an fstab line that would block the boot.

One bug was found by the real run and not by the selftests: `findmnt --verify` prints its summary on stderr, so the first version of the fstab check read nothing and reported a failure. The fix is in the history.

## What runs on the real server

These scripts are the checks. They do not alert on their own. On the server, the pieces around them are:

- a daily health check that sends me a Telegram message,
- a container that restarts other containers when they report unhealthy,
- the pre-reboot GO / NO-GO script, run by hand before a planned reboot,
- a service that messages me when the server is back after a reboot,
- a nightly backup timer at 03:30, a daily SSD trim, and a hardware watchdog that resets the machine if it hangs.

## Limits

- No alerting and no scheduling. Put a check in cron and act on its exit code if you want that.
- The service check knows Docker containers and systemd units. Nothing else.
- The backup check proves a backup is recent. It does not prove it can be restored. Test a restore.
- The pre-reboot check is written for Debian and Ubuntu (kernel files in `/boot`, the `reboot-required` flag file).
- The largest-directory scan goes one level deep and gives up after 120 seconds.
