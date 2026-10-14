# Server storage and runner recovery — 2026-10-14

At inspection, the 25GB root disk had only ~431MiB available. All six mainnet
paper scans had correctly paused below their 512MiB reserve. The main paper bot
and monitors were still running. The largest log was `/var/log/xrdp.log` (~4.9GB),
which had no logrotate rule; `/var/log/syslog.1` was another ~1.08GB.

Before reclaiming these logs, stream finite snapshots to the user's local archive,
throttle compression, and verify decompressed size and SHA-256 against the source.
Keep a manifest with source inode, captured byte length, timestamp and both hashes.
Do not delete or truncate databases, trading history, frozen research or backups.

Install `scripts/bot-xrdp.logrotate` as `/etc/logrotate.d/bot-xrdp`. It retains
14 compressed rotations, checks daily or at 50MB, and uses copytruncate to retain
the live log inode. Future copytruncate rotations have the normal small copy/truncate
race; these are server-access diagnostics, not the bot's trading database.
Change the existing rsyslog rule from weekly/4/delaycompress to daily/14/50MB and
immediate compression, preserving its file list, permissions and reopen hook.
Both standalone rules explicitly retain the distribution's `su root adm` setting;
rsyslog also retains `create` with each source file's existing owner/mode.
The extra hourly timer invokes only these two rules with the standard shared
logrotate state/lock. Exit status 3 means another rotation owns the lock.

The journal drop-in caps persistent logs at 3GiB and reserves 2GiB disk headroom.
Apply it only after space is reclaimed so the reserve does not immediately vacuum
the existing journals. The current ~2.3GiB journal fits under the new cap. Future
rotation retention is bounded; historical snapshots reclaimed in this maintenance
remain in the verified local archives.

The runner timer originally phase-locked with promotion: both ran every 300s and
the runner's ExecCondition always saw promotion activating. A 60s inactive-based
retry plus a saved-status 300s minimum worker interval breaks this lock without
increasing expensive worker frequency. RAM/swap guards and all worker resource
limits are retained. A timer retry may legitimately defer while promotion runs.

The original runner DB contained no sources and no candles. Preserve that DB and
its protocol under `/root/bot_runner_comparison/outage_archive_20261014` before
registering a new forward-only cohort. `recovery_20261014.json` records both
timestamps/watermarks and the outage. Do not backfill the gap into prospective
performance. The 14-day review clock starts at the new registration and still
requires 50 completed pairs. Trading rules, risk and six existing cohorts are frozen.

Rollback: restore the saved resource guard and timer and stop only the new log
rotation timer; restore the saved rsyslog rule and remove the new journal drop-in
if needed. Preserve both runner databases and all local log archives. Reclaimed
log contents can be restored from those archives. Trading services need no restart.
