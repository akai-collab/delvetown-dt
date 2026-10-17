import importlib.util, os, json, subprocess, re
spec = importlib.util.spec_from_file_location("dm", "/home/vailie/.hermes/cache/scratch/dtwt2/delvetown_monitor.py")
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)

for attempt in (1, 2, 3):
    env = dict(os.environ)
    env.update({"AT_MCP_NETWORK": "delve", "AT_MCP_SERVICE": "https://pds.delve.town",
                "AT_MCP_HANDLE": dm.HANDLE, "AT_MCP_STATE_DIR": dm.MONITOR_STATE,
                "AT_MCP_APP_PASSWORD": dm.env_from_file(dm.ENV_FILE).get("AT_MCP_APP_PASSWORD", "")})
    p = subprocess.Popen([dm.BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
    def send(o): p.stdin.write(json.dumps(o) + "\n"); p.stdin.flush()
    send({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}})
    send({"jsonrpc":"2.0","method":"notifications/initialized"})
    send({"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"get_notifications","arguments":{"limit":25}}})
    lines = []
    for _ in range(80):
        line = p.stdout.readline()
        if not line: break
        lines.append(line)
        if '"id":11' in line or '"id": 11' in line: break
    p.stdin.close()
    try:
        err = p.stderr.read()
    except Exception as e:
        err = "stderr read failed: %r" % e
    p.terminate()
    try: rc = p.wait(timeout=10)
    except Exception: p.kill(); rc = "killed"
    hit = next((l for l in lines if re.search(r'"id"\s*:\s*11', l)), None)
    ok = hit is not None
    print("=== attempt %d: answered=%s lines=%d exit=%s" % (attempt, ok, len(lines), rc))
    if ok:
        print("   body head:", hit.strip()[:200])
    else:
        print("   stdout tail:", (lines[-1].strip()[:200] if lines else "(empty)"))
    if err and err.strip():
        print("   STDERR:", " ".join(err.split())[:400])
