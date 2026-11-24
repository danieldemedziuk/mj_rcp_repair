# Naprawa RCP na produkcji (Docker)

Bezrobocie i autokorekta obciążyły konta pracowników `E-`, czego robić nie
powinny. Moduł `mj_rcp_repair` jest **jednorazowy**: instalujesz, klikasz
**Start naprawy**, odinstalowujesz.

---

## 1. Kopia bazy

Naprawa zapisuje do bazy. Jedyny pełny powrót to zrzut.

```bash
docker compose exec -T db pg_dump -U odoo -Fc <baza> > kopia_$(date +%F_%H%M).dump
```

## 2. Wgranie modułu

Katalog `mj_rcp_repair/` do swojego `dev_addons`, potem:

```bash
docker compose restart odoo
```

W Odoo: *Aplikacje → Aktualizuj listę aplikacji → MJ RCP Repair → Instaluj*.

Moduł wymaga `hr_rcp` ≥ **17.0.0.0.16**, `mj_finance_core` ≥ 17.0.1.2.0,
`sync_hub` ≥ 17.0.9.9.0, `analytic_cost_control` ≥ 17.0.4.18.0,
`hr_extension` ≥ 17.0.0.0.8 i `project_timesheet_control` ≥ 17.0.1.6.0.
Sam to sprawdza i odmawia startu, jeśli są starsze. `hr_rcp` 17.0.0.0.12
blokuje kasowanie zaksięgowanych linii RCP i ich zapisów analitycznych oraz
datuje storno na dzień oryginału — bez tego następne przeliczenie odtworzyłoby
duble, które ta naprawa wycofuje. **Najpierw wdróż `hr_rcp`, dopiero potem
instaluj ten moduł.**

`hr_rcp` księguje przez `queue_job`. Runner kolejki startuje wyłącznie jako
moduł serwerowy — w konfiguracji kontenera musi być:

```ini
server_wide_modules = base,web,queue_job
```

Bez tego zadania księgowania wiszą w stanie `pending` i nic się nie dzieje.

## 3. Naprawa

*Ustawienia → Techniczne → MJ Naprawa RCP*

**Podgląd** — przebieg bez zapisu, pokazuje dokładnie to samo co Start.
Przeczytaj i dopiero potem:

**Start naprawy** — i czekaj.

Nie ma nic do wypełnienia. Automat sam:

- sprawdza wersje modułów,
- przywraca `unproductive` projektom bezrobocia, którym flaga zeszła,
- ustala, które projekty faktycznie mają płacę na kontach `E-`,
- naprawia statusy linii RCP,
- stornuje księgowania płacowe, przedatowując je na daty oryginałów,
- podpina i dotwarza linie lustrzane,
- odbudowuje narzuty A/Bx po stawce nieproduktywnej i księguje je,
- weryfikuje i wypisuje `CLEAN`.

Potem, niezależnie od bezrobocia, **spójność księgi** (raz, dla wszystkich
firm):

- odtwarza brakującą stronę E księgowań, którym ktoś ją skasował,
- stornuje duplikaty po skasowanych liniach RCP — płace i narzuty, które mają
  dokładny odpowiednik (te same konta, data, kwota) w linii istniejącej,
- przedatowuje wszystkie storna na daty oryginałów (z parami),
- podpina karty pracy, które wypadły z zaksięgowanych linii, gdy ich godziny
  dokładnie pokrywają brak,
- ponawia księgowanie linii w błędzie,
- uzgadnia konto E z RCP miesiąc po miesiącu i wypisuje `LEDGER CLEAN`.

Na końcu **analityka** (standard kont MPK, płace po kodzie):

- zakłada brakujące konta funkcyjne osób, których kod `Axxx-E-<kod E>`
  istnieje już w EE (kod dokładnie z EE),
- synchronizacja kosztów MPK z EE od 2026-01-01,
- synchronizacja płac z EE od 2026-01-01 (crony nie pobierają historii,
  którą przekodowano w EE),
- storno ręcznych płac (dokumenty ATD), które synchronizacja teraz
  przynosi z EE — **tylko tych pozycji**, które mają odpowiednik z EE;
  pozycja bez odpowiednika jest jedynym zapisem płacy i zostaje
  (`KEPT`),
- kontrole i `ANALYTICS CLEAN`.

Brak połączenia z EE nie wycofuje naprawy księgi — synchronizacja jest
wtedy oznaczona `SKIPPED` i trzeba ją uruchomić kreatorem.

Każdy krok można bezpiecznie powtórzyć — drugi przebieg niczego nie dubluje.

Idzie po każdej firmie osobno — `hr_rcp` zawęża wyszukiwanie narzutów do
aktywnej firmy, więc pojedynczy przebieg zostawiłby narzuty pozostałych.

Raport pojawia się w oknie. Jeśli coś odmówi, przebieg **wycofuje się w
całości** i pisze dlaczego — nic nie zostaje w połowie.

## 4. Odinstalowanie

*Aplikacje → MJ RCP Repair → Odinstaluj*. Model i pozycja menu znikają,
**korekty zostają** — to jest cel.

---

## Czego automat świadomie NIE robi

**Przeceny zaległych linii.** Linia wyceniona przed oznaczeniem projektu jako
nieproduktywny niesie stawkę produktywną i zostanie odrzucona przy
księgowaniu. Przecena **kasuje** taką linię i buduje ją na nowo, a na bazie
deweloperskiej kwalifikuje się do tego ok. **1 163 linii z lat 2023–2024**.
To decyzja, którą ktoś podejmuje świadomie, a nie efekt uboczny kliknięcia
Start. Raport na końcu podaje, ile ich jest.

Gdy zakres zostanie uzgodniony: zaznacz **„Uruchom kroki ręcznie"**, wybierz
krok 7, wpisz jeden projekt w „Ogranicz do MPK", włącz „Zapisz zmiany".

**Zablokowanych tygodni.** Tydzień RCP przelicza się w całości, a te tygodnie
zawierają linie innych projektów. Projekt w etapie zamkniętym sprawia, że
Odoo odmawia przeliczenia całego dokumentu — automat taki tydzień pomija i
wypisuje, zamiast przerywać przebieg.

**Sierot bez odpowiednika.** Księgowanie skasowanej linii, dla którego nie
ma identycznego zapisu w linii istniejącej, może być jedynym zapisem tych
godzin. Raport wypisuje je jako `LEFT FOR REVIEW` i zostawia.

**Linii, których arkusz nie zgadza się z księgą.** Ilość przepisana na
zaksięgowanej linii przed blokadą edycji (na db_dev: jedna linia). Która
strona ma rację, zależy od kart pracy — raport wypisuje je jako
`LEFT FOR REVIEW`; poprawka to storno linii i linia korygująca.

## Znane ograniczenie

Bezrobocie chronią dwa znaczniki (`mj_is_idle_time` i `unproductive`),
autokorekta tylko ręczne `unproductive`. Odznaczenie go na projekcie
autokorekty przepuści płacę na konto `E-` bez ostrzeżenia i automat tego nie
wykryje. Łatka (kategoria „Poprawki i korekty" jako drugi sygnał) jest
zaprojektowana, nie wdrożona.

---

## RCP 2025 — księgowanie wstecz (od 17.0.1.17.0)

Osobny przycisk, **nie** część „Start naprawy”. Wymaga `sync_hub` ≥ 17.0.9.16.0
(odczyt płac z NAV) i działającego połączenia NAV oraz Enterprise.

1. Kopia bazy (pkt 1).
2. Wgraj `sync_hub` i `mj_rcp_repair`, potem `-u sync_hub,mj_rcp_repair`.
3. *MJ Naprawa RCP* → **Preview RCP 2025** — przeczytaj raport.
4. **Post RCP 2025** — trwa kilkanaście minut, nie zamykaj okna.

Co robi, po kolei:

- narzut B „godziny pracy przy ofertach” w modelach kosztów, który przez
  scalenie 26-0084 (17.11.2026) wskazywał konto osobiste handlowca, wraca na
  konto **A038**;
- dwa wiersze jednej osoby w jednym miesiącu modelu kosztów 2025: zostaje
  ten, którego płacę bazową potwierdza najbliższy późniejszy miesiąc; czego
  nie da się rozstrzygnąć — raport `UNRESOLVED`, nic nie jest kasowane;
- konto `A038-E-PT001-PTOMASZEWSKA` (zarchiwizowane), jeśli go brak;
- płace: **I–VI 2025 z NAV** (konto 550-00, pracownik po kodzie E w wymiarze 1,
  przy innym numerze — po nazwisku), **VII–XII z Enterprise**; opis linii z NAV:
  `NAV <dokument> | <opis NAV> | <kod NAV>`, referencja `NAV 550-00 #<nr zapisu>`;
- MPK `24-0064-T` → `24-0064`;
- każdy tydzień 2025: przecena wg dzisiejszego modelu kosztów miesiąca,
  przeliczenie, księgowanie, blokada. Tydzień, którego nie da się zaksięgować,
  jest wycofywany w całości i wypisany (`NOT POSTED`) — np. grudzień
  Kaźmierczaka (brak wiersza w modelu XII 2025, decyzja: zostaje).

Ponowne uruchomienie jest bezpieczne: płace aktualizowane po numerze zapisu
źródłowego, tygodnie zaksięgowane i zablokowane są pomijane.

**Nie robi:** budżetów projektów (`project_charter`, wiersz 210 „Robocizna”) —
to zdjęcie odświeżane przyciskiem/cronem projektu; po księgowaniu 2025 jest
nieaktualne na projektach z RCP 2025.

---

## Wyczyść okres RCP (od 17.0.1.18.0)

Wymaga `hr_rcp` ≥ 17.0.0.0.35. Pola **Wyczyść od / do**, potem **Podgląd
czyszczenia** i **Wyczyść okres**. Tygodnie wybierane są po dacie dokumentu RCP,
w firmie wybranej w kreatorze.

- odblokowuje tydzień, stornuje linie zaksięgowane **i linie w błędzie z żywymi
  narzutami**, usuwa linie;
- stornuje płace pozostawione przez linie usunięte wcześniej (storno jak w hr_rcp:
  data oryginału, klucz `RCP:<linia>:REVERSE:<zapis>`);
- raport: linie pozostałe w okresie, konta, które się nie zerują, zapisy bez pary.

Linia, której nie dało się wystornować, **nie jest usuwana** — jest wypisana
(`STILL BOOKED`). Tygodnie zostają pustymi szkicami: przelicz, potem zaksięguj.
