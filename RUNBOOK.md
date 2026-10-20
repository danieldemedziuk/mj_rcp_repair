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
