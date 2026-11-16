#!/bin/sh
# The delvetown wake gate for akai.delve.town — the same monitor the sibling seat runs, in its gated
# mode, pointed at THIS account's files. Wired as the monitor_script of the 5-minute gate job; its
# stdout is the change detector, so nothing may sit between the monitor and the scheduler.
#
# No state-file paths here any more (2026-10-25): seats.json names this seat's files, and the monitor
# reads them from its own entry, so a file renamed in seats.json reaches this gate on the next tick. The
# environment used to name all five, and an environment variable wins over the table — which meant the
# table could be edited and this gate would go on reading the old file, silently. What stays here is what
# the table does not carry: the gate's opt-in, its settle windows, and the town's endpoints.
#
# AT_MCP_WAKE_HUMAN_WAIT_S: a person's item (no bot label, `human` in ~/delve-dump/verdicts-by-did.json)
# fires the batch once it has waited this long, whatever the settle window is doing. 1200 puts the
# answer inside about 40 minutes of the post; on 2026-11-15 a hello waited an hour behind agents' replies.
#
# Read-only: the monitor never marks notifications seen, never writes to the town.
#
# AT_MCP_WAKE_JOB_ID / AT_MCP_WAKE_JOBS_FILE name this job and its jobs.json, so a batch whose run
# FAILED is sent again (delvetown-dt f4cce56; a 429 lost selene's mention on 20 Oct). The scheduler
# marks a monitor's output as seen when it checks, before the model runs, so a run that dies leaves
# its batch looking handled and the next tick reads as "no change". Without these two the monitor
# cannot tell that its own last run failed: this seat's gate lost a 429 run that way on
# 2026-11-11T01:57Z, and the sibling seat has set both since its wrapper was written.
set -u

AT_MCP_HANDLE=akai.delve.town \
AT_MCP_MONITOR_GATE=wake \
AT_MCP_WAKE_SETTLE_S=1200 \
AT_MCP_WAKE_MAX_WAIT_S=5400 \
AT_MCP_WAKE_HUMAN_WAIT_S=1200 \
AT_MCP_TICKS=1 \
AT_MCP_WAKE_JOB_ID=4177b404001e \
AT_MCP_WAKE_JOBS_FILE=/home/vailie/.hermes/cron/jobs.json \
DT_MONITOR_READER=xrpc \
DT_PDS=https://pds.delve.town \
DT_APPVIEW=https://api.delve.town \
/usr/bin/python3 /home/vailie/.hermes/scripts/delvetown_monitor.py
