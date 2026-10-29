"""Offline checks for hub.py load/keepalive/cache logic (HA and the box are faked).

Run: python3 tests/test_hub_load.py
"""
import sys, types, asyncio, importlib, time
for n in ("homeassistant","homeassistant.helpers","homeassistant.helpers.event","homeassistant.helpers.storage","homeassistant.core"):
    sys.modules[n]=types.ModuleType(n)
ev=sys.modules["homeassistant.helpers.event"]; ev.async_call_later=lambda *a,**k:(lambda:None); ev.async_track_time_interval=lambda *a,**k:(lambda:None)
class Store:
    saved=None
    def __init__(s,*a): pass
    async def async_load(s): return Store.saved
    async def async_save(s,d): Store.saved=d
sys.modules["homeassistant.helpers.storage"].Store=Store
pkg=types.ModuleType("vitrea"); pkg.__path__=[str(__import__("pathlib").Path(__file__).resolve().parents[1] / "custom_components" / "vitrea")]; sys.modules["vitrea"]=pkg
hub=importlib.import_module("vitrea.hub")

class KS:
    def __init__(s,node,key,on): s.node,s.key,s.is_on,s.ratio=node,key,on,0
class FakeMobile:
    def __init__(s): s.calls=[]; s.ts=b"\x01\x02"; s._sock=object(); s.fail_ping=False
    def get_all_key_status(s,n): s.calls.append(("all",n)); return [KS(n,0,True),KS(n,1,False)]
    def get_last_change_timestamp(s,timeout=None):
        s.calls.append(("ts",)); s.last_ts_timeout=timeout
        if s.fail_ping: raise RuntimeError("timeout")
        return s.ts
class Hass:
    async def async_add_executor_job(s,fn,*a): return fn(*a)
    loop=None
def mk():
    h=hub.VitreaHub(Hass(),{"host":"x"}); h._mobile=FakeMobile(); h.available=True; return h

async def main():
    # shared status cache: 100 reads of one node = 1 request
    h=mk(); h._entities={(5,0):types.SimpleNamespace(on_push_state=lambda *a:None)}
    for _ in range(100): await h.async_get_key_status(5,0)
    assert h._mobile.calls.count(("all",5))==1, h._mobile.calls
    # fresh read bypasses cache
    await h.async_get_key_status(5,0,mobile=True); assert h._mobile.calls.count(("all",5))==2
    # keepalive: skipped when recently active, pings when idle, reconnects on failure
    h=mk(); await h._async_keepalive_tick(None); assert not h._mobile.calls
    h._last_activity-=100; await h._async_keepalive_tick(None); assert h._mobile.calls==[("ts",)]
    assert h._mobile.last_ts_timeout is not None and h._mobile.last_ts_timeout<=5, h._mobile.last_ts_timeout  # short ping timeout
    h._last_activity-=100; h._mobile.fail_ping=True; recon=[]
    h._reconnect_with_backoff=lambda *a: recon.append(1); 
    async def noref(): pass
    h._async_refresh_all_states=noref
    await h._async_keepalive_tick(None); assert recon==[1]; assert h.available is True  # routine reconnect: stays available
    # metadata cache: first run scans+saves, second run with same stamp uses cache, changed stamp rescans
    scans=[]
    def fm(): scans.append(1); return ([{"node":1,"key":0,"mobile_key":0,"name":"A","ha_type":"light","dimmable":False,"room":""}],[{"id":0,"name":"S"}])
    for stamp,expect in ((b"\x01\x02",1),(b"\x01\x02",1),(b"\x09\x09",2)):
        h=mk(); h._mobile.ts=stamp; h._fetch_metadata=fm; h._async_migrate_unique_ids=lambda: None
        await h._async_fetch_and_apply_metadata(); assert len(scans)==expect,(stamp,len(scans)); assert h.metadata_ready and len(h.keys)==1
    # timestamp unreadable + cache present -> use cache, no scan
    h=mk(); h._mobile.fail_ping=True; h._fetch_metadata=fm; h._async_migrate_unique_ids=lambda: None
    await h._async_fetch_and_apply_metadata(); assert len(scans)==2
    # cover verify after a command: a missed push must not leave the position stale,
    # but a still-moving blind (transient is_on) must not be applied
    class KS2(KS):
        def __init__(s,node,key,on,ratio): super().__init__(node,key,on); s.ratio=ratio
    captured=[]; hub.async_call_later=lambda hass,delay,fn:(captured.append(fn),lambda:None)[1]
    for status,expect in (([KS2(5,0,False,40)],[(False,40)]), ([KS2(5,0,True,0)],[])):
        h=mk(); h._mobile.get_all_key_status=lambda n,status=status:status
        seen=[]; h._entities={(5,0):types.SimpleNamespace(_position=100,on_push_state=lambda on,r:seen.append((on,r)))}
        captured.clear(); h._schedule_verify(5,0); assert len(captured)==1
        await captured[0](None); assert seen==expect,(status,seen)
    # reconnects are serialized: two threads that saw the same dead session -> ONE login
    import threading
    h=mk(); logins=[]
    def fake_connect(): logins.append(1); h._epoch+=1
    h._connect_mobile=fake_connect; e=h._epoch
    ts=[threading.Thread(target=h._reconnect_with_backoff,args=(e,)) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]; assert len(logins)==1, logins
    # command path: short timeout first, then ONE reconnect and a patient retry (timeout=None)
    h=mk(); seen=[]; rec=[]
    h._reconnect_with_backoff=lambda *a: rec.append(a)
    def send(t):
        seen.append(t)
        if len(seen)==1: raise RuntimeError("timeout")
        return "ok"
    assert h._call_cmd(send)=="ok" and seen==[hub.COMMAND_TIMEOUT,None] and len(rec)==1, (seen,rec)
    # a reconnect that finds the session already replaced must not leave us unavailable (stuck-unavailable bug)
    h=mk(); h.available=False; h._epoch=5; h._reconnect_with_backoff(4); assert h.available is True
    # a routine keepalive reconnect doesn't flip availability; only a FAILED one does
    for ok in (True, False):
        h=mk(); h._last_activity-=100; h._mobile.fail_ping=True
        def rb(*a, ok=ok):
            if not ok: raise RuntimeError("login timeout")
        h._reconnect_with_backoff=rb; h._async_refresh_all_states=noref
        await h._async_keepalive_tick(None); assert h.available is ok, ok
    # entities not yet added to hass are skipped, and one bad entity can't abort the refresh
    seen=[]; ent=types.SimpleNamespace(hass=None,on_push_state=lambda on,r:seen.append(1))
    hub.VitreaHub._apply_state(ent,KS(1,0,True)); assert seen==[]
    ent.hass=object(); hub.VitreaHub._apply_state(ent,KS(1,0,True)); assert seen==[1]
    hub.VitreaHub._apply_state(types.SimpleNamespace(hass=object(),on_push_state=lambda *a:1/0),KS(1,0,True))
    h=mk(); got=[]
    h._entities={(5,0):types.SimpleNamespace(hass=None,on_push_state=lambda *a:got.append("bad")),
                 (5,1):types.SimpleNamespace(hass=object(),on_push_state=lambda *a:got.append("good"))}
    await h._async_refresh_all_states(); assert got==["good"], got
    # a failed login closes its socket instead of leaking it
    closed=[]
    class BadM:
        def __init__(s,**k): pass
        def add_push_callback(s,cb): pass
        def connect(s): pass
        def login(s): raise RuntimeError("login timeout")
        def close(s): closed.append(1)
    hub.MobileVBox=BadM
    h=mk(); h._data={"host":"x",hub.CONF_MOBILE_PORT:1,hub.CONF_MOBILE_USER:"u",hub.CONF_MOBILE_PASSWORD:"p"}
    try: h._connect_mobile(); raise SystemExit("login should have failed")
    except RuntimeError: pass
    assert closed==[1], closed
    print("all hub tests passed")
asyncio.run(main())
