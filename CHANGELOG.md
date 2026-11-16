# Changelog

## 2026.11.1 (16-11-2026)
Eerste versie.
- zenSDK-mock van de Zendure SolarFlow 2400 AC: `GET /properties/report` en `POST /properties/write`
- HomeWizard P1-mock: `GET /api/v1/data`
- Simulatie van huisverbruik (profiel, ruis, waterkoker), PV op een externe omvormer (dagcurve, wolken, limiet in %) en de batterij (ramp, relaispauze, rendement, socSet/minSoc, gridReverse)
- Controle van elke schrijfactie (FOUT / LET OP / flash) met een log in JSONL
- Statuspagina met scenario's, en een bedieningspunt `POST /mock/set`
- HA-testpakket `zendure_mock_test.yaml`, systemd-service en `install.sh`
