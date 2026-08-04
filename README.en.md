<p align="center">
  <img src="logo.png" width="120" alt="Battery Orchestrator">
</p>

<h1 align="center">Battery Orchestrator</h1>

<p align="center">
  Adaptive charge/discharge for home batteries — driven by electricity price,<br>
  solar production and real consumption. No black boxes.
</p>

<p align="center">
  <img alt="Home Assistant Add-on" src="https://img.shields.io/badge/Home%20Assistant-Add--on-8b5cf6?style=flat-square&labelColor=0b0a16">
  <img alt="Deterministic" src="https://img.shields.io/badge/planner-deterministic-22d3ee?style=flat-square&labelColor=0b0a16">
  <img alt="No black boxes" src="https://img.shields.io/badge/no%20black%20boxes-eae8f7?style=flat-square&labelColor=0b0a16">
</p>

<p align="center">
  🇬🇧 English · <a href="README.md">🇪🇸 Leer en español</a>
</p>

---

<p align="center">
  <img src="screenshots/estado-actual.png" alt="Current status tab: aggregate SOC, tariff tier, accumulated savings and countdown to the next peak period" width="100%">
</p>
<p align="center"><em>Sample data — not from a real installation.</em></p>

A Home Assistant add-on that plans and executes your home batteries'
charge/discharge every minute, live against your real installation.
No Node-RED, no EMHASS: a purpose-built, deterministic engine you can
read end to end, plus a web interface where you declare every battery,
price and sensor yourself — nothing comes preloaded or hidden.

## Why it exists

The usual solutions (EMHASS, generic linear programming) solve the
problem well but hide the logic behind parameters that are hard to
reason about and a solver that doesn't explain its decisions. Battery
Orchestrator does the opposite: a two-pass algorithm you can read in
full, where every hourly decision comes with its reason in plain text
("charging off-peak to cover the next peak period", "blocked: full and
solar surplus available"...).

## What it does

- **Plans** hour by hour by combining tariff (fixed peak/mid-peak/off-peak
  or dynamic PVPC), solar forecast (HA sensor or Forecast.Solar API) and
  real consumption reconstructed from your own installation's history —
  no opaque machine learning.
- **Shares the charge** across all your batteries proportionally to their
  real capacity, and lets each one self-manage while discharging (with
  the correct power limit in each case: max unless it's full and there's
  still sun, in which case 0W so it doesn't self-drain).
- **Respects your limits**: max/min SOC per battery, contracted power,
  energy reserve for the future peak period even if that means an
  emergency mid-peak charge.
- **Estimates each battery's real health** by observing how much energy
  it takes to move its SOC a large chunk, and comparing that against the
  capacity you declared — not a blind cycle counter.
- **Calculates real accumulated savings**, comparing what you've actually
  paid against what you would have paid without a battery, hour by hour.
- **Flags anomalous consumption**: if real consumption spikes well above
  what's expected and stays that way for several cycles, it's flagged in
  the interface and a Home Assistant notification is sent — always with
  the full detail visible, never just a bare alert.
- **Configurable priority**: savings (default), pure solar
  self-consumption (never charges from the grid), or battery longevity
  (never exceeds 90% SOC).
- **Deferrable loads**: washing machine, dishwasher, electric water
  heater... any appliance with a controllable switch/plug. You choose the
  frequency (one-off, daily, or several times a day, optionally limited to
  specific days of the week) and whether it can be interrupted mid-way or
  not; the app decides on its own the best time to run it — with solar
  surplus, or failing that the cheapest hour available — without
  triggering false anomalous-consumption alerts.
- **Live status**: SOC, solar and consumption refresh every 5 seconds
  reading straight from Home Assistant, without waiting for the next full
  optimization cycle.
- **Read-only panel (wallpanel)**: besides Ingress, its own port for
  pinning the panel on a wall-mounted tablet (WallPanel, Fully Kiosk...)
  without going through Home Assistant's login — no access to the
  configuration at all, blocked server-side too, not just hidden in the UI.
- **Everything configurable from the web UI**: batteries, tariff, solar
  panels, consumption sensor — nothing hardcoded except the base URL of
  the free Forecast.Solar API. Configuration is exportable/importable as
  a file, in case you reinstall the add-on.

## Screenshots

<table>
<tr>
<td width="50%"><img src="screenshots/prevision.png" alt="Forecast SOC chart through the day"></td>
<td width="50%"><img src="screenshots/salud-bateria.png" alt="Battery health tab: estimated real capacity vs. declared"></td>
</tr>
</table>

More screenshots (settings, anomalous consumption alert) in [DOCS.en.md](DOCS.en.md).

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ →
   Repositories**, and add:
   ```
   https://github.com/neoalarrode/EF-HA-Orchestrator
   ```
2. Find "Battery Orchestrator" in the store, install it and start it.
3. Open it from the sidebar (it uses Ingress, no port is exposed).

Step-by-step setup instructions in [DOCS.en.md](DOCS.en.md).

## Project status

Actively used and developed — see [CHANGELOG.md](CHANGELOG.md). Always
starts in simulation mode: you'll see exactly what the add-on would do
without touching your real batteries, until you trust its decisions.
