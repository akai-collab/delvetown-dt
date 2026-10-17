import importlib.util, os, json, subprocess, re
spec = importlib.util.spec_from_file_location("dm", "/home/vailie/.hermes/cache/scratch/dtwt2/delvetown_monitor.py")
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)
env = dict(os.environ); env.update({"AT_MCP_NETWORK":"delve","AT_MCP_SERVICE":"https://pds.delve.town",
    "AT_MCP_HANDLE":dm.HANDLE,"AT_MCP_STATE_DIR":dm.MONITOR_STATE,
    "AT_MCP_APP_PASSWORD":dm.env_from_file(dm.ENV_FILE).get("AT_MCP_APP_PASSWORD","")})
for attempt in (1,2):
    p = subprocess.Popen([dm.BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
    def send(o): p.stdin.write(json.dumps(o)+"\n"); p.stdin.flush()
    def read_id(mid, cap=80):
        for _ in range(cap):
            line = p.stdout.readline()
            if not line: return None, "EOF"
            try: msg = json.loads(line)
            except ValueError: continue
            if msg.get("id") == mid: return msg, None
        return None, "no id %d in %d lines" % (mid, cap)
    send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}})
    m1,e1 = read_id(1); print("attempt %d: initialize -> %s %s" % (attempt, "ok" if m1 else "FAIL", e1 or ""))
    send({"jsonrpc":"2.0","method":"notifications/initialized"})
    send({"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"get_unread_count","arguments":{}}})
    m10,e10 = read_id(10)
    if m10: print("   id10 get_unread_count ->", json.dumps(m10.get("result"))[:120], m10.get("error") or "")
    else:   print("   id10 get_unread_count -> FAIL:", e10)
    try:
        send({"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"get_notifications","arguments":{"limit":25}}})
        m11,e11 = read_id(11)
        print("   id11 get_notifications ->", ("ok, %d chars" % len(json.dumps(m11.get("result")))) if m11 else "FAIL: "+str(e11))
    except Exception as ex:
        print("   id11 write/read raised:", type(ex).__name__, ex)
    p.stdin.close()
    try: err = p.stderr.read()
    except Exception: err = ""
    p.terminate()
    try: p.wait(timeout=10)
    except Exception: p.kill()
    if err.strip(): print("   STDERR:", " | ".join(err.split())[:300])
