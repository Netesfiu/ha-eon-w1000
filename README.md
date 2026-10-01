# E.ON W1000 Home Assistant integráció

Natív Home Assistant integráció az E.ON W1000 okosmérő adatainak importálásához email/XLSX exportból.

Ha eddig a [ZsBT/hass-w1000-portal](https://github.com/ZsBT/hass-w1000-portal) vagy az [EON-W1000-n8n](https://github.com/Netesfiu/EON-W1000-n8n) megoldást használtad, ez az integráció kiváltja azokat — nincs szükség külső szerverre, n8n-re vagy Dockerre.

## Mit csinál

1. **IMAP-on keresztül** (Gmail, saját email, stb.) letölti az E.ON portálról érkező ütemezett XLSX exportokat
2. Feldolgozza a **15 perces +A/-A** (fogyasztás/betáplálás) és **napi 1.8.0/2.8.0** (mérőállás) adatokat
3. **Órás bontásban** importálja a Home Assistant energia statisztikáiba, **a saját sorozatába**: `sensor.eon_w1000_eon_w1000_grid_import` / `sensor.eon_w1000_eon_w1000_grid_export`. A régi `sensor.grid_energy_*` sorozatot **sem olvassa, sem írja** — a korábbi import (2026. május 19. – július 14., 1368 óra) érintetlen marad
4. Létrehozza a `sensor.eon_w1000_*` entitásokat a legutóbbi import állapotával

## A helyesség szabályai

Ezek nem stílus kérdések, hanem a korábbi importáló ismert hibáinak lezárásai:

* **Excel-only, saját sorozat.** A kumulatív görbét **kizárólag a XLSX archívumból** építi fel, 0.0 bázissal az első importált óra előtti határórán, egész Wh-ban halmozva. Nem olvas vissza semmilyen korábban tárolt összeget (sem a saját, sem a legacy sorozatból), ezért nincs szükség horgonyra, és nem keletkezhet eltolódás egy meg nem lévő sorozat miatt. Az átfedő rolling ablakok így is idempotensek: ugyanannak az órának mindig ugyanaz az értéke, akárhány fájlból származik.
* **Nincs ANCHOR-hiba, de nincs csendes lyuk sem.** Mivel a bázis mindig 0.0, `no_anchor` állapot nem fordulhat elő. Ha viszont az archívumban **rés, csonka vagy kétértelmű óra** van, az import megszakad (`status: no_data`, `last_error` a rés leírásával) ahelyett, hogy lyukas sorozatot írna.
* **Csak folytonos, teljes órák importálhatók.** A csonka negyedórás farok, egy hiányzó óra, egy rés vagy egy félig kitöltött csatorna **megállítja** az importot az adott óránál — nem ugrik át, és nem egészít ki nullával.
* **A hiányzó érték hiányzó marad.** Ha egy negyedóra értéke nincs a fájlban, az nem 0.0 lesz (ami valósnak látszó nulla fogyasztást jelentene), hanem kimarad.
* **Idempotens.** Ugyanannak az ablaknak az újraimportálása bájtazonos statisztikát ad.
* **A postafiók olvasottsági jelzője nem szűrő.** A keresés dátumtartomány (nem `UNSEEN`), a duplikátumokat `Message-ID` alapján szűri, és a levelet **csak sikeres import után** jelöli olvasottnak. Ha bármi elhasal (hálózat, horgony, parse), a levél a következő körben újra feldolgozásra kerül — nem veszik el az adat.
* **Lánc-ellenőrzés.** Írás előtt ellenőrzi, hogy minden óra értéke pontosan az előző óra értéke + az adott óra energiája; eltérésnél megszakítja az importot.

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
   - **Visszatekintés**: alapértelmezett 10 nap
   - **Email szűrők**: feladó (`noreply@eon.com`) és tárgy (`[EON-W1000]`)

### E.ON portál beállítás

Az [E.ON portálon](https://e-portal.eon-hungaria.com/w1000) állíts be egy ütemezett exportot:

- **Mérőváltozók**: +A, -A, 1.8.0, 2.8.0
- **Gyakoriság**: naponta
- **Visszamenőleg**: 7 nap
- **Email tárgy**: `[EON-W1000]` (ajánlott)

## Entitások

| Entitás | Leírás |
|---|---|
| `sensor.eon_w1000_eon_w1000_grid_import` | Saját energia sorozat (`..._import`), a legutóbbi importált Excel összeggel |
| `sensor.eon_w1000_eon_w1000_grid_export` | Saját energia sorozat (`..._export`), a legutóbbi importált Excel összeggel |
| `sensor.eon_w1000_last_update` | Utolsó postafiók-ellenőrzés |
| `sensor.eon_w1000_last_processing` | Utolsó sikeres import |
| `button.eon_w1000_process_now` | Azonnali postafiók-ellenőrzés |

Az energiaszenzorok `state_class: total` + `device_class: energy`, de az **élő állapotuk szándékosan `unknown`**: a statisztikát kizárólag az import írja. Ha az élő állapot egy szám lenne, a recorder minden feldolgozásnál a *feldolgozás pillanatára* könyvelne el fogyasztást (késleltetett, pontatlan, a jövőbe mutató ugrásokat okozó minta). A hasznos állapot attribútumokban van:

- `status` — `ok` / `no_mail` / `no_data` / `parse_error` / `mail_error`
- `statistic_id` — a saját sorozat azonosítója (ezt add hozzá az Energy felületen)
- `historical_total` — a legutóbb importált Excel összeg (kWh)
- `history_source` — `excel_only`
- `last_processing`, `last_window_from`, `last_window_to` — az utolsó sikeres import
- `raw_meter_register` — a fájlban látott nyers mérőállás (1.8.0 / 2.8.0)
- `skipped_hours`, `skipped_detail` — hány órát és miért hagyott ki
- `duplicate_hours` — hány óra hordozott a szokásosnál több negyedórát (lásd Korlátok)
- `last_error` — az utolsó hiba szövege

## Energia felület beállítása

Az energia felület beállításaiban (Beállítások → Irányítópultok → Energia → Hálózati fogyasztás / visszatáplálás):

- **Hálózati fogyasztás** → `sensor.eon_w1000_eon_w1000_grid_import`
- **Hálózati visszatáplálás** → `sensor.eon_w1000_eon_w1000_grid_export`

A régi `sensor.grid_energy_import` / `sensor.grid_energy_export` bejegyzést **ne** hagyd bent a listában, ha ugyanazt a hálózati pontot kétszer számolnád; a mögötte lévő előzmény adat megmarad, csak ne legyen kijelölve.

## Szolgáltatások

- `eon_w1000.process_now` — Azonnali email ellenőrzés és feldolgozás (automatizálásból is hívható)
- `eon_w1000.import_files` — Egy vagy több helyi XLSX exportfájl importálása (kezdeti backfillhez, amikor a levelek már nincsenek a postafiókban)

```yaml
service: eon_w1000.import_files
data:
  paths:
    - /config/eon/2026-05-19.xlsx
    - /config/eon/2026-05-20.xlsx
```

## Áttérés a korábbi importálóról (n8n)

1. **Állítsd le az n8n workflow-t** (`W1000 Digest data` és a hívó workflow a Gmail triggerrel), hogy ne legyen két író ugyanabba a sorozatba.
2. HACS → frissítés erre a verzióra → HA újraindítás.
3. Az Energy felületen **állítsd át a forrásokat** a saját sorozatokra (lásd fent), és vedd ki a régi `sensor.grid_energy_*` bejegyzéseket a kijelölésből. A mögöttük lévő 2026. máj.–júl. előzmény a rekorderben **megmarad**, csak nem lesz kijelölve — az integráció hozzá sem nyúl.
4. Ha az őszi levelek már nincsenek a postafiókban, futtasd a `eon_w1000.import_files` szolgáltatást a meglévő XLSX-ekre; egyébként nyomd meg a **Process export mail now** gombot (vagy várd meg a következő kört).
5. Ellenőrizd a `sensor.eon_w1000_eon_w1000_grid_import` attribútumait: `status: ok`, `skipped_hours: 0`, `historical_total` a legutóbbi Excel összeggel. Az energia felületen a fogyasztás és a visszatáplálás **külön görbe** kell legyen (a korábbi hiba épp az volt, hogy a betáplálás sorozatba a vételezés lánca került).

`status: no_data` + `last_error` esetén az archívumban rés vagy csonka óra van: egészítsd ki a hiányzó XLSX-szel, majd futtasd újra (a művelet idempotens).

## Korlátok

* **Őszi óraátállítás.** A helyi idő szerint címkézett fájlban az óraátállítás napján a 02:00 óra kétszer szerepel, így abban a vödörben két fizikai óra energiája lesz. Az energia nem vész el (minden negyedóra beleszámít), de az az egy nap órás bontása eltolódik; az integráció ezt `duplicate_hours`-ként jelzi.
* **A fájl 1.8.0/2.8.0 regiszterei és a +A/-A sorok napjai.** A valós exportokban a mérőóra-regiszter csak minden nap 00:00 sorában van kitöltve, és a **regiszter-oszlop** naptári napjai egy nappal eltérnek a +A/-A sorok címkéitől: a `D` nap 00:00 sorában álló mérőállás valójában a `D` nap **záró** értéke (azaz a `D+1` 00:00-kor mért állás). Ezért a napi fogyasztás `R(D) − R(D−1)`, és nem `R(D+1) − R(D)`; közvetlenül párosítva 0,4–10 kWh eltérés adódik, a helyes párosítással 0,001 kWh.

  Ez **méréssel eldöntött** kérdés, nem feltevés: a `+A/-A` sorok napjai a helyesek — a betáplálás (-A) napi összege a rendszer saját napelem-termelésével (független eszköz, a Home Assistant saját órájával) azonos napon **r = +0,944** (123 nap, 2026-05-26 … 2026-09-30), egy nappal eltolva csak **r ≈ +0,43**. Az import a +A/-A sorokat használja, a regisztereket kizárólag diagnosztikára (`raw_meter_register` attribútum, `_last_register`), így ez a jelenség az import értékeit nem érinti.
* **A kumulatív szint 0-tól indul**, nem a fizikai mérőóra állásától: a görbe a **fogyasztástörténetet** mutatja, nem a mérő regiszterét. Ez szándékos és dokumentált — a `sensor.grid_energy_*` legacy sorozat abszolút szintjéhez képest is eltolva lehet. Az Energy dashboard és a statisztika-görbék a különbségeket (napi/órás fogyasztást) mutatják, amire ennek nincs hatása.
* **A nyers 1.8.0/2.8.0 regiszterek egy nappal korábban vannak címkézve**, mint a hozzájuk tartozó +A/-A sorok (lásd fent a méréssel igazolt eltolást). Az import ezért a +A/-A sorokat használja, a regisztereket kizárólag `raw_meter_register` diagnosztikára — a regiszter naiv párosítása 0,4–10 kWh hibát adna naponta.
* **Az élő entitásállapot `unknown`.** Az Energy felület a statisztika-sorozatot használja, így a görbe és a hozzá tartozó számok helyesek; az entitás aktuális állapota viszont nem egy „most mért” érték, ezért a HA `entity_unavailable` típusú figyelmeztetést mutathat. Ez tudatos: így nem keletkezik párhuzamos, élő statisztika.
* **Az `old_wide` (2025-ös, egy változó/sor) formátum szándékosan nem támogatott.** Abban a formátumban egy időpont négy sorban szerepel (négy változó), ezért a „negyedórás slot” fogalom nem értelmezhető, és naiv beolvasásnál 8 slot lenne 4 helyett. Az ilyen fájlok kizárása explicit, olvasható hibát ad, nem csendes hibás importot.

## Verzió és migráció

A config entry verziója `2.1`. A 1.x entry-t (korábbi telepítés, visszaállított mentés) az
`async_migrate_entry` viszi át: a beállításai változatlanok maradnak, csak a két bootstrap kulcs
(`initial_import`, `initial_export`) kerül bele. A major verziót szándékosan nem emeljük anélkül,
hogy a migráció kész lenne: a Home Assistant a major eltérésnél megköveteli a migrációs
handlert, és enélkül az entry **nem tölt be** (`Migration handler not found`).

## Fejlesztés

```bash
/…/.venv-ha/bin/python -m pytest tests/
```

- `tests/test_core.py` — parser és lánc-logika (szintetikus, formahű exportok)
- `tests/test_excel_history.py` — saját sorozat, 0-bázis, idempotencia, dashboard-metaadat, szolgáltatás-regisztráció
- `tests/test_recorder_history.py` — **valódi HA Recorder** (izolált SQLite): írás, `change` értékek, replay idempotencia, `has_sum`/kWh metaadat, legacy sorozat érintetlensége

A tesztek szintetikus, de formahű exportfájlokat használnak (14 oszlop, +A/-A/1.8.0/2.8.0 sorrend, negyedórás sorok, csak éjfélkor kitöltött regiszterek), és lefedik a fenti szabályokat: csonka farok, rés, félig kitöltött csatorna, hiányzó érték, negatív érték, idempotencia, lánc-ellenőrzés, soros/dátum időbélyeg, óraátállítás, valamint hogy a statisztika-sorokban **szám** áll (a `recorder.import_statistics` `state`/`sum` mezője csak `float`/`int` lehet).

## Licensz

MIT — lásd [LICENSE](LICENSE)
