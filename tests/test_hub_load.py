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
    h._reconnect_with_backoff=lambda: recon.append(1); 
    async def noref(): pass
    h._async_refresh_all_states=noref
    await h._async_keepalive_tick(None); assert recon==[1]; assert h.available is False
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
    print("all hub tests passed")
asyncio.run(main())
