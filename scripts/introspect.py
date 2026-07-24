import inspect
import tarfile, gzip, json

print("### OpenAP API introspection ###")
from openap import FuelFlow, prop
try:
    from openap import Emission
    print("Emission available")
except Exception as e:
    print("Emission import err:", e)

print("\n-- FuelFlow.__init__ --")
print(inspect.signature(FuelFlow.__init__))
print("-- FuelFlow methods --")
print([m for m in dir(FuelFlow) if not m.startswith("_")])
for meth in ("enroute", "takeoff", "climb", "cruise", "at_thrust"):
    if hasattr(FuelFlow, meth):
        print(f"  {meth}{inspect.signature(getattr(FuelFlow, meth))}")

print("\n-- prop.aircraft('A320') keys --")
ac = prop.aircraft("A320")
def shorten(d, depth=0):
    if isinstance(d, dict):
        return {k: shorten(v, depth+1) for k, v in d.items()}
    return d
import pprint
pprint.pprint(ac if isinstance(ac, dict) else str(ac))

print("\n-- available aircraft (prop) --")
try:
    from openap import prop as P
    print(P.available_aircraft()[:40])
except Exception as e:
    print("available_aircraft err:", e)

print("\n-- quick FuelFlow sanity: A320 cruise --")
ff = FuelFlow(ac="A320")
try:
    val = ff.enroute(mass=66000, tas=450, alt=35000, vs=0)
    print("enroute ff (kg/s):", val, "-> kg/h:", val*3600)
except Exception as e:
    print("enroute err:", e)

print("\n### trace peek ###")
t = tarfile.open("/mnt/wd_elements/adsb-co2/data/raw/v2026.07.23-planes-readsb-prod-0.tar.aa", mode="r|")
n = 0
for m in t:
    if m.isfile() and "trace_full_" in m.name and m.name.endswith(".json"):
        raw = t.extractfile(m).read()
        gz = raw[:2] == b"\x1f\x8b"
        if gz:
            raw = gzip.decompress(raw)
        d = json.loads(raw)
        print("name", m.name, "gzip?", gz, "npts", len(d.get("trace", [])),
              "meta", {k: d.get(k) for k in ("icao", "r", "t", "dbFlags", "timestamp")})
        print("  first", d["trace"][0])
        n += 1
        if n >= 3:
            break
t.close()
