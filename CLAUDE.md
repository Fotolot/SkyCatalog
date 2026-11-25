# Instrukcje dla Claude – projekt SkyCatalog

## Wybór wersji aplikacji

Po zakończeniu uzgodnionej partii prac zapytaj mnie, jaką wersję aplikacji wybrać.

1. Odczytaj aktualną wersję z plików projektu. Jeśli istnieją różne numery wersji, wskaż rozbieżności.

2. Przeanalizuj wykonane zmiany i zaproponuj wersję zgodnie z SemVer:
   - PATCH, np. 1.1.1 → 1.1.2: poprawki błędów bez utraty zgodności.
   - MINOR, np. 1.1.1 → 1.2.0: nowe funkcje zachowujące zgodność.
   - MAJOR, np. 1.1.1 → 2.0.0: zmiany łamiące zgodność, np. niezgodny format danych lub konfiguracji.

   Sam rozmiar zmian nie uzasadnia zwiększenia MAJOR.

3. Przedstaw propozycję w takim formacie:

   ```
   Aktualna wersja: [numer]
   Wykonane zmiany: [krótkie podsumowanie]
   Sugerowana wersja: [numer i typ]
   Uzasadnienie: [dlaczego ten typ pasuje]

   Jaką wersję wybierasz?
   1. [sugerowany numer] - zalecana
   2. Pozostawienie aktualnej wersji
   3. Własny numer wersji
   ```

4. Poczekaj na moją odpowiedź.

### Gdzie jest wersja (do punktu 1)

- `VERSION` – źródło wersji. `MAJOR.MINOR` (np. `1.2`) oznacza automatyczny numer poprawki
  (najwyższy tag `vMAJOR.MINOR.N` + 1); pełny `MAJOR.MINOR.PATCH` (np. `1.3.0`) jest używany
  dokładnie – tak ustawia się wersję wybraną przez użytkownika.
- Tagi `v*` i wydania GitHub Releases – faktycznie wydane wersje (aktualna = najwyższy tag).
- `narzedzia/wersja.py` – wylicza wersję w CI i zapisuje ją do `sortowanie/_build.py` (plik
  generowany, w `.gitignore`) oraz do metadanych `.exe`.
- `sortowanie/__init__.py` – `__version__` z `_build.py`, a bez niego z `VERSION` (+ `.0`).
- README i opisy PR mogą wymieniać numery wersji – sprawdź je pod kątem rozbieżności.
