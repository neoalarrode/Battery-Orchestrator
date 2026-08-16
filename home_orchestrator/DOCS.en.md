<p align="center">
  <img src="logo.png" width="72" alt="Home Orchestrator">
</p>

<h1 align="center">Home Orchestrator — Energy — documentation</h1>

<p align="center"><em>This guide covers the Energy plugin. Climate, Lighting, Tuya, TP-Link and
Starlink are configured from their own page after installing them — see <a href="README.en.md#plugins">README.en.md</a>.</em></p>

<p align="center">
  🇬🇧 English · <a href="DOCS.md">🇪🇸 Leer en español</a>
</p>

<p align="center">
  <a href="#what-it-does">What it does</a> ·
  <a href="#getting-started">Getting started</a> ·
  <a href="#installation-type-per-panelstring">Installation type</a> ·
  <a href="#deferrable-loads">Deferrable loads</a> ·
  <a href="#read-only-panel-wallpanel">Read-only panel</a> ·
  <a href="#the-tabs">The tabs</a> ·
  <a href="#battery-health-how-its-calculated">Battery health</a> ·
  <a href="#savings-and-consumption-alerts">Savings and alerts</a> ·
  <a href="#priority-savings-self-consumption-or-longevity">Priority</a> ·
  <a href="#safety-notes">Safety notes</a>
</p>

---

*Screenshots on this page are from a demo with sample data, not a real installation.*

## What it does

Every cycle (configurable, every 60s by default):

1. Works out the electricity price for the coming hours — fixed tariff
   (<img alt="off-peak" src="https://img.shields.io/badge/-off--peak-34d399?style=flat-square">
   <img alt="mid-peak" src="https://img.shields.io/badge/-mid--peak-fbbf24?style=flat-square">
   <img alt="peak" src="https://img.shields.io/badge/-peak-fb7185?style=flat-square">)
   or dynamic PVPC via an HA sensor, where tiers are worked out automatically by price terciles of the day.
2. Adds up the solar forecast from every panel/array you declare, correcting the current hour with real measured generation if you have a sensor configured.
3. Calculates the home's expected consumption from real history (average for that hour of day over the last N days).
4. Decides whether to charge or discharge, with this priority (adjustable, see [Priority](#priority-savings-self-consumption-or-longevity)):
   - Always charge when there's solar surplus.
   - Charge off-peak just enough to cover the nearest peak period (skipped in "Solar self-consumption" mode).
   - If that's not enough (the forecast peak-period need exceeds what could be charged off-peak), also charge during mid-peak — "emergency charging" — rather than risk falling short (also skipped in "Solar self-consumption").
   - Discharge during peak hours first; during mid-peak only with the surplus left over once what's needed for the rest of the day's peak periods is reserved.
   - Also discharge during off-peak hours, but only with the surplus above that same reserve — typical after a sunny day with a good forecast for the next one: instead of buying from the grid overnight (even if cheap) or just leaving the battery full, it uses up the surplus and frees up room so tomorrow's sun isn't wasted. It never touches the reserve.
5. Shares the charge power across your batteries proportionally to their declared real capacity (a full battery gets 0W, the rest share what's left). Discharge is NOT shared — each battery self-manages — but the discharge power limit for each one is still set: the max you declared, unless it's full and there's still solar surplus, in which case it's set to 0W so it doesn't self-discharge needlessly.
6. Decides the window for each declared deferrable load (washing machine, water heater...) using that same hour-by-hour plan, and turns its switch on or off depending on whether "now" falls inside that window — see [Deferrable loads](#deferrable-loads).
7. Applies the decision to Home Assistant (or just logs it, in simulation mode) and updates the day's history and each battery's health observations.

None of this uses linear programming or machine learning: it's code you
can read end to end, and every hour of the plan carries its reason in
plain text.

## Getting started

1. Install the add-on and open it (it appears in the sidebar thanks to Ingress).
2. **Start in simulation mode** (enabled by default in "General" → "Settings" tab): in the "Current status" tab you'll see exactly what it WOULD do, without touching anything real.
3. In "Settings → Batteries", add each one: name, real capacity in Wh, its SOC (%) sensor, the charge switch and the discharge switch, max charge/discharge power and the min/max SOC you want respected. If your battery exposes `number` entities to limit charge/discharge power, declare those too (optional but recommended — without them the app can only switch on/off without precisely controlling power). The "Power sensor" (optional) has three shapes: none, two separate sensors (a discharge one, always positive, and optionally a charge one) or one single combined signed sensor (positive while charging, negative while discharging — the typical "battery power" many inverters expose). With either option giving a charge reading, the "Energy flow right now" widget can show live how much energy is going into the battery and whether it's coming from solar surplus or the grid — without either, you'll only see the last command sent. The discharge one (if you declare it) is also used for the real-consumption calculation and to estimate health.
4. Configure the tariff in "Settings → Electricity tariff": fixed (enter your peak/mid-peak/off-peak prices and hours) or PVPC (point it at your HA sensor — tiers are worked out automatically by price terciles of the day).
5. Add your solar panels in "Settings → Solar forecast": via an HA sensor that already publishes a forecast, or directly through the Forecast.Solar API (you'll need lat/lon/tilt/azimuth/kWp for your installation; the API key is optional, empty = free plan). If you have an instantaneous-generation sensor for THAT panel/string, declare it in the same form — it corrects the current hour for that panel with the real reading instead of relying only on the forecast. If you have several strings/roofs, each with its own sensor, there's no need to create an aggregated sensor in Home Assistant: declare each one separately and the app sums them itself, both forecast and real generation. Also set the **installation type** for each panel (see below).
6. Real home consumption, in "Settings → Home consumption": point it at a sensor that **already subtracts the batteries' AC charging** (for example an "instantaneous consumption" sensor from your installation) — **not** a raw grid meter that does include it. The app automatically adds, hour by hour, the solar production and each battery's discharge (the sensors from step 3) to reconstruct full real consumption, whatever is covering it at each moment. No signed sensor or charge sensor is needed: the charge terms cancel out mathematically by starting from a sensor that already subtracts them.
7. If you have contracted power, enter it in "Settings → Safety & limits" so grid charging never exceeds it (charging with solar surplus doesn't count, it doesn't draw from the grid).
8. If you have appliances on a controllable plug (washing machine, dishwasher, water heater...) that can wait for whatever time suits best, declare them in "Settings → Deferrable loads" — see [Deferrable loads](#deferrable-loads).
9. Click "Run cycle now" in "Current status" and check the day's plan and the SOC chart in the "Forecast" tab.
10. Choose your priority mode in "Settings → Priority" if the default behavior ("Savings") isn't what you want — see [Priority](#priority-savings-self-consumption-or-longevity).
11. Once you trust the decisions, turn off simulation mode.
12. Download a backup of your configuration from "Settings → Backup" — useful if you ever reinstall the add-on.

## EcoFlow batteries

If you have an EcoFlow battery (STREAM family), no Home Assistant sensor or switch needs to be declared: the app manages it directly through the EcoFlow Cloud API.

1. Create a developer account at [developer-eu.ecoflow.com](https://developer-eu.ecoflow.com) and generate an Access Key and a Secret Key.
2. In "Settings → EcoFlow batteries", paste both keys and click "Save".
3. Click "Search EcoFlow batteries" — every device visible with that account shows up. Click "Add as battery" on the one you want to manage: the "+ Add battery" form opens already set to "EcoFlow" as the source and linked to that device — you only need to fill in the real capacity (Wh) and, if you want, adjust the power/SOC limits, same as with any other battery.
4. Everything else behaves exactly like a Home-Assistant-declared battery: same capacity-proportional charge sharing, same simulation mode, same health estimate.

**Technical note**: charge/discharge control uses the same "scheduled task" model as the official EcoFlow app (enable/disable, power limit, target SOC) — a command EcoFlow doesn't document in its public API but has been verified to work reliably. If you have several linked EcoFlow units (a BKW system), commands are always sent to the group's "main" device, which the app resolves on its own.

**Panels wired directly to the battery (MPPT ports)**: if your EcoFlow battery is in Bluetooth or Hybrid mode (with the [BLE Bridge](https://github.com/neoalarrode/Battery-Orchestrator-BLE-Bridge) v0.2.2+ installed), you can register its MPPT ports as solar panels from **Settings → Solar → "+ Add solar panel / array" → Source: "MPPT port of an EcoFlow battery"**: pick the battery, click "Search MPPT ports" and add whichever port(s) you want (1 to 4 depending on the model — Max, Ultra, Pro, AC Pro, Microinverter... each with however many it has). Since each port is added separately, a single battery with panels from different zones or orientations can have several panels declared. No Home Assistant sensor needed — the instantaneous power is read straight from the bridge, and they're automatically flagged as "directly connected to battery" (see "Installation type per panel/string" below) — deducted from what the app requests over AC from the rest of the batteries, so it isn't double-counted.

## Installation type per panel/string

The installation type is declared on each **solar panel/array**, not on the battery — because the same installation can have both types of panels at once (e.g. one string wired directly into a battery and another feeding a separate self-consumption installation). Each panel is one of two types:

- **Self-consumption (AC) installation** — this panel/string is NOT directly connected to any battery. For a battery to make use of its surplus, the app has to explicitly turn on charge mode and set the power over AC — this is the default behavior.
- **Directly connected to a battery (integrated inverter)** — this panel/string is wired directly into a battery with a hybrid/integrated inverter. In this case the app does NOT need to turn on any charge mode: the battery already absorbs that surplus on its own, keeping whatever's left over as it regulates its own output. The app automatically subtracts that power from what it requests over AC for the rest of the batteries (to avoid double-counting), and only logs an estimate for history and health — it sends no real command for that part. For charging from the grid (off-peak or mid-peak emergency) and for discharging, the app still sends the explicit command regardless of the panel's type.

Getting the type wrong isn't a big deal: marking a self-consumption panel as "connected to battery" makes the app subtract too much when requesting AC charging (batteries will charge somewhat slower than they could); marking a panel that's actually connected to a battery as "self-consumption" makes the app request more AC power than needed (harmless, the battery was already receiving that energy on its own). Check the "Current status" log after the change to confirm it does what you expect.

## Deferrable loads

<p align="center">
  <img src="screenshots/cargas-diferibles.png" alt="Deferrable loads widget in Current status: live state and scheduled window for each load" width="100%">
</p>

Appliances with a controllable switch/plug (washing machine, dishwasher, electric water heater...) that don't need to run at an exact moment, only within a window of the day. Declared in "Settings → Deferrable loads":

- A **switch** the app turns on and off, and optionally a **power sensor (W)** for that same load — with it, the app measures on its own how much energy each activation uses and how long its cycle actually takes, without you having to enter it by hand (though you can give a starting estimate if you want).
- **Frequency**: one-off (a single time, won't repeat until you "reschedule" it from the UI), daily (once a day), or several times a day (configurable count). With daily or several-times-a-day, you can limit it to specific days of the week — e.g. a washing machine only on Mondays and Saturdays.
- **Interruptible or not.** Some loads are fine to cut off mid-way — an electric water heater, for example, just picks up heating again next time it's needed. Others, like a washing machine or dishwasher, must not be interrupted mid-program. Only mark it interruptible in the first case: if it is, the app turns it off early if the expected solar surplus that justified the window disappears for several cycles in a row; if it isn't, it stays on for its whole window no matter what, and the window grows on its own if history shows its cycle takes longer than configured.

**How it decides when to run it:** for each activation, the app first looks for the hour (or block of hours, if it needs more than one) with the most forecast solar surplus that's enough for it; if no slot has enough surplus, it automatically picks the cheapest available hour instead — you don't choose between a "solar mode" or a "cheap mode," the system decides on its own based on what's available that day.

**Doesn't trigger false anomalous-consumption alerts:** while a deferrable load is on by the app's own decision, its expected consumption is automatically added to the forecast used by the anomaly detector (see [Savings and alerts](#savings-and-consumption-alerts)) — so it doesn't mistake a washing machine it just turned on itself for unusual consumption.

## Read-only panel (wallpanel)

Besides Ingress, the add-on exposes its own port (**8098** by default, configurable like any other add-on port from **Settings → Add-ons → Home Orchestrator — Energy → Network**) so you can reach the panel directly by IP without going through Home Assistant's login — meant for pinning it on a wall-mounted tablet with an app like [WallPanel](https://github.com/thanksmister/wallpanel-android) or Fully Kiosk Browser, pointed at `http://<your-ha-ip>:8098`.

Through that port the panel is **read-only**: "Current status", "Forecast" and "Battery health" show the same live data as always, but the "Settings" tab doesn't appear and neither does the "Run cycle now" button. This isn't just cosmetic — the server itself rejects (with a 403) any attempt to read or change the configuration, add/edit/delete batteries, panels or deferrable loads, or force a cycle, if the request comes in through that port, even if you bypass the UI and call the API directly. The reason is that, unlike Ingress, this port has no Home Assistant login in front of it, so it must not be able to touch anything.

If you're not going to use it, you can disable it by leaving the port empty in the add-on's network settings.

## The tabs

<p align="center">
  <img src="screenshots/estado-actual.png" alt="Current status: aggregate SOC, savings and countdown to the next peak period" width="100%">
</p>

- **Current status** — summary of the most recent cycle: aggregate SOC (with the trend from the last few hours), tariff tier, price, solar, consumption, whether it's charging/discharging, savings accumulated today and in total, countdown to the next tier change and a comparison of today's consumption against the average of recent days. An indicator next to the title shows "Healthy" or "Anomaly" depending on whether unusual consumption has been detected (see [Savings and alerts](#savings-and-consumption-alerts)). Right under the title, the "Live now" line (SOC, solar and consumption) refreshes on its own every 5 seconds reading straight from Home Assistant — no need to wait for the next full optimization cycle (which is slower and only repeats every `cycle_seconds`) to see a fresh number. Below that, the log of what the last run did. Further down: a diagram of the energy flow right now (where the active consumption — home plus battery charging, if any — is coming from and in what proportion, live data refreshed every 5 seconds), a meter showing how much of your contracted power is in use, the breakdown for each individual battery (colored by how far each one sits below what's expected, weighted by its real capacity), the countdown to the next peak-price hour with how much you've banked against what's needed plus last-hour forecast accuracy (whether what happened matches what the plan predicted), and the status of each deferrable load (live and scheduled window, see [Deferrable loads](#deferrable-loads)).

<p align="center">
  <img src="screenshots/prevision.png" alt="Forecast: aggregate SOC chart through the day with tariff bands" width="100%">
</p>

- **Forecast** — a chart of the aggregate SOC of all your batteries through the day (with tariff bands in the background and a line marking "now"), and the full "Day plan" table: from 00:00 to 00:00, combining what already happened today (real history) with what's forecast from now on.
- **Battery health** — see below.

<p align="center">
  <img src="screenshots/configuracion.png" alt="Settings: declared batteries and electricity tariff" width="100%">
</p>

- **Settings** — everything you declare yourself: batteries, tariff, solar, consumption, limits, priority, general settings and backup.

## Battery health: how it's calculated

<p align="center">
  <img src="screenshots/salud-bateria.png" alt="Battery health: estimated real capacity vs. declared, one healthy and one degraded" width="100%">
</p>

Two distinct metrics, from two distinct sources:

- **Estimated health (real vs. declared capacity)** — the one shown large on each card. Every time a battery completes a charge or discharge of at least 8% of SOC in one go, the app measures how much energy that took: `real capacity = energy moved / (Δ SOC % / 100)`. The median of the latest reliable observations is kept, and health is that real capacity divided by what you declared when adding the battery. At least one such large observation is needed for it to show up — if your battery only makes small moves, you'll see a notice instead of a made-up number.
- **Equivalent cycles** — a lifetime count (never resets) of all energy charged + discharged, divided by twice the declared capacity. It's a measure of how much work the battery has done, not how much capacity it has left; shown as context alongside health.

Neither metric is a BMS measurement — there's no way to know the real
state of the cells without one. They're honest estimates: where each
number comes from and how confident it is (the number of observations)
is always explained, no black box.

## Savings and consumption alerts

<p align="center">
  <img src="screenshots/anomalia.png" alt="Current status with an anomalous consumption alert detected" width="100%">
</p>

**Accumulated savings.** Every cycle, what you've actually paid (what you buy from the grid for direct consumption, plus whatever's charged from the grid into the battery) is calculated and compared against what you would have paid without a battery (buying directly from the grid whatever solar didn't cover, each hour at its real price). The difference is the savings; it accumulates by day and in total since the app started keeping count. During grid-charging hours it can briefly go negative — that's normal, that energy is recovered later by avoiding buying at peak price.

**Anomalous consumption alert.** Every cycle, real consumption measured right now is compared against what the historical forecast expected for this hour of day. If real consumption exceeds the forecast by more than 60% **and** the difference is at least 400W (so it doesn't trigger on small consumption baselines), and that's sustained for 3 cycles in a row, the "Current status" indicator switches from "Healthy" to "Anomaly", a box opens below with the detail (since when, real vs. expected consumption, the difference) and a persistent notification is created in Home Assistant. It clears itself (indicator, box and notification) once consumption returns to what's expected for 3 cycles in a row. This only works if you have the consumption sensor configured in "Settings → Home consumption".

## Priority: savings, self-consumption, or longevity

In "Settings → Priority" you choose how the planner decides, between three modes, each a clear rule rather than a fuzzy weight:

- **Savings** (default) — the usual behavior: charges with solar surplus, and also from the grid off-peak (or mid-peak as an emergency if needed) just enough to cover the next peak period.
- **Solar self-consumption** — the battery ONLY charges with solar surplus, never from the grid even if it's cheap. Less potential savings on low-sun days, but zero "artificial" paid charge cycles.
- **Battery longevity** — same as "Savings", but the charge target never exceeds 90% of the configured real max SOC, to reduce the wear of always keeping the battery full.

Additionally, with "Savings" or "Longevity" selected (doesn't apply with "Solar self-consumption", which never charges from the grid), there's a separate switch:

- **Sustained charging** — instead of always charging at full power, deliberate grid charging (off-peak and mid-peak emergency charging) is spread at a sustained power over the hours remaining until the first time the battery will actually be needed (the next hour, whether mid-peak or peak, with forecast consumption above solar — off-peak never discharges, so it doesn't count), with a 20% safety margin in case the forecast is a bit off. Charging slowly and steadily generates less heat and stress than bursts at full power. If time runs short (for example, it enters mid-peak emergency charging with the peak period already close), the same calculation yields a high power on its own — there's no separate "panic" branch, it's the same number with fewer hours to spread it over. Solar-surplus charging isn't affected: it's opportunistic and free, there's no point slowing it down and wasting sun.

## Safety notes

- A battery whose SOC sensor is down is skipped for that entire cycle (no value is made up), and it's listed as skipped in "Current status".
- If a battery reaches its configured max SOC and there's still solar surplus, its discharge limit is set to 0W so it doesn't self-discharge needlessly.
- The charge target respects the real max SOC you've configured per battery (if you set a cap below 100% to extend its lifespan, the peak-period energy reserve takes that into account and never tries to exceed it).
- Contracted power only limits charging from the grid (charging with solar surplus doesn't count, it doesn't draw from the grid).
- The historical consumption/solar forecast automatically retries with shorter windows if your Home Assistant keeps fewer days than requested (by default the `recorder` only keeps 10).
- Accumulated savings and the anomalous-consumption alert need the "Home consumption" sensor configured — without it, neither is calculated nor shown in "Current status".
- Restoring a configuration from a file only checks that it has the expected basic keys (batteries, tariff, solar, general); review the data after importing in case it comes from an older version of the add-on.
- A deferrable load marked as NOT interruptible stays on for its whole scheduled window no matter what, even if the forecast solar surplus disappears — that's the safe default for appliances with a program (washing machine, dishwasher). Only mark it interruptible if it's genuinely fine to cut off mid-way.
- The read-only port (see [Read-only panel](#read-only-panel-wallpanel)) has no login in front of it — anyone with access to your local network can view it (never write, that's blocked server-side). Don't expose it outside your LAN (without a VPN) or forward it to the internet.
