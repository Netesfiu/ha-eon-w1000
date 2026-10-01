# E.ON W1000 Home Assistant integráció

Natív Home Assistant integráció az E.ON W1000 okosmérő adatainak importálásához email/XLSX exportból.

Ha eddig a [ZsBT/hass-w1000-portal](https://github.com/ZsBT/hass-w1000-portal) vagy az [EON-W1000-n8n](https://github.com/Netesfiu/EON-W1000-n8n) megoldást használtad, ez az integráció kiváltja azokat — nincs szükség külső szerverre, n8n-re vagy Dockerre.

## Mit csinál

1. **IMAP-on keresztül** (Gmail, saját email, stb.) letölti az E.ON portálról érkező ütemezett XLSX exportokat
2. Feldolgozza a **15 perces +A/-A** (fogyasztás/betáplálás) és **napi 1.8.0/2.8.0** (mérőállás) adatokat
3. **Órás bontásban** importálja a Home Assistant energia statisztikáiba — ugyanabba a sorozatba (`sensor.grid_energy_import` / `sensor.grid_energy_export`), amit az Energy dashboard használ, így a dashboardot nem kell átállítani
4. Létrehozza a `sensor.eon_w1000_*` entitásokat a legutóbbi import állapotával

## A helyesség szabályai

Ezek nem stílus kérdések, hanem a korábbi importáló ismert hibáinak lezárásai:

* **Horgony-elv.** A kumulatív értéket nem a fájl saját mérőóra-regisztereiből számolja, hanem az **utolsó, a recorderben már meglévő órához** igazítja, és onnantól egész Wh-ban halmozza. Az átfedő rolling ablakok így nem hoznak létre "seam"-et (éjféli törést), és ugyanannak az órának mindig ugyanaz lesz az értéke, akárhány fájlból származik.
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
| `sensor.eon_w1000_grid_import` | Legutóbb importált kumulatív vételezés (kWh) |
| `sensor.eon_w1000_grid_export` | Legutóbbi kumulatív betáplálás (kWh) |
| `sensor.eon_w1000_last_update` | Utolsó postafiók-ellenőrzés |
| `sensor.eon_w1000_last_processing` | Utolsó sikeres import |
| `button.eon_w1000_process_now` | Azonnali postafiók-ellenőrzés |

Az energiaszenzorok szándékosan **nem** `total_increasing` entitások: a statisztikát az integráció a meglévő sorozatba írja, és ha ezek az entitások is statisztikát gyártanának, az egy *második*, párhuzamos sorozatot hozna létre. A hasznos állapot attribútumokban van:

- `status` — `ok` / `no_mail` / `no_data` / `no_anchor` / `parse_error` / `mail_error`
- `last_processing`, `last_window_from`, `last_window_to` — az utolsó sikeres import
- `raw_meter_register` — a fájlban látott nyers mérőállás (1.8.0 / 2.8.0)
- `skipped_hours`, `skipped_detail` — hány órát és miért hagyott ki
- `duplicate_hours` — hány óra hordozott a szokásosnál több negyedórát (lásd Korlátok)
- `last_error` — az utolsó hiba szövege

## Energia felület beállítása

Az energia felület beállításaiban:

- **Hálózati fogyasztás** → `sensor.grid_energy_import`
- **Hálózati visszatáplálás** → `sensor.grid_energy_export`

## Szolgáltatások

- `eon_w1000.process_now` — Azonnali email ellenőrzés és feldolgozás (automatizálásból is hívható)

## Áttérés a korábbi importálóról (n8n)

1. **Állítsd le az n8n workflow-t** (`W1000 Digest data` és a hívó workflow a Gmail triggerrel), hogy ne legyen két író ugyanabba a sorozatba.
2. HACS → frissítés erre a verzióra → HA újraindítás.
3. **Ne** állítsd át az Energy dashboardot, és **ne** nyúlj az `input_number.grid_import_meter` / `input_number.grid_export_meter` és a `template` szenzorokhoz: a statisztikát az integráció ugyanabba a sorozatba írja.
4. Nyomd meg a **Process export mail now** gombot (vagy várd meg a következő kört).
5. Ellenőrizd a `sensor.eon_w1000_grid_import` attribútumait: `status: ok`, `last_window_to` a legutóbbi lezárt nap 23:00 órája, `skipped_hours: 0`. Az energia felületen az utolsó órák értékének folytonosnak kell lennie (nincs ugrás a csatlakozási ponton).

Ha `status: no_anchor`, akkor az importálandó ablak előtti utolsó óra nincs meg a recorderben — ilyenkor kézi feltöltés (backfill) kell; az integráció szándékosan nem esik vissza a nyers mérőóra-regiszterre.

## Korlátok

* **Őszi óraátállítás.** A helyi idő szerint címkézett fájlban az óraátállítás napján a 02:00 óra kétszer szerepel, így abban a vödörben két fizikai óra energiája lesz. Az energia nem vész el (minden negyedóra beleszámít), de az az egy nap órás bontása eltolódik; az integráció ezt `duplicate_hours`-ként jelzi.
* **A fájl 1.8.0/2.8.0 regiszterei és a +A/-A sorok napjai.** A valós exportokban a mérőóra-regiszter csak minden nap 00:00 sorában van kitöltve, és a **regiszter-oszlop** naptári napjai egy nappal eltérnek a +A/-A sorok címkéitől: a `D` nap 00:00 sorában álló mérőállás valójában a `D` nap **záró** értéke (azaz a `D+1` 00:00-kor mért állás). Ezért a napi fogyasztás `R(D) − R(D−1)`, és nem `R(D+1) − R(D)`; közvetlenül párosítva 0,4–10 kWh eltérés adódik, a helyes párosítással 0,001 kWh.

  Ez **méréssel eldöntött** kérdés, nem feltevés: a `+A/-A` sorok napjai a helyesek — a betáplálás (-A) napi összege a rendszer saját napelem-termelésével (független eszköz, a Home Assistant saját órájával) azonos napon **r = +0,944** (123 nap, 2026-05-26 … 2026-09-30), egy nappal eltolva csak **r ≈ +0,43**. Az import a +A/-A sorokat használja, a regisztereket kizárólag diagnosztikára (`raw_meter_register` attribútum, `_last_register`), így ez a jelenség az import értékeit nem érinti.
* A kumulatív szint a recorderben lévő előző órához igazodik, ezért a fizikai mérőóra-álláshoz képest állandó eltolással állhat (a dashboard a különbségeket mutatja, amikre ez nincs hatással).

## Fejlesztés

```bash
python tests/test_core.py     # vagy: pytest tests/
```

A tesztek szintetikus, de formahű exportfájlokat használnak (14 oszlop, +A/-A/1.8.0/2.8.0 sorrend, negyedórás sorok, csak éjfélkor kitöltött regiszterek), és lefedik a fenti szabályokat: csonka farok, rés, félig kitöltött csatorna, hiányzó érték, negatív érték, idempotencia, lánc-ellenőrzés, soros/dátum időbélyeg, óraátállítás.

## Licensz

MIT — lásd [LICENSE](LICENSE)
