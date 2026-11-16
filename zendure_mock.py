#!/usr/bin/env python3
"""
Zendure zenSDK mock + HomeWizard P1 mock - voor een TEST-Home Assistant.

Doet zich voor als een Zendure SolarFlow 2400 AC (lokale zenSDK-API) en een
HomeWizard P1-meter, met een eenvoudige simulatie van huis, zonnepanelen (externe
omvormer) en batterij. Elke schrijfactie wordt gelogd en gecontroleerd.

Alleen de Python-standaardbibliotheek (3.9+). Starten:
    python3 zendure_mock.py --port 8080

Endpoints
  GET  /properties/report   zenSDK-rapport
  POST /properties/write    zenSDK-schrijfactie (toegepast, gecontroleerd, gelogd)
  GET  /api/v1/data         HomeWizard P1 (active_power_w)
  GET  /mock/state          volledige simulatiestatus + laatste schrijfacties (JSON)
  POST /mock/set            simulatie aanpassen, bv. {"soc": 20} of {"load_w": 800}
  GET  /                    statuspagina
"""
import argparse
import collections
import datetime as dt
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2026.11.2"

# kWh per pack, zelfde mapping als sensor "Zendure 2400 AC Totale Capaciteit" (Gielz)
PACK_KWH = {5: 2.88, 70: 1.92, 250: 0.96, 240: 1.92, 300: 1.92, 350: 2.88, 500: 2.40}

# Schrijfbare eigenschappen met toegestaan bereik (zoals Gielz ze schrijft:
# socSet/minSoc in promille). Volgens de zenSDK-documentatie.
RW_RANGES = {
    "acMode": (1, 2),
    "inputLimit": (0, 2400),
    "outputLimit": (0, 2400),
    "socSet": (700, 1000),
    "minSoc": (0, 500),
    "gridReverse": (0, 2),
    "smartMode": (0, 1),
    "gridOffMode": (0, 2),
    "chargeMaxLimit": (0, 2400),
    "inverseMaxPower": (0, 2400),
}
WARN_PROPS = {
    "chargeMaxLimit": "staat in de zenSDK als alleen-lezen (RO)",
    "inverseMaxPower": "fabrikant raadt aanpassen af",
}
FORBIDDEN_PROPS = {"batCalTime", "gridStandard", "Fanmode", "Fanspeed"}


class Sim:
    def __init__(self, a):
        self.lock = threading.RLock()
        self.sn = a.sn
        self.product = a.product
        self.packs = a.packs
        self.pack_type = a.pack_type
        self.cap_kwh = PACK_KWH.get(a.pack_type, 2.88) * a.packs
        self.soc = float(a.soc)
        self.speed = float(a.speed)
        self.eff = 0.95
        self.ramp_w_s = float(a.ramp)
        self.relay_delay_s = float(a.relay_delay)
        # Zendure-instellingen
        self.p = {
            "acMode": 1, "inputLimit": 0, "outputLimit": 0,
            "socSet": 1000, "minSoc": 100, "gridReverse": 1,
            "smartMode": 0, "gridOffMode": 2,
            "chargeMaxLimit": 2400, "inverseMaxPower": 2400,
        }
        self.power_ac = 0.0          # + laden, - ontladen (AC-zijde)
        self.relay_hold_until = 0.0
        self.calibrating = False
        self.error = 0
        # Huis en zon
        self.pv_peak_w = float(a.pv_peak)
        self.pv_limit_pct = 100.0
        self.load_override = None
        self.p1_override = None
        self.clouds_override = None
        self._noise = 0.0
        self._clouds = 0.8
        self._spike_until = 0.0
        self.load_w = 0.0
        self.pv_w = 0.0
        # Log en tellers
        self.writes = collections.deque(maxlen=300)
        self.log_path = a.log
        self.day = dt.date.today()
        self.cnt = collections.Counter()
        self.last_tick = time.time()

    # ---------- simulatie ----------
    def _pv_profile(self, now):
        lt = time.localtime(now)
        doy = lt.tm_yday
        season = math.cos(2 * math.pi * (doy - 172) / 365)      # 1 = juni, -1 = december
        half_day = 6 + 2.5 * season                              # uren
        noon = 12.7 + (1 if lt.tm_isdst > 0 else 0)
        h = lt.tm_hour + lt.tm_min / 60 + lt.tm_sec / 3600
        x = (h - noon) / half_day
        if abs(x) >= 1:
            return 0.0
        amp = 0.55 + 0.45 * season
        clouds = self.clouds_override if self.clouds_override is not None else self._clouds
        return self.pv_peak_w * amp * math.cos(math.pi / 2 * x) ** 1.5 * clouds

    def _load_profile(self, now):
        lt = time.localtime(now)
        h = lt.tm_hour + lt.tm_min / 60
        base = 250
        morning = 500 * math.exp(-((h - 7.5) / 1.0) ** 2)
        evening = 900 * math.exp(-((h - 19.0) / 2.0) ** 2)
        spike = 2000 if now < self._spike_until else 0
        return max(80.0, base + morning + evening + self._noise + spike)

    def tick(self):
        with self.lock:
            now = time.time()
            dt_s = min(now - self.last_tick, 5.0)
            self.last_tick = now
            if dt.date.today() != self.day:
                self.day = dt.date.today()
                self.cnt.clear()
            # ruis, wolken, waterkoker
            self._noise = max(-150, min(150, self._noise + random.uniform(-20, 20) * dt_s))
            self._clouds = max(0.3, min(1.0, self._clouds + random.uniform(-0.01, 0.01) * dt_s))
            if now > self._spike_until and random.random() < 0.0005 * dt_s:
                self._spike_until = now + 180
            self.load_w = self.load_override if self.load_override is not None else self._load_profile(now)
            self.pv_w = self._pv_profile(now) * self.pv_limit_pct / 100.0

            # doelvermogen van de batterij
            p = self.p
            soc_max = p["socSet"] / 10.0
            soc_min = p["minSoc"] / 10.0
            if p["acMode"] == 1:
                tgt = float(min(p["inputLimit"], p["chargeMaxLimit"]))
                if self.soc >= soc_max:
                    tgt = 0.0
            else:
                tgt = -float(min(p["outputLimit"], p["inverseMaxPower"]))
                if self.soc <= soc_min:
                    tgt = 0.0
                if p["gridReverse"] == 2:      # terugleveren verboden: niet meer dan het huis vraagt
                    tgt = max(tgt, -max(0.0, self.load_w - self.pv_w))
            if now < self.relay_hold_until:
                tgt = 0.0
            step = self.ramp_w_s * dt_s
            self.power_ac += max(-step, min(step, tgt - self.power_ac))
            if abs(self.power_ac) < 1:
                self.power_ac = 0.0

            # energie (versneld met speed)
            dc = self.power_ac * self.eff if self.power_ac > 0 else self.power_ac / self.eff
            self.soc += dc * dt_s * self.speed / 3600 / (self.cap_kwh * 1000) * 100
            self.soc = max(0.0, min(100.0, self.soc))

    def p1(self):
        if self.p1_override is not None:
            return float(self.p1_override)
        return self.load_w - self.pv_w + self.power_ac

    # ---------- zenSDK ----------
    def report(self):
        with self.lock:
            p = self.p
            charge = max(self.power_ac, 0.0)
            dis = max(-self.power_ac, 0.0)
            dc_charge = charge * self.eff
            dc_dis = dis / self.eff
            state = 1 if charge > 0 else 2 if dis > 0 else 0
            soc_max = p["socSet"] / 10.0
            soc_min = p["minSoc"] / 10.0
            soc_limit = 1 if self.soc >= soc_max - 0.05 else 2 if self.soc <= soc_min + 0.05 else 0
            remain = int((self.soc - soc_min) / 100 * self.cap_kwh * 1000 / dis * 60) if dis > 0 else 59940
            per_pack = (dc_charge or dc_dis) / self.packs
            packs = []
            for i in range(self.packs):
                packs.append({
                    "sn": f"{self.sn[:-2]}P{i + 1}",
                    "packType": self.pack_type,
                    "socLevel": int(round(self.soc)),
                    "state": state,
                    "power": int(round(per_pack)),
                    "maxTemp": 2731 + 180 + i * 5,
                    "totalVol": int(4800 + self.soc * 4),
                    "batcur": int(round(per_pack / 51.2 * 10)) * (1 if state == 1 else -1 if state == 2 else 0),
                    "maxVol": 332 + (1 if i % 2 else 0),
                    "minVol": 330,
                    "softVersion": 4096,
                })
            props = {
                "heatState": 0,
                "packInputPower": int(round(dis)),
                "outputPackPower": int(round(charge)),
                "outputHomePower": int(round(dis)),
                "remainOutTime": remain,
                "packState": state,
                "electricLevel": int(round(self.soc)),
                "gridInputPower": int(round(charge)),
                "solarInputPower": 0,
                "solarPower1": 0, "solarPower2": 0, "solarPower3": 0,
                "solarPower4": 0, "solarPower5": 0, "solarPower6": 0,
                "pass": 0,
                "reverseState": 0,
                "socStatus": 1 if self.calibrating else 0,
                "hyperTmp": 2731 + 350,
                "gridOffPower": 0,
                "dcStatus": 0,
                "pvStatus": 0,
                "acStatus": 1 if state else 0,
                "dataReady": 1,
                "gridState": 1,
                "BatVolt": int(4800 + self.soc * 4),
                "socLimit": soc_limit,
                "writeRsp": 0,
                "packNum": self.packs,
                "is_error": self.error,
                "rssi": -55,
                **p,
            }
            return {
                "timestamp": int(time.time()),
                "messageId": random.randint(1, 99999),
                "sn": self.sn,
                "version": 2,
                "product": self.product,
                "properties": props,
                "packData": packs,
            }

    def write(self, raw, client):
        entry = {"tijd": dt.datetime.now().isoformat(timespec="seconds"), "van": client,
                 "props": None, "status": "ok", "meldingen": []}
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("properties"), dict):
                raise ValueError("verwacht {\"sn\": ..., \"properties\": {...}}")
        except Exception as e:  # noqa: BLE001
            entry.update(status="FOUT", meldingen=[f"ongeldige JSON: {e}"], raw=raw[:300])
            return self._log(entry), 400
        props = body["properties"]
        entry["props"] = props
        with self.lock:
            sn = str(body.get("sn")).strip()
            if sn.lower() in ("unknown", "unavailable", "none", ""):
                entry["status"] = "LET OP"
                entry["meldingen"].append(
                    f"sn '{sn}': serienummer-sensor had nog geen waarde (opstarten of Zendure "
                    "onbereikbaar); niet toegepast")
                return self._log(entry), 200
            if sn != self.sn:
                entry["status"] = "FOUT"
                entry["meldingen"].append(f"sn '{body.get('sn')}' klopt niet (verwacht {self.sn}); niet toegepast")
                return self._log(entry), 200
            applied = {}
            for k, v in props.items():
                if k in FORBIDDEN_PROPS:
                    entry["status"] = "FOUT"
                    entry["meldingen"].append(f"{k}: niet schrijven (zenSDK)")
                    continue
                if k not in RW_RANGES:
                    entry["status"] = "FOUT"
                    entry["meldingen"].append(f"{k}: onbekende of alleen-lezen eigenschap")
                    continue
                try:
                    iv = int(round(float(v)))
                except (TypeError, ValueError):
                    entry["status"] = "FOUT"
                    entry["meldingen"].append(f"{k}={v!r}: geen getal")
                    continue
                lo, hi = RW_RANGES[k]
                if not lo <= iv <= hi:
                    entry["status"] = "FOUT"
                    entry["meldingen"].append(f"{k}={iv}: buiten bereik {lo}-{hi}")
                    continue
                if k in WARN_PROPS:
                    entry["meldingen"].append(f"{k}: {WARN_PROPS[k]}")
                    if entry["status"] == "ok":
                        entry["status"] = "LET OP"
                applied[k] = iv
            if "acMode" in applied and applied["acMode"] != self.p["acMode"]:
                self.relay_hold_until = time.time() + self.relay_delay_s
                self.cnt["relais"] += 1
            if applied.get("inputLimit", 0) > applied.get("chargeMaxLimit", self.p["chargeMaxLimit"]):
                entry["meldingen"].append("inputLimit hoger dan chargeMaxLimit (wordt begrensd)")
            self.p.update(applied)
            changed = [k for k in applied if k != "smartMode"]
            if self.p["smartMode"] == 0 and changed:
                entry["flash"] = True
                self.cnt["flash"] += 1
        return self._log(entry), 200

    def _log(self, entry):
        with self.lock:
            self.cnt["writes"] += 1
            if entry["status"] != "ok":
                self.cnt["meldingen"] += 1
            self.writes.appendleft(entry)
        line = json.dumps(entry, ensure_ascii=False)
        print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        return {"success": entry["status"] != "FOUT", "meldingen": entry["meldingen"]}

    # ---------- bediening ----------
    def state(self):
        with self.lock:
            return {
                "versie": VERSION,
                "sn": self.sn,
                "soc": round(self.soc, 2),
                "capaciteit_kwh": round(self.cap_kwh, 2),
                "power_ac_w": round(self.power_ac),
                "load_w": round(self.load_w),
                "pv_w": round(self.pv_w),
                "p1_w": round(self.p1()),
                "pv_peak_w": self.pv_peak_w,
                "pv_limit_pct": self.pv_limit_pct,
                "load_override": self.load_override,
                "p1_override": self.p1_override,
                "clouds_override": self.clouds_override,
                "speed": self.speed,
                "calibrating": self.calibrating,
                "error": self.error,
                "relais_wacht": self.relay_hold_until > time.time(),
                "instellingen": dict(self.p),
                "writes_today": self.cnt["writes"],
                "flash_writes_today": self.cnt["flash"],
                "warnings_today": self.cnt["meldingen"],
                "relais_today": self.cnt["relais"],
                "laatste_writes": list(self.writes)[:50],
            }

    def set(self, d):
        with self.lock:
            if "soc" in d:
                self.soc = max(0.0, min(100.0, float(d["soc"])))
            for key, attr in (("load_w", "load_override"), ("p1_override_w", "p1_override"),
                              ("clouds", "clouds_override")):
                if key in d:
                    v = d[key]
                    setattr(self, attr, None if v in (None, "", "auto", "null", "None") else float(v))
            if "pv_peak_w" in d:
                self.pv_peak_w = max(0.0, float(d["pv_peak_w"]))
            if "pv_limit_pct" in d:
                self.pv_limit_pct = max(0.0, min(100.0, float(d["pv_limit_pct"])))
            if "speed" in d:
                self.speed = max(0.1, float(d["speed"]))
            if "calibrating" in d:
                v = d["calibrating"]
                self.calibrating = v.strip().lower() in ("1", "true", "ja", "on") if isinstance(v, str) else bool(v)
            if "error" in d:
                self.error = 1 if d["error"] else 0
            if "spike" in d:
                self._spike_until = time.time() + float(d["spike"])
            if d.get("reset_counters"):
                self.cnt.clear()
                self.writes.clear()
            if isinstance(d.get("props"), dict):          # direct instellen, zonder log
                for k, v in d["props"].items():
                    if k in self.p:
                        self.p[k] = int(v)
        return self.state()


PAGE = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zendure mock</title><style>
body{font-family:system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#222}
h1{font-size:20px;margin:0 0 12px} .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.t{background:#fff;border-radius:8px;padding:10px;box-shadow:0 1px 2px #0001}.t b{display:block;font-size:22px}
form{background:#fff;border-radius:8px;padding:10px;margin:12px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:end}
label{font-size:12px;display:flex;flex-direction:column}input{width:110px;padding:4px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
.FOUT{background:#fde2e2}.LET{background:#fff4d6}.flash{color:#b45309;font-weight:600}
</style></head><body><h1>Zendure zenSDK mock <small id="v"></small></h1>
<div class="grid" id="tiles"></div>
<form id="f">
<label>SOC %<input name="soc"></label>
<label>Huisverbruik W (leeg = profiel)<input name="load_w"></label>
<label>P1 vast W (leeg = sim)<input name="p1_override_w"></label>
<label>PV piek W<input name="pv_peak_w"></label>
<label>PV-limiet %<input name="pv_limit_pct"></label>
<label>Wolken 0-1 (leeg = auto)<input name="clouds"></label>
<label>Snelheid x<input name="speed"></label>
<label>Waterkoker s<input name="spike"></label>
<label>Kalibreren<select name="calibrating"><option value="">-</option><option value="1">ja</option><option value="0">nee</option></select></label>
<button>Toepassen</button> <button type="button" id="r">Tellers wissen</button></form>
<table><thead><tr><th>Tijd</th><th>Status</th><th>Eigenschappen</th><th>Meldingen</th></tr></thead><tbody id="w"></tbody></table>
<script>
const keep=new Set(["load_w","p1_override_w","clouds"]);
async function upd(){const s=await (await fetch('/mock/state')).json();
document.getElementById('v').textContent='v'+s.versie+' · sn '+s.sn;
const t=[['SOC',s.soc.toFixed(1)+' %'],['Batterij AC',s.power_ac_w+' W'+(s.relais_wacht?' (relais)':'')],['P1',s.p1_w+' W'],
['Huis',s.load_w+' W'],['PV',s.pv_w+' W ('+s.pv_limit_pct+'%)'],['acMode',s.instellingen.acMode==1?'1 laden':'2 ontladen'],
['inputLimit',s.instellingen.inputLimit+' W'],['outputLimit',s.instellingen.outputLimit+' W'],['smartMode',s.instellingen.smartMode?'1 RAM':'0 Flash'],
['socSet/minSoc',s.instellingen.socSet/10+' / '+s.instellingen.minSoc/10+' %'],['gridReverse',s.instellingen.gridReverse],
['Writes vandaag',s.writes_today],['Flash-writes',s.flash_writes_today],['Relaiswissels',s.relais_today],['Meldingen',s.warnings_today],['Snelheid','x'+s.speed]];
document.getElementById('tiles').innerHTML=t.map(x=>'<div class=t>'+x[0]+'<b>'+x[1]+'</b></div>').join('');
document.getElementById('w').innerHTML=s.laatste_writes.map(e=>'<tr class="'+e.status.split(' ')[0]+'"><td>'+e.tijd.slice(11)+'</td><td>'+e.status+(e.flash?' <span class=flash>flash</span>':'')+'</td><td><code>'+JSON.stringify(e.props)+'</code></td><td>'+e.meldingen.join('<br>')+'</td></tr>').join('');}
document.getElementById('f').onsubmit=async ev=>{ev.preventDefault();const d={};
for(const [k,v] of new FormData(ev.target)){if(v!=='')d[k]=isNaN(v)?v:Number(v);else if(keep.has(k)&&ev.target[k].dataset.touched)d[k]=null;}
if('calibrating' in d)d.calibrating=!!d.calibrating;
await fetch('/mock/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});ev.target.reset();upd();};
document.querySelectorAll('#f input').forEach(i=>i.oninput=()=>i.dataset.touched=1);
document.getElementById('r').onclick=async()=>{await fetch('/mock/set',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"reset_counters":true}'});upd();};
upd();setInterval(upd,2000);
</script></body></html>"""


def make_handler(sim):
    class H(BaseHTTPRequestHandler):
        server_version = "ZendureMock/" + VERSION

        def log_message(self, *args):  # stil; writes worden apart gelogd
            pass

        def _send(self, code, obj, ctype="application/json"):
            data = obj.encode() if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n).decode("utf-8", "replace") if n else ""

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/properties/report":
                self._send(200, sim.report())
            elif path == "/api/v1/data":
                self._send(200, {"wifi_strength": 80, "active_power_w": round(sim.p1(), 1),
                                 "active_power_l1_w": round(sim.p1(), 1), "meter_model": "MOCK"})
            elif path == "/mock/state":
                self._send(200, sim.state())
            elif path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html")
            else:
                self._send(404, {"error": "onbekend pad"})

        def do_POST(self):
            path = self.path.split("?")[0]
            raw = self._body()
            if path == "/properties/write":
                res, code = sim.write(raw, self.client_address[0])
                self._send(code, res)
            elif path == "/mock/set":
                try:
                    self._send(200, sim.set(json.loads(raw or "{}")))
                except Exception as e:  # noqa: BLE001
                    self._send(400, {"error": str(e)})
            else:
                self._send(404, {"error": "onbekend pad"})

    return H


def main():
    ap = argparse.ArgumentParser(description="Zendure zenSDK + HomeWizard P1 mock")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--sn", default="MOCK2400AC000001")
    ap.add_argument("--product", default="solarFlow2400AC")
    ap.add_argument("--packs", type=int, default=4, help="aantal accu's (1-6)")
    ap.add_argument("--pack-type", type=int, default=350, help="packType (350 = 2,88 kWh)")
    ap.add_argument("--soc", type=float, default=50)
    ap.add_argument("--pv-peak", type=float, default=4000, help="piekvermogen externe omvormer (W)")
    ap.add_argument("--speed", type=float, default=1, help="SOC-verloop versnellen (1 = echt)")
    ap.add_argument("--ramp", type=float, default=500, help="vermogensverandering W/s")
    ap.add_argument("--relay-delay", type=float, default=3, help="wachttijd bij wissel acMode (s)")
    ap.add_argument("--log", default="writes.jsonl", help="schrijflog (leeg = geen bestand)")
    a = ap.parse_args()

    sim = Sim(a)

    def loop():
        while True:
            sim.tick()
            time.sleep(0.5)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(sim))
    print(f"Zendure mock {VERSION} op http://{a.host}:{a.port}  (sn {a.sn}, {sim.cap_kwh:.2f} kWh)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
