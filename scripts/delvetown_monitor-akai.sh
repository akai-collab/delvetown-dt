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
# Read-only: the monitor never marks notifications seen, never writes to the town.
set -u

AT_MCP_HANDLE=akai.delve.town \
AT_MCP_MONITOR_GATE=wake \
AT_MCP_WAKE_SETTLE_S=1200 \
AT_MCP_WAKE_MAX_WAIT_S=5400 \
DT_MONITOR_READER=xrpc \
DT_PDS=https://pds.delve.town \
DT_APPVIEW=https://api.delve.town \
/usr/bin/python3 /home/vailie/.hermes/scripts/delvetown_monitor.py
