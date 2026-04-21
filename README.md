# Battery Optimizer

Battery Optimizer is a Home Assistant custom integration that plans battery charging and discharging from spot electricity prices, current/forecast load, battery limits, efficiency losses, degradation cost, and safety constraints.

It is designed for transparency first: it exposes the plan, projected SOC, decision reasons, charge/discharge windows, and a manual override. It starts in advisory-only mode so you can watch decisions before allowing inverter control.

## What It Does

- Reads today and tomorrow prices from a Nord Pool style price sensor.
- Reads battery SOC, current load, and optional load forecast.
- Computes a rolling 24 to 48 hour plan.
- Respects reserve SOC, preferred max SOC, hard max SOC, charge/discharge power, efficiency, degradation cost, hysteresis, and minimum dwell time.
- Falls back to hold if required data is missing.
- Exposes Home Assistant sensors, select, switch, button, diagnostics, and services.
- Includes a Solarman command backend for your inverter entity layout.
- Keeps the optimization engine backend-agnostic so other inverter backends can be added later.

PV and export tariff are intentionally not included in this first implementation because your system is configured without export or solar optimization.

## Installation

Copy the integration folder into Home Assistant:

```text
custom_components/battery_optimizer
```

Restart Home Assistant, then add it from:

```text
Settings -> Devices & services -> Add integration -> Battery Optimizer
```

Leave `Advisory-only mode` enabled until the sensors and plan look right.

## Required Configuration

You will be asked for:

- Nord Pool price sensor: `sensor.nordpool_kwh_se4_sek_3_10_025`
- Battery SOC sensor: `sensor.inverter_battery`
- Current load sensor: `sensor.inverter_load_power`
- Battery capacity entity or fallback capacity in kWh
- Max charge and discharge power in kW
- Charge and discharge efficiency
- Battery voltage entity or fallback nominal voltage, used to convert Solarman amps into kW and planned kW back into amps
- Tax and grid fees per kWh. Default: `0.773 SEK/kWh`
- Optimizer aggressiveness:
  - `conservative`: higher cycling penalty
  - `balanced`: default
  - `aggressive`: lower cycling penalty for more invoice-focused battery use
- Safety limits:
  - Reserve SOC: default `10%`
  - Preferred max SOC: default `90%`
  - Hard max SOC: default `100%`
  - Main fuse: default `20A`
  - Peak shaving threshold: default `24A`

## Your Solarman Entity Mapping

Recommended starting mapping:

```yaml
price_entity: sensor.nordpool_kwh_se4_sek_3_10_025
battery_soc_entity: sensor.inverter_battery
battery_state_entity: sensor.inverter_battery_state
battery_capacity_entity: sensor.inverter_battery_capacity
battery_voltage_entity: sensor.inverter_battery_voltage
load_power_entity: sensor.inverter_load_power

grid_charging_switch: switch.inverter_battery_grid_charging
grid_charging_current_number: number.inverter_battery_grid_charging_current
max_charging_current_number: number.inverter_battery_max_charging_current
max_discharging_current_number: number.inverter_battery_max_discharging_current

peak_shaving_switch: switch.inverter_grid_peak_shaving
peak_shaving_number: number.inverter_grid_peak_shaving

work_mode_select: select.inverter_work_mode
phase_current_entities: sensor.inverter_external_ct1_current,sensor.inverter_external_ct2_current,sensor.inverter_external_ct3_current
phase_power_entities: sensor.inverter_external_ct1_power,sensor.inverter_external_ct2_power,sensor.inverter_external_ct3_power
phase_voltage_entities: sensor.inverter_grid_l1_voltage,sensor.inverter_grid_l2_voltage,sensor.inverter_grid_l3_voltage
```

The integration assumes your inverter remains in `Zero Export To CT`. It does not currently change `select.inverter_work_mode`.

## Remaining Values To Confirm

Before real control is enabled, confirm these values in the config flow:

- `sensor.inverter_battery_capacity` reports total usable capacity in kWh.
- `sensor.inverter_battery_voltage` reports battery voltage in volts.
- `number.inverter_battery_max_charging_current` and `number.inverter_battery_max_discharging_current` are the actual safety current limits in amps.
- `sensor.inverter_external_ct1_power`, `ct2`, and `ct3` represent live grid import/export per phase. Daily savings uses positive import from these sensors.
- Month-to-date and today cost sensors are backfilled from recorder history when possible. If recorder history or historical price states are missing, they start accumulating from the current runtime.
- The inverter peak shaving number still expects total watts, but Battery Optimizer watches individual phase currents and dynamically lowers that total-watt threshold when any phase reaches the per-phase limit.

## Entities

Created entities include:

- `sensor.battery_optimizer_planned_mode`
- `sensor.battery_optimizer_projected_soc`
- `sensor.battery_optimizer_projected_soc_schedule`
- `sensor.battery_optimizer_projected_soc_today`
- `sensor.battery_optimizer_projected_soc_tomorrow`
- `sensor.battery_optimizer_expected_value`
- `sensor.battery_optimizer_cost_without_battery`
- `sensor.battery_optimizer_cost_with_battery`
- `sensor.battery_optimizer_daily_cost_without_battery`
- `sensor.battery_optimizer_daily_cost_with_battery`
- `sensor.battery_optimizer_daily_savings`
- `sensor.battery_optimizer_daily_energy_without_battery`
- `sensor.battery_optimizer_daily_energy_with_battery`
- `sensor.battery_optimizer_monthly_cost_without_battery`
- `sensor.battery_optimizer_monthly_cost_with_battery`
- `sensor.battery_optimizer_monthly_savings`
- `sensor.battery_optimizer_monthly_energy_without_battery`
- `sensor.battery_optimizer_monthly_energy_with_battery`
- `sensor.battery_optimizer_price_today_comparison`
- `sensor.battery_optimizer_price_tomorrow_comparison`
- `sensor.battery_optimizer_load_forecast`
- `sensor.battery_optimizer_upcoming_charge_hours`
- `sensor.battery_optimizer_upcoming_discharge_hours`
- `sensor.battery_optimizer_cheapest_charge_windows`
- `sensor.battery_optimizer_best_discharge_windows`
- `sensor.battery_optimizer_decision_reasons`
- `sensor.battery_optimizer_last_command`
- `select.battery_optimizer_aggressiveness`
- `select.battery_optimizer_override_mode`
- `switch.battery_optimizer_advisory_only_mode`
- `button.battery_optimizer_apply_current_plan`

The planned mode sensor includes a `plan` attribute with the upcoming intervals.

## Services

```yaml
service: battery_optimizer.set_override
data:
  mode: auto
```

Allowed modes:

- `auto`
- `force_charge`
- `hold`
- `force_discharge`

Apply the current plan manually:

```yaml
service: battery_optimizer.apply_now
```

## Example Dashboard

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Price, SOC, and Plan
    entities:
      - entity: sensor.nordpool_kwh_se4_sek_3_10_025
        name: Current Nord Pool price
      - entity: sensor.inverter_battery
        name: Current SOC
      - entity: sensor.battery_optimizer_projected_soc
        name: Projected SOC next hour
      - entity: sensor.battery_optimizer_projected_soc_today
        name: Projected SOC today
      - entity: sensor.battery_optimizer_projected_soc_tomorrow
        name: Projected SOC tomorrow
      - entity: sensor.battery_optimizer_projected_soc_schedule
        name: Projected SOC schedule
      - entity: sensor.battery_optimizer_planned_mode
        name: Planned mode
      - entity: sensor.battery_optimizer_upcoming_charge_hours
        name: Upcoming charge hours
      - entity: sensor.battery_optimizer_upcoming_discharge_hours
        name: Upcoming discharge hours

  - type: history-graph
    title: Price vs Current and Projected SOC
    hours_to_show: 48
    entities:
      - entity: sensor.nordpool_kwh_se4_sek_3_10_025
        name: Hourly avg price
      - entity: sensor.inverter_battery
        name: Current SOC
      - entity: sensor.battery_optimizer_projected_soc
        name: Projected SOC

  - type: custom:apexcharts-card
    header:
      show: true
      title: Today - Battery SOC and Price
    graph_span: 1d
    span:
      start: day
    now:
      show: true
      label: Now
    yaxis:
      - id: soc
        min: 0
        max: 100
        decimals: 0
      - id: price
        decimals: 3
        opposite: true
    apex_config:
      stroke:
        width: 2
      legend:
        show: true
    series:
      - entity: sensor.battery_optimizer_price_today_comparison
        name: Raw Nord Pool
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.quarter_hours || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_today_comparison
        name: Hourly average
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.hourly_average || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.inverter_battery
        name: Actual SOC
        yaxis_id: soc
        type: line
        curve: smooth
      - entity: sensor.battery_optimizer_projected_soc_today
        name: Projected SOC
        yaxis_id: soc
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.projected_soc || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.projected_soc_percent];
          });

  - type: custom:apexcharts-card
    header:
      show: true
      title: Tomorrow - Battery SOC and Price
    graph_span: 1d
    span:
      start: day
      offset: +1d
    now:
      show: true
      label: Now
    yaxis:
      - id: soc
        min: 0
        max: 100
        decimals: 0
      - id: price
        decimals: 3
        opposite: true
    apex_config:
      stroke:
        width: 2
      legend:
        show: true
    series:
      - entity: sensor.battery_optimizer_price_tomorrow_comparison
        name: Raw Nord Pool
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.quarter_hours || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_tomorrow_comparison
        name: Hourly average
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.hourly_average || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_projected_soc_tomorrow
        name: Projected SOC
        yaxis_id: soc
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.projected_soc || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.projected_soc_percent];
          });

  # Requires ApexCharts Card from HACS.
  # This plots future Nord Pool attributes, which the built-in history graph cannot do.
  - type: custom:apexcharts-card
    header:
      show: true
      title: Today - Price vs Projected SOC
    graph_span: 1d
    span:
      start: day
    now:
      show: true
      label: Now
    yaxis:
      - id: price
        decimals: 3
      - id: soc
        min: 0
        max: 100
        opposite: true
    apex_config:
      stroke:
        width: 2
      legend:
        show: true
    series:
      - entity: sensor.battery_optimizer_price_today_comparison
        name: Today raw Nord Pool
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.quarter_hours || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_today_comparison
        name: Today hourly average
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.hourly_average || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_today_comparison
        name: Projected SOC
        yaxis_id: soc
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.projected_soc || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.projected_soc_percent];
          });

  - type: custom:apexcharts-card
    header:
      show: true
      title: Tomorrow - Price vs Projected SOC
    graph_span: 1d
    span:
      start: day
      offset: +1d
    now:
      show: true
      label: Now
    yaxis:
      - id: price
        decimals: 3
      - id: soc
        min: 0
        max: 100
        opposite: true
    apex_config:
      stroke:
        width: 2
      legend:
        show: true
    series:
      - entity: sensor.battery_optimizer_price_tomorrow_comparison
        name: Tomorrow raw Nord Pool
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.quarter_hours || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_tomorrow_comparison
        name: Tomorrow hourly average
        yaxis_id: price
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.hourly_average || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.price];
          });
      - entity: sensor.battery_optimizer_price_tomorrow_comparison
        name: Projected SOC
        yaxis_id: soc
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.projected_soc || [];
          return points.map((point) => {
            return [new Date(point.time).getTime(), point.projected_soc_percent];
          });

  - type: entities
    title: Daily Savings So Far
    entities:
      - entity: sensor.battery_optimizer_daily_cost_without_battery
        name: Today's cost without battery
      - entity: sensor.battery_optimizer_daily_cost_with_battery
        name: Today's cost with battery
      - entity: sensor.battery_optimizer_daily_savings
        name: Today's savings
      - entity: sensor.battery_optimizer_daily_energy_without_battery
        name: Today's energy without battery
      - entity: sensor.battery_optimizer_daily_energy_with_battery
        name: Today's grid energy with battery

  - type: entities
    title: Month-To-Date Invoice Estimate
    entities:
      - entity: sensor.battery_optimizer_monthly_cost_without_battery
        name: Month cost without battery
      - entity: sensor.battery_optimizer_monthly_cost_with_battery
        name: Month cost with battery
      - entity: sensor.battery_optimizer_monthly_savings
        name: Month savings
      - entity: sensor.battery_optimizer_monthly_energy_without_battery
        name: Month energy without battery
      - entity: sensor.battery_optimizer_monthly_energy_with_battery
        name: Month grid energy with battery

  - type: entities
    title: Load Forecast
    entities:
      - entity: sensor.battery_optimizer_load_forecast
        name: Next forecast load
      - entity: sensor.inverter_load_power
        name: Current load

  - type: custom:apexcharts-card
    header:
      show: true
      title: Today - Load Forecast vs Actual
    graph_span: 1d
    span:
      start: day
    now:
      show: true
      label: Now
    yaxis:
      - min: 0
        decimals: 2
    apex_config:
      stroke:
        width: 2
      legend:
        show: true
    series:
      - entity: sensor.inverter_load_power
        name: Actual load
        type: line
        curve: smooth
        transform: |
          return x > 50 ? x / 1000 : x;
      - entity: sensor.battery_optimizer_load_forecast
        name: Forecast load
        type: line
        curve: stepline
        data_generator: |
          const points = entity?.attributes?.forecast || [];
          const startOfToday = new Date();
          startOfToday.setHours(0, 0, 0, 0);
          return points
            .filter((point) => new Date(point.time) >= startOfToday)
            .map((point) => {
              return [new Date(point.time).getTime(), point.load_kw];
            });

  - type: entities
    title: Projected Coming Window
    entities:
      - entity: sensor.battery_optimizer_cost_without_battery
        name: Projected cost without battery
      - entity: sensor.battery_optimizer_cost_with_battery
        name: Projected cost with battery
      - entity: sensor.battery_optimizer_expected_value
        name: Projected savings

  - type: entities
    title: Battery Optimizer
    entities:
      - entity: sensor.battery_optimizer_planned_mode
      - entity: sensor.battery_optimizer_projected_soc
      - entity: sensor.battery_optimizer_expected_value
      - entity: sensor.battery_optimizer_upcoming_charge_hours
      - entity: sensor.battery_optimizer_upcoming_discharge_hours
      - entity: sensor.battery_optimizer_decision_reasons
      - entity: select.battery_optimizer_aggressiveness
      - entity: select.battery_optimizer_override_mode
      - entity: switch.battery_optimizer_advisory_only_mode
      - entity: button.battery_optimizer_apply_current_plan

  - type: entities
    title: Optimizer Controls
    entities:
      - entity: select.battery_optimizer_aggressiveness
        name: Aggressiveness
      - entity: select.battery_optimizer_override_mode
        name: Manual override
      - entity: switch.battery_optimizer_advisory_only_mode
        name: Advisory-only mode
      - entity: button.battery_optimizer_apply_current_plan
        name: Apply current plan

  - type: entities
    title: Windows
    entities:
      - entity: sensor.battery_optimizer_cheapest_charge_windows
      - entity: sensor.battery_optimizer_best_discharge_windows
```

## Optimization Strategy

The optimizer uses dependency-free dynamic programming rather than a heavy MILP solver dependency. It:

1. Reads the available price horizon, normally today plus tomorrow.
2. Adds the configured per-kWh tax/grid fee to spot prices for an all-in import price.
3. Computes low and high price thresholds from the 30th and 70th percentiles.
4. Estimates whether the spread is profitable after round-trip efficiency, degradation cost, and hysteresis.
5. Discretizes battery SOC into small states and searches for the lowest-cost path through the horizon.
6. Evaluates charge, hold, and discharge in every interval instead of hard-blocking decisions by percentile thresholds.
7. Adds degradation and aggressiveness-scaled cycling penalty directly into the objective.
8. Uses future stored-energy value to avoid wasting battery before better later peaks.
9. Caps discharge by the load forecast so the plan avoids discharging beyond expected household consumption.
10. Holds when the spread is not worth cycling the battery.
11. Recomputes often, but by default only writes inverter settings every 30 minutes unless a phase-current emergency needs an immediate update.
12. Applies hysteresis and minimum dwell intervals to reduce charge/discharge oscillation.
13. Explains the current decision in plain attributes.

Load forecasting uses recorder history by default. It now:

1. learns exact weekday-and-interval patterns
2. learns separate workday vs weekend/holiday profiles
3. blends those historical patterns with a rolling recent trend
4. falls back to current load when history is too thin

The `sensor.battery_optimizer_load_forecast` attributes show the forecast source, sample count, workday/weekend-holiday profile, raw pattern value, recent-trend value, current-load fallback, and adaptive bias for each point. A dedicated `load_forecast_entity` can still be configured for external forecasts.

This is deliberately conservative. A future linear programming backend can be added behind the same `optimize()` input/output model.

## Safety Behavior

- If prices or SOC are missing, the coordinator asks the backend to hold.
- Advisory-only mode prevents writes to Solarman entities.
- Grid charging current is capped by `number.inverter_battery_max_charging_current` if available.
- Peak shaving is enabled before applying active commands.
- Per-phase peak shaving watches each configured CT current sensor. If any phase reaches the configured threshold, the backend lowers the inverter's total-watt peak shaving setpoint to force battery support even when total three-phase load is still low.
- The coordinator refreshes every 30 seconds. This is useful fuse-risk reduction, but Home Assistant should not be treated as the only electrical protection layer for very fast spikes.
- The reserve SOC default is `10%`.
- The preferred charge ceiling is `90%`; the optimizer may use `100%` only when high-price opportunities justify it and the option is enabled.

## Adding New Battery/Inverter Backends

Add a new backend module that implements the `CommandBackend` protocol in `backend.py`:

```python
class MyBackend:
    async def apply(self, plan: PlanInterval) -> CommandResult:
        ...

    async def hold(self, reason: str) -> CommandResult:
        ...
```

Keep backend code limited to Home Assistant service calls and inverter-specific translation. Do not put optimization logic in the backend. The optimizer should only emit `charge`, `discharge`, or `hold` with target kW and reasons.

Then update `BatteryOptimizerCoordinator` to choose the backend from configuration.

## Development

Run optimizer tests:

```bash
pytest tests
```

The Home Assistant entity layer normally needs Home Assistant's test framework. The included unit tests focus on the pure optimizer so the most important planning behavior is fast and deterministic.
