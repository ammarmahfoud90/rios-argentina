# Ríos de Argentina

Every gauged river in Argentina, coloured by whether its level today is
normal for the date — measured against that gauge's own history, which in
places runs to 126 unbroken years of daily readings.

Live map: *(add the Pages URL once it deploys)*

Built on the [INA a5 API](https://alerta.ina.gob.ar/a5/apiUI) for gauge data
and [HydroRIVERS](https://www.hydrosheds.org/products/hydrorivers) for the
river network. Updated daily.

## What the colours mean

Each station's reading is ranked against every reading it has ever recorded
within ±7 days of today's date, across all its years. Below the 5th
percentile is *muy por debajo*; above the 95th is *muy por encima*.

A gauge's colour travels downstream until another gauge takes over, or until
the catchment grows past 1.5× the gauge's own — meaning a major tributary
joined and the flow is no longer what was measured. Upstream, the mirror.
Reaches nobody measures stay grey.

## What it does not claim

This is the part that took the longest, and it is the point of the project.

**Only 2,613 of 64,740 reaches are coloured.** Argentina has roughly 125
usable public gauges for the map's footprint. The US map this was modelled
on has 4,693. Colouring more would mean inventing readings.

**20 stations are shown but colour nothing.** The Paraná below Diamante is a
braided delta and HydroRIVERS carries one centerline through one channel;
the port gauges are on the others — San Nicolás is 10 km from the modelled
main stem, San Pedro 29 km. The Río de la Plata is not a river in
HydroRIVERS at all. Those gauges keep their reading and say why they have
no reach.

**Some readings are withheld.** Five guards run before anything is
published: stale data, a stuck sensor repeating one value, a one-day jump
larger than anything in the record, an impossible value, and a sustained
step. The last one exists because a Corrientes series was reporting 7.01 m
during a documented low-water month — a gauge datum change, not a flood. It
is distinguished from a real flood by duration: a flood recedes, a re-zeroed
gauge does not.

**Regulated reaches are still an open problem.** Canals and gauges below
dams report whatever the operator releases, so "above normal for the date"
is not a statement about weather there.

## Method notes worth knowing

INA publishes the same physical gauge as several series — instantaneous,
daily mean, four-hourly — and separately as a historical archive and as live
telemetry. Joining those is where the errors live. 403 series collapse to
148 places. Where a historical donor and modern telemetry could not be
verified as sharing a gauge datum, the station is demoted rather than
spliced on a guess, because a seasonal offset also absorbs genuine
hydrological change and would erase the 2019–22 bajante.

That bajante is visible in the data as a check on the method: scored against
a pre-2002 baseline, 2020 sits at percentile 13, 2021 at 5, 2022 at 16, with
seven independent main-stem gauges agreeing.

## Running it

    pip install requests pyshp

One-off, in order:

    python3 build_climatology.py --all     # download history, build curves
    python3 repair_climatology.py          # datum seams, collapse siblings
    python3 guard_readings.py              # validate current readings
    python3 prep_network.py --min-order 3  # HydroRIVERS -> network.json
    python3 snap_v2.py --route-max-km 7    # attach gauges to reaches
    python3 propagate.py                   # colour the network

Daily, unattended:

    python3 build_daily.py

The daily job deliberately does not rerun the repair or the snapping. Those
were judgement calls, and a bad automatic re-splice would silently change
what the map claims. Rerun them by hand when the roster changes, and read
the reports before committing.

## Data

- Gauges: Instituto Nacional del Agua, [a5](https://alerta.ina.gob.ar/a5)
- Network: Lehner, B., Grill G. (2013). Global river hydrography and network
  routing. *Hydrological Processes* 27(15), 2171–2186.
