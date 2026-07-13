# Health Monitor — Netlify

Pliki do wrzucenia bezpośrednio na Netlify (drag & drop lub CLI).

```
health-netlify/
├── index.html       ← cała aplikacja
├── _redirects       ← routing SPA
├── netlify.toml     ← security headers
└── README.md
```

---

## Krok 1 — Udostępnij Arkusz Google

Arkusz musi być dostępny publicznie (tylko do odczytu), bo klucz API działa tylko z publicznymi zasobami.

1. Otwórz arkusz → Udostępnij (prawy górny róg)
2. Zmień na „Każda osoba mająca link może wyświetlać"
3. Kliknij Gotowe

> **Bezpieczeństwo:** dane są Twoimi danymi zdrowotnymi, ale nie ma w nich nic
> wrażliwego finansowo. Dostęp do linku i tak wymaga znajomości URL arkusza.
> Samą aplikację możesz zabezpieczyć hasłem przez Netlify (patrz krok 4).

---

## Krok 2 — Klucz Google Sheets API

1. Wejdź na https://console.cloud.google.com/
2. Utwórz projekt (np. `health-monitor`)
3. **APIs & Services → Enable APIs** → włącz **Google Sheets API**
4. **APIs & Services → Credentials → Create Credentials → API Key**
5. Skopiuj klucz (zaczyna się od `AIza…`)
6. Kliknij **Edit API key** → ogranicz:
   - **Application restrictions:** HTTP referrers (websites)
   - Dodaj: `https://twoja-domena.netlify.app/*`
   - **API restrictions:** Restrict to → Google Sheets API

---

## Krok 3 — Deploy na Netlify

### Opcja A: przeciągnij i upuść (najszybciej)

1. Wejdź na https://app.netlify.com/drop
2. Przeciągnij folder `health-netlify` na stronę
3. Gotowe — dostaniesz URL np. `https://xxx.netlify.app`

### Opcja B: przez repo (zalecane dla aktualizacji)

```bash
# zainstaluj Netlify CLI jeśli nie masz
npm install -g netlify-cli

cd health-netlify
netlify init      # połącz z kontem / utwórz nowe site
netlify deploy --prod
```

---

## Krok 4 — Hasło dostępu (prywatność)

W panelu Netlify dla Twojego site:
**Site configuration → Access & security → Site protection → Enable password**

Wpisz hasło → tylko osoba z hasłem zobaczy dashboard.

---

## Krok 5 — Pierwsze uruchomienie

1. Otwórz URL z Netlify
2. Pojawi się ekran konfiguracji — wklej klucz API
3. Klucz zapisuje się w `localStorage` przeglądarki (tylko lokalnie)
4. Dashboard ładuje dane i renderuje wykresy

---

## Wskaźnik Recovery — jak działa

| Składnik       | Waga | Logika                                |
|----------------|------|---------------------------------------|
| HRV            | 40%  | dzisiaj / średnia 7d (wyżej = lepiej) |
| HR spoczynkowe | 35%  | średnia 7d / dzisiaj (niżej = lepiej) |
| Sen całkowity  | 25%  | dzisiaj / średnia 7d (więcej = lepiej)|

- **≥ 100** → powyżej Twojej normy — dobry dzień na mocny trening
- **85–99** → w normie — trening umiarkowany OK
- **< 85**  → sygnał zmęczenia — warto odpocząć
