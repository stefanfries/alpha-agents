# Entry Conditions V2 — Verbesserungsideen aus dem Chat

Status: **Analyse / Konzept** — noch keine Implementierung.
Scope: Screening stage, Trend Detection Policy, Entry Timing, Market Regime, spätere Warrant Selection.

## Ausgangspunkt

Der Chat reflektiert die aktuellen ENTRY-Bedingungen und stellt eine zentrale Frage:

> Sind die heutigen Entry-Regeln wirklich gute Kaufzeitpunkte, oder bestätigen sie nur mehrfach denselben bereits gelaufenen Trend?

Die Antwort ist: Die aktuellen Regeln sind ein brauchbarer Start für Trendbestätigung, aber sie sind für das Ziel "frühe, robuste Trendfolge mit Optionsscheinen" zu starr und teilweise redundant.

Aktuell arbeitet `TrendDetectionPolicyConfig` im Kern mit booleschen NEW-Regeln:

- SuperTrend bullish
- EMA20 rising
- ADX > Mindestwert
- ADX rising
- Price > EMA50
- TQ60 über Schwelle
- TQ20 über Schwelle
- TSI über Schwelle

Diese Regeln werden über `passes_rule_group(...)` als gleichwertige Stimmen gezählt. Dadurch wirkt ein Signal robuster, als es tatsächlich ist, weil mehrere Regeln dieselbe Information aus verschiedenen Blickwinkeln messen.

## Kernproblem: scheinbare statt echte Bestätigung

Viele heutige ENTRY-Kriterien sind nicht unabhängig:

| Regelgruppe | Heutige Regeln | Gemessene Information |
| --- | --- | --- |
| Trendrichtung | SuperTrend bullish, EMA20 rising, Price > EMA50 | Preis liegt in / über einem Aufwärtstrend |
| Trendstärke | ADX > 20, ADX rising | Trend ist stark bzw. wird stärker |
| Momentum / Trendqualität | TQ20, TQ60, TSI | Bewegung hat positive Steigung / Momentum |

Wenn eine Aktie stark steigt, werden oft automatisch mehrere dieser Regeln gleichzeitig grün. Das ist keine echte sechs- bis achtfache Bestätigung, sondern eher zwei bis drei unabhängige Informationsdimensionen.

Das Risiko: Ein ALL- oder hohes `min_true`-System kann frühe Turnarounds verpassen und gleichzeitig späte, bereits überdehnte Trends als scheinbar besonders attraktiv einstufen.

## Zielbild: ENTRY in Dimensionen zerlegen

Statt weitere Einzelindikatoren in dieselbe flache Liste einzubauen, sollte ENTRY V2 die Entscheidung in getrennte Dimensionen aufteilen.

### 1. Trend Score

Frage: Ist ein belastbarer mittelfristiger Aufwärtstrend vorhanden?

Mögliche Komponenten:

- Price > EMA50
- EMA20 > EMA50
- EMA20 rising
- SuperTrend bullish
- optional: weekly trend confirmation

Interpretation: Diese Dimension beantwortet nicht, ob jetzt der perfekte Einstieg ist. Sie sagt nur, ob die Aktie grundsätzlich in das Trendfolge-Universum passt.

### 2. Momentum Score

Frage: Beschleunigt oder trägt die Bewegung noch?

Mögliche Komponenten:

- TQ20
- TQ60
- TSI
- ROC / kurzfristiges Momentum
- relative Stärke gegenüber Benchmark, z. B. Stock-TQ minus Benchmark-TQ

Wichtig: Nicht alle Momentum-Indikatoren sollten ungewichtet als separate Pflichtregeln zählen. Sie messen ähnliche Dinge und sollten eher zu einem Score aggregiert werden.

### 3. Trend Strength Score

Frage: Ist der Trend stark genug, um einen gehebelten Optionsschein zu rechtfertigen?

Mögliche Komponenten:

- ADX über Mindestwert
- ADX slope, idealerweise geglättet oder über 8-10 Bars statt nur 5 Bars
- +DI > -DI
- Abstand +DI zu -DI

Verbesserung gegenüber heute: `ADX > 20` allein ist nicht richtungsbewusst. Ein hoher ADX in einem Abwärtstrend darf nicht wie ein bullisches Signal wirken. Deshalb sollte Directional Movement explizit ergänzt werden.

### 4. Entry Timing Score

Frage: Ist jetzt ein guter Einstieg, oder ist der Trend bereits zu weit gelaufen?

Diese Dimension fehlt dem heutigen System am stärksten.

Mögliche Komponenten:

- ATR-normalisierte Distanz zu EMA20
- ATR-normalisierte Distanz zu EMA50
- Swing-High- / Higher-High-Breakout
- Pullback-and-recovery-Signal
- kurzfristige Momentum-Beschleunigung
- Signalalter seit NEW

Diese Dimension sollte nicht mit BREAK/SELL verwechselt werden. Eine überdehnte Aktie kann weiterhin ein gesunder HOLD sein, aber ein schlechter neuer BUY.

## Early Entry vs Confirmed Entry

Die größte konkrete Verbesserung aus dem Chat ist die Trennung zwischen frühem und bestätigtem Entry.

### Early Entry

Ziel: Kandidaten erkennen, bei denen sich ein neuer Trend gerade entwickelt.

Mögliche Eigenschaften:

- Price > EMA50 oder kurz vor / gerade über EMA50
- EMA20 dreht nach oben
- TQ20 > 0
- Momentum verbessert sich
- +DI kreuzt oder überholt -DI
- ADX steigt
- SuperTrend darf noch neutral oder noch nicht bullish sein

Nutzen: Das System kann frühe Turnaround-Kandidaten erfassen, die eine starre ALL-Regel noch ausschließen würde.

### Confirmed Entry

Ziel: Bereits bestätigte, robuste Trends erfassen.

Mögliche Eigenschaften:

- Price > EMA50
- EMA20 > EMA50
- EMA20 rising
- SuperTrend bullish
- ADX > 20
- ADX rising
- +DI > -DI
- TQ20 > 0
- TQ60 positiv

Nutzen: Das System behält die heutige Stärke der Trendbestätigung, aber klassifiziert solche Signale anders als frühe Entries.

## A-E-Klassifikation für Kaufqualität

Aus den Scores kann eine handlungsnahe Klassifikation entstehen:

| Klasse | Bedeutung | Typische Situation | Handlungsidee |
| --- | --- | --- | --- |
| A Immediate | Trend, Momentum und Timing stimmen | Breakout / frühes Momentum ohne Überdehnung | Entry bevorzugen |
| B Good | Trend bestätigt, Timing akzeptabel | Solider Trend, aber kein perfekter Trigger | Entry möglich |
| C Pullback | Trend intakt, aber kurzfristig überdehnt | Gute Aktie, schlechter Preis | Rücksetzer abwarten |
| D Extended | Trend stark, Distanz zu EMA/ATR sehr groß | Chasing-Risiko hoch | Kein neuer Entry |
| E Too late | Momentum verliert Kraft oder Trend ist spät | Viele Trendfilter grün, Timing schwach | Kein neuer Entry |

Beispielhafte Score-Darstellung:

```text
Trend 82 / Momentum 76 / Timing 91 -> A Immediate
Trend 91 / Momentum 88 / Timing 38 -> C Pullback
Trend 95 / Momentum 92 / Timing 12 -> D Extended
```

Wichtig: Diese Klassifikation sollte zunächst advisory sein. Sie sollte nicht sofort die bestehende NEW/BREAK-State-Machine ersetzen.

## Overextension als priorisierter Baustein

Das bereits vorhandene Dokument `entry-timing-extension-filter-plan.md` passt sehr gut zu den Chat-Ideen. Es sollte als erster praktischer Baustein für Entry Timing umgesetzt werden.

Bevorzugte Formel:

```text
ema20_extension_atr = (close - ema20) / atr20
```

Warum ATR-normalisiert statt Prozentabstand:

- 5 % Abstand bedeuten bei einer ruhigen Aktie etwas anderes als bei einer hochvolatilen Aktie.
- ATR berücksichtigt das normale Bewegungsniveau der Aktie.
- Für Optionsscheine ist ein Einstieg nach einer 2-3 ATR Extension besonders riskant, weil ein normaler Pullback den Optionsschein überproportional trifft.

Empfohlene erste Umsetzung bleibt bewusst klein:

1. `ema20_extension_atr` berechnen.
2. In `SelectionResult` und Screening UI anzeigen.
3. Keine Entry-Blockade in Phase 1.
4. Danach Backtest: Forward Returns nach Extension-Buckets auswerten.

## Breakout / Higher High als besserer Trigger

Ein wichtiger Chat-Punkt ist: Ein gleitender Durchschnitt bestätigt oft spät. Ein Breakout über ein relevantes Swing High kann unmittelbarer sein.

Möglicher Early-Entry-Trigger:

```text
Swing-High-Breakout + steigendes Momentum + +DI verbessert sich -> Early Entry
```

Das passt gut zu den bestehenden Ideen in `trend-detection-improvement-ideas.md`, sollte aber als eigener Plan konkretisiert werden:

- Wie wird ein relevantes Swing High definiert?
- Welcher Lookback wird genutzt?
- Muss der Breakout per Schlusskurs bestätigt sein?
- Wird Volumen als Bestätigung genutzt?
- Wie viele Bars bleibt das Breakout-Signal gültig?

## Marktregime stärker nutzen

Das Market-Regime-System ist bereits implementiert, aber überwiegend advisory. Für ENTRY V2 sollte es nicht die Einzeltitel-Logik ersetzen, sondern die Aggressivität steuern.

Mögliche Regime-Wirkung:

| Regime | Entry-Verhalten |
| --- | --- |
| Green | A/B Entries normal zulassen, Early Entries möglich |
| Yellow | strengere Timing-Anforderungen, Early Entries nur bei sehr starkem Momentum / Breakout |
| Red | neue Entries pausieren oder nur manuell erlauben |

Damit bleibt die Aktienauswahl stock-spezifisch, aber das System jagt keine Long-Signale aggressiv in einem schwachen Gesamtmarkt.

## Optionsschein-Qualität bleibt nachgelagert

Die Chat-Idee einer Layer-Architektur ist sinnvoll:

1. Market Regime
2. Stock Trend
3. Momentum
4. Trend Strength
5. Entry Timing
6. Optionsschein Quality

Wichtig: Der Optionsschein-Filter sollte erst nach der Aktien-Entry-Qualität greifen. Ein sehr guter Optionsschein macht keinen schlechten Aktien-Entry gut. Umgekehrt kann ein A-Entry verworfen werden, wenn kein ausreichend liquider / fair gepreister Optionsschein verfügbar ist.

## ENTRY und BREAK nicht spiegeln

Der Chat betont zu Recht: ENTRY und BREAK sollten nicht spiegelbildlich gebaut werden.

Für ENTRY braucht das System:

- Trendbestätigung
- frühes Momentum
- gutes Timing
- keine starke Überdehnung

Für BREAK braucht das System dagegen:

- klare Verschlechterung der ursprünglichen Trendannahme
- Schutz vor größeren Verlusten
- weniger Fokus auf perfektes Timing

Die bestehende asymmetrische BREAK-Logik mit SuperTrend / EMA50 / trend break ist deshalb grundsätzlich sinnvoll. ENTRY V2 sollte vor allem die Kaufentscheidung verbessern, nicht automatisch die Exit-Logik neu bauen.

## Empfohlene Roadmap

### Phase 1 — Dokumentierte Diagnose und Metrik-Sichtbarkeit

- Dieses Dokument als Konzeptbasis nutzen.
- ATR-normalisierte EMA20-Extension implementieren und sichtbar machen.
- Keine Änderung an NEW/BREAK-Entscheidungen.
- Erste Backtest-Auswertung nach Extension-Buckets vorbereiten.

### Phase 2 — Entry-Timing-Klassifikation advisory

- `NORMAL`, `EXTENDED`, `VERY_EXTENDED` oder direkt A-E als nicht-blockierende Klassifikation anzeigen.
- Bestehende `policy_results` nicht ersetzen.
- UI soll klar unterscheiden: guter Trend vs guter Einstieg jetzt.

### Phase 3 — Early vs Confirmed Entry einführen

- Separate Klassifikation für Early und Confirmed Entry entwerfen.
- SuperTrend für Early Entry nicht zwingend machen.
- +DI / -DI ergänzen.
- Optional Swing-High-Breakout als Early-Trigger planen.

### Phase 4 — Score-Modell statt flachem `min_true`

- Trend Score, Momentum Score und Timing Score berechnen.
- Regeln nicht mehr alle gleich zählen.
- Gewichtung erst nach Backtests festlegen.
- Bestehende Config Keys aus Kompatibilitätsgründen nicht vorschnell entfernen.

### Phase 5 — Regime- und Warrant-aware Entry-Verhalten

- Market Regime beeinflusst Entry-Schwellen.
- Bei Yellow strengere Timing-Anforderungen.
- Bei Red neue Entries pausieren oder deutlich erschweren.
- Optionsschein-Selektion erhält Kontext zur Entry-Klasse, z. B. keine aggressiven Hebel bei C/D.

## Backtest-Fragen vor Implementierung harter Regeln

Bevor Schwellenwerte produktiv blockieren, sollten mindestens diese Fragen historisch getestet werden:

1. Wie entwickeln sich heutige NEW-Signale nach 5/10/20 Tagen je nach `ema20_extension_atr`?
2. Sind Early-Entry-Kandidaten mit noch neutralem SuperTrend profitabler oder nur noisier?
3. Verbessert +DI > -DI die Trefferquote gegenüber ADX allein?
4. Reduziert eine Overextension-Klasse Drawdowns bei Optionsscheinen stärker, als sie Gewinner verpasst?
5. Welche A-E-Klasse hat nach Spread, Slippage und Optionsschein-Auswahl die beste Netto-Performance?
6. Wirkt Market Regime als echter Performance-Filter oder nur als psychologisch plausibler Zusatz?

## Konkrete nächste Entscheidung

Die nächste sinnvolle technische Arbeit ist nicht sofort ein großer Entry Score, sondern ein kleiner, überprüfbarer Schritt:

> Implementiere `ema20_extension_atr` als sichtbare, nicht-blockierende Entry-Timing-Metrik und werte anschließend historische NEW-Signale nach Extension-Buckets aus.

Danach kann entschieden werden, ob daraus eine A-E-Klassifikation, ein Early/Confirmed-Entry-Modell oder ein gewichteter Score entstehen soll.
