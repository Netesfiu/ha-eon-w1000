# E.ON W1000 Home Assistant Integration

Natív Home Assistant integráció az E.ON W1000 okosmérő adatainak importálásához email/XLSX exportból.

Ha eddig a [ZsBT/hass-w1000-portal](https://github.com/ZsBT/hass-w1000-portal) vagy az [EON-W1000-n8n](https://github.com/Netesfiu/EON-W1000-n8n) megoldást használtad, ez az integráció kiváltja azokat — nincs szükség külső szerverre, n8n-re vagy Dockerre.

## Mit csinál

1. **IMAP-on keresztül** (Gmail, saját email, stb.) letölti az E.ON portálról érkező ütemezett XLSX exportokat
2. Feldolgozza a **15 perces +A/-A** (fogyasztás/betáplálás) és **napi 1.8.0/2.8.0** (mérőállás) adatokat
3. **Órás bontásban** importálja a Home Assistant energia statisztikáiba
4. Létrehozza a `sensor.eon_w1000_grid_import` és `sensor.eon_w1000_grid_export` entitásokat (total_increasing, kWh)

## Telepítés

### HACS (ajánlott)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Netesfiu&repository=ha-eon-w1000&category=integration)

1. HACS → Egyedi repók → Add hozzá: `https://github.com/Netesfiu/ha-eon-w1000`
2. Telepítsd az integrációt
3. Indítsd újra a Home Assistantot

### Manuális

```bash
cd /config/custom_components
git clone https://github.com/Netesfiu/ha-eon-w1000.git eon_w1000
```

Majd HA újraindítás.

## Beállítás

1. **Beállítások → Eszközök és szolgáltatások → Integrációk → + Hozzáadás**
2. Keresd meg: **E.ON W1000**
3. Add meg az IMAP adatokat:
   - **IMAP szerver**: pl. `imap.gmail.com` (Gmail esetén [app jelszó](https://support.google.com/accounts/answer/185833) kell!)
   - **Felhasználónév / jelszó**
   - **Lekérdezési intervallum**: alapértelmezett 60 perc
   - **Email szűrők**: feladó (`noreply@eon.com`) és tárgy (`[EON-W1000]`)

### E.ON portál beállítás

Az [E.ON portálon](https://e-portal.eon-hungaria.com/w1000) állíts be egy ütemezett exportot:

- **Mérőváltozók**: +A, -A, 1.8.0, 2.8.0
- **Gyakoriság**: naponta
- **Visszamenőleg**: 7 nap
- **Email tárgy**: `[EON-W1000]` (ajánlott)

## Entitások

| Entitás | Leírás | Típus |
|---|---|---|
| `sensor.eon_w1000_grid_import` | Hálózati vételezés (összesített, kWh) | total_increasing |
| `sensor.eon_w1000_grid_export` | Hálózati betáplálás (összesített, kWh) | total_increasing |

## Energia felület beállítása

Az energia felület beállításaiban:
- **Hálózati fogyasztás** → `sensor.eon_w1000_grid_import`
- **Hálózati visszatáplálás** → `sensor.eon_w1000_grid_export`

## Szolgáltatások

- `eon_w1000.process_now` — Azonnali email ellenőrzés és feldolgozás (automatizálásból is hívható)

## Működés

Az integráció:
- Letölti az olvasatlan emaileket, kiolvassa az XLSX mellékletet
- A 15 perces értékeket órás szintre aggregálja
- A mérőállásokat (1.8.0/2.8.0) forward-backward pass algoritmussal rekonstruálja
- `recorder.import_statistics` segítségével betölti a statisztikákat
- Feldolgozás után az emaileket megjelöli olvasottként

## Licensz

MIT — lásd [LICENSE](LICENSE)
