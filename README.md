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
phase_voltage_entities: sensor.inverter_grid_l1_voltage,sensor.inverter_grid_l2_voltage,sensor.inverter_grid_l3_voltage
```

The integration assumes your inverter remains in `Zero Export To CT`. It does not currently change `select.inverter_work_mode`.

## Remaining Values To Confirm

Before real control is enabled, confirm these values in the config flow:

- `sensor.inverter_battery_capacity` reports total usable capacity in kWh.
- `sensor.inverter_battery_voltage` reports battery voltage in volts.
- `number.inverter_battery_max_charging_current` and `number.inverter_battery_max_discharging_current` are the actual safety current limits in amps.
- The inverter peak shaving number still expects total watts, but Battery Optimizer watches individual phase currents and dynamically lowers that total-watt threshold when any phase reaches the per-phase limit.

## Entities

Created entities include:

- `sensor.battery_optimizer_planned_mode`
- `sensor.battery_optimizer_projected_soc`
- `sensor.battery_optimizer_expected_value`
- `sensor.battery_optimizer_upcoming_charge_hours`
- `sensor.battery_optimizer_upcoming_discharge_hours`
- `sensor.battery_optimizer_cheapest_charge_windows`
- `sensor.battery_optimizer_best_discharge_windows`
- `sensor.battery_optimizer_decision_reasons`
- `sensor.battery_optimizer_last_command`
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
    title: Battery Optimizer
    entities:
      - entity: sensor.battery_optimizer_planned_mode
      - entity: sensor.battery_optimizer_projected_soc
      - entity: sensor.battery_optimizer_expected_value
      - entity: sensor.battery_optimizer_upcoming_charge_hours
      - entity: sensor.battery_optimizer_upcoming_discharge_hours
      - entity: sensor.battery_optimizer_decision_reasons
      - entity: select.battery_optimizer_override_mode
      - entity: switch.battery_optimizer_advisory_only_mode
      - entity: button.battery_optimizer_apply_current_plan
  - type: entities
    title: Windows
    entities:
      - entity: sensor.battery_optimizer_cheapest_charge_windows
      - entity: sensor.battery_optimizer_best_discharge_windows
```

## Optimization Strategy

The first optimizer is a rolling-horizon heuristic rather than a solver dependency. It:

1. Reads the available price horizon, normally today plus tomorrow.
2. Computes low and high price thresholds from the 30th and 70th percentiles.
3. Estimates whether the spread is profitable after round-trip efficiency and degradation cost.
4. Charges in cheap intervals if there is room below the chosen max SOC.
5. Discharges in expensive intervals if SOC remains above reserve.
6. Holds when the spread is not worth cycling the battery.
7. Applies hysteresis and minimum dwell intervals to reduce charge/discharge oscillation.
8. Explains the current decision in plain attributes.

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
