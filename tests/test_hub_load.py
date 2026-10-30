"""Offline checks for hub.py load/keepalive/cache logic (HA and the box are faked).

Run: python3 tests/test_hub_load.py
"""
import sys, types, asyncio, importlib, time
for n in ("homeassistant","homeassistant.helpers","homeassistant.helpers.event","homeassistant.helpers.storage","homeassistant.core","homeassistant.exceptions"):
    sys.modules[n]=types.ModuleType(n)
class HomeAssistantError(Exception): pass
sys.modules["homeassistant.exceptions"].HomeAssistantError=HomeAssistantError
ev=sys.modules["homeassistant.helpers.event"]; ev.async_call_later=lambda *a,**k:(lambda:None); ev.async_track_time_interval=lambda *a,**k:(lambda:None)
class Store:
    saved=None
    def __init__(s,*a): pass
    async def async_load(s): return Store.saved
    async def async_save(s,d): Store.saved=d
sys.modules["homeassistant.helpers.storage"].Store=Store
pkg=types.ModuleType("vitrea"); pkg.__path__=[str(__import__("pathlib").Path(__file__).resolve().parents[1] / "custom_components" / "vitrea")]; sys.modules["vitrea"]=pkg
hub=importlib.import_module("vitrea.hub")
hub.LOGIN_MIN_GAP=0.0  # real value is exercised in its own test below
from vitrea._mobile_client import MobileVBoxConnectionError, MobileVBoxError

class KS:
    def __init__(s,node,key,on): s.node,s.key,s.is_on,s.ratio=node,key,on,0
class FakeMobile:
    def __init__(s): s.calls=[]; s.ts=b"\x01\x02"; s._sock=object(); s.fail_ping=False
    def get_all_key_status(s,n): s.calls.append(("all",n)); return [KS(n,0,True),KS(n,1,False)]
    def handshake(s,timeout=None): s.calls.append(("hs",))
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
    # command path: a session that doesn't answer gets ONE reconnect (with the command's deadline) and ONE retry
    import threading, time
    h=mk(); seen=[]; rec=[]
    h._reconnect_with_backoff=lambda *a: rec.append(a)
    def send(t):
        seen.append(t)
        if len(seen)==1: raise MobileVBoxConnectionError("timeout")
        return "ok"
    h._cmd_seq[(1,0)]=1
    assert h._run_command((1,0),1,time.monotonic(),send)=="ok" and seen[0]==hub.COMMAND_TIMEOUT and len(seen)==2 and len(rec)==1, (seen,rec)
    assert rec[0][1] is not None  # deadline passed so the reconnect can't outlive the command
    # a NACK means the box answered: no reconnect, the error reaches the caller
    h=mk(); rec=[]; h._reconnect_with_backoff=lambda *a: rec.append(a); h._cmd_seq[(1,0)]=1
    def nack(t): raise MobileVBoxError("set_key_status failed: NACK")
    try: h._run_command((1,0),1,time.monotonic(),nack); raise SystemExit("NACK should raise")
    except MobileVBoxError: pass
    assert rec==[]
    # latest wins: a command superseded while the first send was failing is NOT replayed after the reconnect
    h=mk(); rec=[]; h._reconnect_with_backoff=lambda *a: rec.append(a); h._cmd_seq[(1,0)]=1; sent=[]
    def send_then_newer(t):
        sent.append(t); h._cmd_seq[(1,0)]=2; raise MobileVBoxConnectionError("timeout")
    try: h._run_command((1,0),1,time.monotonic(),send_then_newer); raise SystemExit("should be superseded")
    except hub._Superseded: pass
    assert len(sent)==1 and rec==[], (sent,rec)
    # a command older than COMMAND_MAX_AGE is dropped without being sent
    h=mk(); h._cmd_seq[(1,0)]=1; sent=[]
    try: h._run_command((1,0),1,time.monotonic()-hub.COMMAND_MAX_AGE-1,lambda t: sent.append(t)); raise SystemExit("should expire")
    except TimeoutError: pass
    assert sent==[]
    # ...and one that expires while the first send was timing out is not replayed
    h=mk(); h._cmd_seq[(1,0)]=1; rec=[]; h._reconnect_with_backoff=lambda *a: rec.append(a)
    def slow_fail(t):
        time.sleep(0.3); raise MobileVBoxConnectionError("timeout")
    try: h._run_command((1,0),1,time.monotonic()-hub.COMMAND_MAX_AGE+0.1,slow_fail); raise SystemExit("should expire")
    except TimeoutError: pass
    assert rec==[]
    # a command never waits behind a reconnect that is already running: it gives up at its own deadline
    h=mk(); held=threading.Event(); release=threading.Event()
    def hold():
        with h._connect_lock: held.set(); release.wait(5)
    t=threading.Thread(target=hold); t.start(); held.wait()
    t0=time.monotonic()
    try: h._reconnect_with_backoff(0, time.monotonic()+0.3); raise SystemExit("should time out")
    except TimeoutError: pass
    assert time.monotonic()-t0<2, time.monotonic()-t0
    release.set(); t.join()
    # login rate limit: a recent login + a short deadline -> give up instead of sleeping the gap
    h=mk(); h._epoch=3; hub.LOGIN_MIN_GAP=5.0; h._last_login=time.monotonic()
    t0=time.monotonic()
    try: h._reconnect_with_backoff(3, time.monotonic()+1.0); raise SystemExit("should refuse")
    except TimeoutError: pass
    finally: hub.LOGIN_MIN_GAP=0.0
    assert time.monotonic()-t0<1.0
    # young deaths: a session that dies young bumps the counter, a total failure bumps it again,
    # the backoff grows with it, and a long-lived session clears it
    h=mk(); h._connect_mobile=lambda: None; h._session_started=time.monotonic(); h._reconnect_with_backoff(None)
    assert h._young_deaths==1
    h=mk(); h._session_started=time.monotonic()-1000; h._young_deaths=0
    def boom(): raise RuntimeError("login timeout")
    h._connect_mobile=boom; hub.RECONNECT_DELAYS=(0.0,0.0)
    try: h._reconnect_with_backoff(None); raise SystemExit("should fail")
    except RuntimeError: pass
    finally: hub.RECONNECT_DELAYS=(0.3,1.0,2.0,4.0)
    assert h._young_deaths==1, h._young_deaths
    h=mk(); h._young_deaths=4; slept=[]
    real_sleep=time.sleep; hub.time=types.SimpleNamespace(monotonic=time.monotonic, sleep=lambda x: slept.append(x))
    try:
        # BACKOFF_STEPS[4]==30s: poll loop would spin, so drive it with a moving clock instead
        clock=[0.0]; hub.time=types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda x: clock.__setitem__(0, clock[0]+x))
        h._backoff_wait(None); assert clock[0]>=hub.BACKOFF_STEPS[4]-0.01, clock
        clock[0]=0.0; h._epoch=1; calls=[0]
        def mono():
            calls[0]+=1
            if calls[0]==4: h._epoch=2   # a command replaces the session meanwhile
            return clock[0]
        hub.time=types.SimpleNamespace(monotonic=mono, sleep=lambda x: clock.__setitem__(0, clock[0]+x))
        h._backoff_wait(1); assert clock[0]<5, clock
    finally: hub.time=time
    h=mk(); h._young_deaths=3; h._session_started=time.monotonic()-hub.LONG_SESSION-1; h._last_activity=time.monotonic()
    await h._async_keepalive_tick(None); assert h._young_deaths==0
    # background reconnect is single flight
    h=mk(); h._bg_reconnecting=True; rec=[]; h._reconnect_with_backoff=lambda *a: rec.append(a)
    assert await h._async_background_reconnect(h._epoch) is False and rec==[]
    # async layer: superseded -> silent, failure -> HomeAssistantError, success -> verify scheduled
    h=mk(); verified=[]; h._schedule_verify=lambda *a: verified.append(a)
    async def go(h, send, **kw): return await h._async_command((1,0), send, **kw)
    await go(h, lambda t: None, verify=(1,0)); assert verified==[(1,0)]
    h=mk(); h._reconnect_with_backoff=lambda *a: (_ for _ in ()).throw(TimeoutError("busy"))
    try: await go(h, lambda t: (_ for _ in ()).throw(MobileVBoxConnectionError("timeout"))); raise SystemExit("should raise")
    except HomeAssistantError: pass
    h=mk(); seq_bump=lambda t: h._cmd_seq.__setitem__((1,0), 99) or (_ for _ in ()).throw(MobileVBoxConnectionError("timeout"))
    await go(h, seq_bump)   # superseded mid-flight: no exception
    # reads never trigger a login
    h=mk(); rec=[]; h._reconnect_with_backoff=lambda *a: rec.append(a)
    try: h._call(lambda: (_ for _ in ()).throw(MobileVBoxConnectionError("timeout"))); raise SystemExit("should raise")
    except MobileVBoxConnectionError: pass
    assert rec==[]
    # metadata: key type/boiler recorded, scenario list = regular list UNION op 66
    class FM:
        def get_room_numbers(s): return []
        def get_node_numbers(s): return [1]
        def get_node_params(s,n): return {"node_type":1,"num_keys":2,"room_no":0}
        def get_key_params(s,n,k): return [{"name":"Lamp","category":2,"type":2,"room_no":0},{"name":"Heater","category":15,"type":21,"room_no":0}][k]
        def get_scenario_numbers(s): return [1]
        def get_scenario_statuses(s): return {1:True,37:False}
        def get_scenario_name(s,n): return f"S{n}"
    h=mk(); h._mobile=FM(); hub.METADATA_PACE=0
    keys,scen=h._fetch_metadata()
    assert [(k["name"],k["ha_type"],k["boiler"],k["key_type"]) for k in keys]==[("Lamp","light",False,2),("Heater","switch",True,21)], keys
    assert [(x["id"],x["listed"],x["has_status"]) for x in scen]==[(1,True,True),(37,False,True)], scen
    assert h.scenario_enabled=={1:True,37:False}
    # a box that NACKs op 66 must not break the scan (and must not be retried with re-logins)
    class FM2(FM):
        n=0
        def get_scenario_statuses(s): FM2.n+=1; raise MobileVBoxError("NACK")
    h=mk(); h._mobile=FM2(); h._connect_mobile=lambda: (_ for _ in ()).throw(SystemExit("re-login on NACK"))
    keys,scen=h._fetch_metadata(); assert FM2.n==1 and [x["id"] for x in scen]==[1] and not scen[0]["has_status"]
    # boiler on carries the app's 0xFFFF timer
    h=mk(); got=[]; h._schedule_verify=lambda *a: None
    h._mobile.set_key_status=lambda n,k,st,ratio=0,timer=0,timeout=None: got.append((n,k,ratio,timer))
    await h.async_boiler_on(7,2); assert got==[(7,2,0,0xFFFF)], got
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
    # a dead-on-arrival session is discarded and reconnect retries until one answers
    class Flaky:
        made=[]
        def __init__(s,**k): s.closed=False; s.n=len(Flaky.made); Flaky.made.append(s)
        def add_push_callback(s,cb): pass
        def connect(s): pass
        def login(s): pass
        def handshake(s,timeout=None):
            if s.n < 2: raise RuntimeError("dead on arrival")
        def close(s): s.closed=True
    hub.MobileVBox=Flaky
    h=mk(); h._data={"host":"x",hub.CONF_MOBILE_PORT:1,hub.CONF_MOBILE_USER:"u",hub.CONF_MOBILE_PASSWORD:"p"}; h.available=False
    h._reconnect_with_backoff()
    assert h._mobile is Flaky.made[2] and Flaky.made[0].closed and Flaky.made[1].closed and h.available, [m.closed for m in Flaky.made]
    # rooms as areas: match an existing area by name, create only if missing, never override a user's area
    class Dev:
        def __init__(s,i,area=None): s.id,s.area_id=i,area
    class Devs:
        def __init__(s): s.d={}; s.upd=[]
        def async_get_or_create(s,config_entry_id,identifiers,**kw):
            key=next(iter(identifiers)); return s.d.setdefault(key,Dev(key[1][-6:], "user-area" if key[1].endswith("node_9") else None))
        def async_update_device(s,i,area_id): s.upd.append((i,area_id))
    class Areas:
        def __init__(s): s.made=[]
        def async_get_area_by_name(s,n): return types.SimpleNamespace(id="a-kitchen") if n.lower()=="kitchen" else None
        def async_create(s,n): s.made.append(n); return types.SimpleNamespace(id="a-"+n)
    devs,areas=Devs(),Areas()
    arm=types.ModuleType("homeassistant.helpers.area_registry"); arm.async_get=lambda h: areas
    drm=types.ModuleType("homeassistant.helpers.device_registry"); drm.async_get=lambda h: devs
    class DeviceInfo(dict): pass
    drm.DeviceInfo=DeviceInfo
    hp=sys.modules["homeassistant.helpers"]; hp.area_registry=arm; hp.device_registry=drm
    sys.modules["homeassistant.helpers.area_registry"]=arm; sys.modules["homeassistant.helpers.device_registry"]=drm
    h=hub.VitreaHub(Hass(),{"host":"x"},{hub.CONF_USE_ROOMS_AS_AREAS:True},"E1")
    h.keys=[{"node":1,"room":"Kitchen"},{"node":2,"room":"Hall"},{"node":9,"room":"Hall"},{"node":3,"room":""}]
    h._async_apply_rooms_as_areas()
    assert sorted(devs.upd)==sorted([("node_1","a-kitchen"),("node_2","a-Hall")]) and areas.made==["Hall"], (devs.upd,areas.made)
    h=hub.VitreaHub(Hass(),{"host":"x"},{},"E1"); h.keys=[{"node":1,"room":"Kitchen"}]; devs.upd.clear(); h._async_apply_rooms_as_areas(); assert devs.upd==[]
    print("all hub tests passed")
asyncio.run(main())
