# Zendure mock server

Testserver voor [Zendure zenSDK + Forecasting](https://github.com/cardinal808/Zendure-HA-zenSDK-with-Forecasting) (gebaseerd op [Gielz1986/Zendure-HA-zenSDK](https://github.com/Gielz1986/Zendure-HA-zenSDK)). Hiermee test je de set in een aparte test-Home Assistant zonder dat je echte batterij wordt aangestuurd.

`zendure_mock.py` doet zich voor als een **Zendure SolarFlow 2400 AC** (lokale zenSDK-API) en een **HomeWizard P1-meter**. Er zit een eenvoudige simulatie in van huisverbruik, zonnepanelen op een externe omvormer en de batterij. De test-HA praat met de mock in plaats van met je echte apparaten. Elke schrijfactie wordt gecontroleerd en gelogd.

## Wat wordt nagebootst

- `GET /properties/report` geeft alle velden die `zendure_gielz1986_nl.yaml` leest: `properties` (electricLevel, acMode, input-/outputLimit, smartMode, socSet/minSoc in promille, gridReverse, socLimit, chargeMaxLimit, inverseMaxPower, …) en `packData` (packType 350 = 2,88 kWh, standaard 4 accu's = 11,52 kWh).
- `POST /properties/write` past de waarden toe en controleert:
  - **FOUT** (rood): ongeldige JSON (bv. een template die `None` oplevert), een verkeerd `sn`, een onbekende of alleen-lezen eigenschap, of een waarde buiten bereik (socSet 700–1000, minSoc 0–500, limieten 0–2400). Zo'n schrijfactie wordt **niet** toegepast.
  - **LET OP** (geel): `chargeMaxLimit` (RO volgens de zenSDK) en `inverseMaxPower` (fabrikant raadt af). Wordt wel toegepast.
  - **flash**: de schrijfactie viel in de stand `smartMode 0`. De teller *Flash-writes* laat zien hoe vaak dat per dag gebeurt.
- **Batterij:** laadt of ontlaadt naar `inputLimit`/`outputLimit` met 500 W/s ramp, 3 s relaispauze bij een wissel van `acMode` en 95 % rendement per richting. Stopt bij `socSet`/`minSoc` (socLimit 1/2). Met `gridReverse 2` ontlaadt hij niet meer dan het huis vraagt.
- **P1** (`/api/v1/data`) = huisverbruik − PV + batterijvermogen.
- **Huisverbruik:** basislast met een ochtend- en avondpiek, ruis en af en toe een waterkoker (2 kW, 3 min).
- **PV:** dagcurve van de externe omvormer op basis van datum en tijd (NL), met wolken. De curve wordt geschaald met `pv_limit_pct`, de limiter voor het pakket Negatieve prijs.

Niet nagebootst: PV op de Zendure zelf (`solarInputPower`, `gridOffPower`, `pass`). Die zijn altijd 0, net als bij de 2400 AC zonder MPPT.

## Installatie in Proxmox (LXC)

1. Maak een Debian 12-container aan: 1 core, 256 MB RAM, 2 GB schijf en een vast IP, bijvoorbeeld `192.168.1.60`.
2. Installeer de mock in de container (console of `pct enter <ctid>`):
   ```bash
   apt update && apt install -y curl
   curl -fsSL https://raw.githubusercontent.com/cardinal808/zendure_mock_server/main/install.sh | bash
   ```
   Hetzelfde commando werkt ook een bestaande installatie bij. Een vaste versie installeren:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/cardinal808/zendure_mock_server/v2026.11.1/install.sh | REF=v2026.11.1 bash
   ```
3. Open de statuspagina: `http://192.168.1.60:8080/`.
4. Bekijk de log live met `journalctl -u zendure-mock -f`. De log staat ook in `/var/lib/zendure-mock/writes.jsonl`.

Opties staan in de `ExecStart`-regel van `/etc/systemd/system/zendure-mock.service`: `--soc`, `--packs`, `--pack-type`, `--pv-peak`, `--speed` (SOC-verloop versnellen, bv. 30), `--ramp`, `--relay-delay`, `--sn`. Pas ze aan en voer `systemctl daemon-reload && systemctl restart zendure-mock` uit. Bij een update laat `install.sh` de service ongemoeid, zodat je eigen opties blijven staan.

## Koppelen in de test-HA

1. Zet de set zoals gebruikelijk in `packages`, plus **`zendure_mock_test.yaml`**. Dat pakket hoort **alleen in de test**. Vanuit de Terminal-add-on van de test-HA:
   ```bash
   curl -fsSL -o /config/packages/zendure_mock_test.yaml https://raw.githubusercontent.com/cardinal808/zendure_mock_server/main/zendure_mock_test.yaml
   ```
   Herstart daarna Home Assistant.
2. Vul in Configuratie het volgende in:
   - Zendure IP-adres: `192.168.1.60:8080`
   - HomeWizard P1 IP-adres: `192.168.1.60:8080`
   - Zonnepanelen sensor / omvormer-vermogenssensor: `sensor.mock_pv_vermogen`
   - Negatieve prijs: aantal omvormers `1`, type *Productielimiet*, limiter `number.mock_omvormer_limiet`
3. Laat de Proxmox-firewall van de test-VM alleen de mock doorlaten, en blokkeer de echte Zendure, SolarEdge en airco.

## Scenario's

Gebruik de statuspagina, of in HA `script.mock_zendure_scenario` met `key`/`value`:

| Wat | key | value |
|---|---|---|
| Accu bijna leeg / vol | `soc` | `8` / `99` |
| Vast huisverbruik / terug naar profiel | `load_w` | `1800` / `null` |
| Vaste P1-waarde (bv. teruglevering) | `p1_override_w` | `-1500` / `null` |
| Zonnige of bewolkte dag | `clouds` | `1` / `0.3` / `null` |
| Grotere PV-installatie | `pv_peak_w` | `6000` |
| Waterkoker nu | `spike` | `180` |
| Kalibratie bezig (socStatus 1) | `calibrating` | `true` / `false` |
| Foutmelding (is_error 1) | `error` | `1` / `0` |
| Sneller laden/ontladen | `speed` | `30` |

Rechtstreeks met curl kan ook: `curl -X POST http://192.168.1.60:8080/mock/set -d '{"soc": 20}'`.

## Waar je op let bij een release-test

- **Rode regels:** elke FOUT is een bug in een template of `rest_command`.
- **Flash-writes per dag:** een paar is normaal. Tientallen wijzen op heen-en-weer schakelen tussen RAM en Flash.
- **Relaiswissels per dag:** veel wissels betekenen pendelen tussen laden en ontladen.
- **Gedrag bij grenzen:** wat doet de automation bij `soc` 8 of 99, en bij `calibrating`? Klopt de laadcontrole van de forecasting?
