# Suppliers and price sources

Findings from Phase 0 discovery (2026-09-05). Everything marked **unverified** could
not be checked from the development sandbox: its egress proxy blocks every shop
domain, including KoRo and Amazon, so robots.txt and terms pages could not be fetched
directly. Shipping figures below come from web-search snippets of the shops' own
pages and from third-party summaries, and must be confirmed before they go into
`config/suppliers.yaml`.

**Action for you:** run `python scripts/check_robots.py` from your machine and paste
the table into the "robots.txt results" section at the bottom. Open each shop's AGB
once and tick the ToS column.

## How a shop earns a place on the allow-list

1. Public product pages show prices without login.
2. robots.txt allows the specific product and search paths we would read.
3. The AGB / Nutzungsbedingungen do not forbid automated reading of public pages
   for internal price comparison. If they forbid it, the shop stays usable in
   *manual* mode: you paste the URL and price, the tool does the arithmetic.
4. Reputable: real Impressum, established for years, pays VAT in Germany.

Automated access is always: honest User-Agent (`lelahanoi-procure/<version>`),
at most one request every few seconds per host, at most a handful of pages per run,
cached for the price-age window, no login, no cart, no checkout.

## Local pickup suppliers (no web presence, priced from invoices)

| Supplier | Role | Address (to confirm) | Notes |
|---|---|---|---|
| METRO | cash & carry, backbone for sugar, dairy, dry goods | **open question**: no METRO in Landshut. Nearest are METRO Regensburg, Bajuwarenstraße 11, 93053 Regensburg (≈ 75 km one way by road) and METRO München-Brunnthal (≈ 85 km). Which one do you use? | Catalog prices maintained in Supabase. metro.de prices are behind login for store assortment, so no web adapter. |
| Bäckerei Tafelmaier | local bakery | to confirm | appears in bank rules; probably not relevant to syrups |
| Deli Tadka GmbH | local Asian/Indian wholesale (?) | to confirm | likely the source for Vietnamese specialities today; needs an address for the trip cost |
| Bayerstorfer | ? | to confirm | appears in bank rules |

Trip cost = round-trip distance from Altstadt 369, Landshut × per-km rate (config).
One trip per supplier per run is charged once and shared by every item bought there.

## Online shops

### KoRo (koro.com/de) — allow-list candidate, automated read proposed

- Sells: nuts, dried fruit, sugars, cocoa, matcha, syrups, superfood powders, bulk packs. The likely source for "KoRo-exact" lines.
- Shipping DE: 3.90 EUR, free from 69 EUR, no minimum order (from koro's shipping page via search snippet, **unverified**).
- Prices: public, gross, per pack; per-kg price shown on product pages.
- robots.txt: **unverified**. Terms: AGB found for consumer orders; nothing seen that addresses automated reading, **unverified**.
- Note: KoRo also runs a B2B channel with different prices. Do you have a business account there? If so its prices are login-only and become a manual source.

### Amazon.de — manual by default, optional logged-out single-page fetch

- Conditions of Use (Nutzungsbedingungen) forbid "data mining, robots, or similar data
  gathering and extraction tools to extract for re-utilisation any substantial parts"
  and building a database of "prices and product listings" without written consent.
  Reading a handful of product pages per run for internal comparison is a small use,
  but our `price_observations` table is literally a price database, so the
  conservative reading is: **no automated Amazon fetching by default.**
- Proposed mode: `manual`. You paste an Amazon URL and the observed gross price into
  the list (or the tool's offer overrides file); the engine treats it like any offer.
  A `logged_out_fetch` mode stays available behind an explicit config flag for you to
  enable if you decide the risk is acceptable; it fetches only `/dp/<ASIN>` pages you
  name, never search, never an account.
- Amazon Business account: never touched, per hard constraint 3.

### Bremer Gewürzhandel (bremer-gewuerzhandel.de) — allow-list candidate

- Owner-run spice mail order, Bremen, since 2009; 250+ spices, vanilla, dried fruit, tea.
- Shipping DE: 4.99 EUR, free from 49 EUR (**unverified**, search snippet).
- Useful for: cinnamon, star anise, cardamom, vanilla, dried citrus for syrups.

### Pati-Versand (pati-versand.de) — allow-list candidate, high threshold

- Patisserie supplies: aroma pastes, extracts, sugars, glucose, citric acid.
- Shipping DE: GLS 9.99 EUR, free from 150 EUR; DHL 15.99 EUR, free from 300 EUR
  (from their Zahlung & Versand page via snippet, **unverified**). The thresholds mean
  it rarely wins on landed cost for small baskets, which is exactly what the engine
  should show.

### Vietnamese and Asian specialities (pick one or two)

| Shop | Since / where | Shipping DE (unverified) | Fit |
|---|---|---|---|
| asiafoodland.de | Hagen, since 2004 | free from 60.01 EUR; staggered below | broad Vietnamese range (pandan, lotus, coconut, condensed milk) |
| asia4friends.de | ? | 5.99 < 39 EUR, 3.99 < 59 EUR, free from 59 EUR, no minimum, Kauf auf Rechnung | dedicated "Vietnamesische Welt" |
| villagefoods.de | ? | not found | Vietnamese collection; Shopify shop |
| nagofa.de | ? | not found | Vietnamese focus |

Recommendation: allow-list **asiafoodland.de** and **asia4friends.de**, keep the others
as manual. You may already buy these at Deli Tadka; the invoice history will tell.

## Proposed allow-list for `config/suppliers.yaml`

```yaml
suppliers:
  metro:            {kind: pickup, address: "<confirm>", sources: [invoices, catalog]}
  deli_tadka:       {kind: pickup, address: "<confirm>", sources: [invoices]}
  koro:             {kind: online, host: www.koro.com, mode: fetch, shipping: 3.90, free_from: 69.00, tos_checked: null}
  amazon:           {kind: online, host: www.amazon.de, mode: manual, tos_checked: null}
  bremer_gewuerz:   {kind: online, host: www.bremer-gewuerzhandel.de, mode: fetch, shipping: 4.99, free_from: 49.00, tos_checked: null}
  pati_versand:     {kind: online, host: www.pati-versand.de, mode: fetch, shipping: 9.99, free_from: 150.00, tos_checked: null}
  asiafoodland:     {kind: online, host: www.asiafoodland.de, mode: fetch, shipping: null, free_from: 60.01, tos_checked: null}
  asia4friends:     {kind: online, host: asia4friends.de, mode: fetch, shipping: 5.99, free_from: 59.00, tos_checked: null}
```

`mode: fetch` only takes effect once `tos_checked` carries a date and the robots
table below shows the paths as allowed. Until then every shop behaves as `manual`.

## robots.txt results

Not yet run (sandbox egress blocked). Paste the output of `scripts/check_robots.py` here.

| host | robots.txt | path | allowed | checked |
|---|---|---|---|---|
| | | | | |

## ToS review log

| shop | AGB URL | automated reading of public pages | reviewed by | date |
|---|---|---|---|---|
| KoRo | https://www.koro.com/de/agb | not yet reviewed | | |
| Amazon.de | https://www.amazon.de/gp/help/customer/display.html?nodeId=GLSBYFE9MGKKQXXM | **forbidden for data mining / price databases** → manual | Claude (search snippet) | 2026-09-05 |
| Bremer Gewürzhandel | https://www.bremer-gewuerzhandel.de/agb | not yet reviewed | | |
| Pati-Versand | https://www.pati-versand.de/agb | not yet reviewed | | |
| asiafoodland | https://www.asiafoodland.de/cms/agb.html | not yet reviewed | | |
| asia4friends | https://asia4friends.de/agb | not yet reviewed | | |

## Open questions for you

1. Which METRO store do you drive to, and what per-km rate should the trip cost use?
2. Addresses of Deli Tadka and any other local supplier you want modelled as pickup.
3. Confirm the allow-list above, or strike shops you do not want to buy from.
4. Amazon: keep it manual (recommended), or enable the logged-out single-page fetch?
5. Do you have a KoRo B2B account with different prices?
