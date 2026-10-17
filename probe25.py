import sys, json, importlib.util
spec = importlib.util.spec_from_file_location("dm", "/home/vailie/.hermes/cache/scratch/dtwt2/delvetown_monitor.py")
dm = importlib.util.module_from_spec(spec); spec.loader.exec_module(dm)
for lim in (10, 25):
    got = dm.read_town(lim)
    print("=== read_town(%d) -> %r" % (lim, type(got).__name__))
    if isinstance(got, tuple):
        print("   tuple:", [ (type(x).__name__, (len(x) if hasattr(x,'__len__') else x)) for x in got ])
        notes = got[1] if len(got)>1 else None
    else:
        notes = got
    if isinstance(notes, str):
        print("   raw head:", " ".join(notes.split())[:300])
        items = dm._items(notes)
        print("   items parsed:", len(items))
        if items: print("   first item:", json.dumps({k:v for k,v in items[0].items() if k in ('uri','reason','author','rkey')})[:200])
    elif notes is None:
        print("   notes is None (no answer)")
