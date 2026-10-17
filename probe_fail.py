import importlib.util, os, json
spec = importlib.util.spec_from_file_location("dm", "/home/vailie/.hermes/cache/scratch/dtwt2/delvetown_monitor.py")
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)
print("config: GATE=%r WAKE_LIMIT=%r" % (dm.GATE, getattr(dm, "WAKE_LIMIT", None)))
print("  BIN=%s exists=%s" % (dm.BIN, os.path.exists(dm.BIN)))
print("  ENV_FILE=%s app_password_len=%d" % (dm.ENV_FILE, len(dm.env_from_file(dm.ENV_FILE).get("AT_MCP_APP_PASSWORD",""))))
print("  MONITOR_STATE=%s exists=%s" % (dm.MONITOR_STATE, os.path.exists(dm.MONITOR_STATE)))
print("  WAKE_STATE=%s" % dm.WAKE_STATE)
u, notes, fail = dm.read_town(dm.WAKE_LIMIT)
print("direct read_town(%r): unread=%r fail=%r notes_len=%d items=%d" % (
    getattr(dm,'WAKE_LIMIT',None), u, fail, len(notes or ""), len(dm._items(notes)) if notes else 0))
os.environ["AT_MCP_MONITOR_GATE"] = "wake"
print("--- now main() in the same process, gate=wake ---")
dm.main()
u2, notes2, fail2 = dm.read_town(dm.WAKE_LIMIT)
print("read_town again: unread=%r fail=%r notes_len=%d" % (u2, fail2, len(notes2 or "")))
