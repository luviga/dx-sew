# Year curves of the run feeders and common days

`dxsew.texas.curves` rebuilds the 15-minute year curve of every run feeder from the coupling's own recipe and building files (decision K66), checks each one against the coupling's peak days, sums the curves of each stratum (K47 as amended by K60) and chooses common days per stratum and season (K48 as amended by K60, K67). The code is in `src/dxsew/texas/curves.py` and its tests are in `tests/texas/test_texas_curves.py`.

```
uv run python -m dxsew.texas.curves --stamp <stamp> --typical <typical_feeders csv> --runs-root <folder> \
    --features <features parquet> [--fit-target <parquet>] [--workers <n>]
```

It reads the workspaces that [runs.md](runs.md) describes, after phase `5b` of DeltaE/dss-eulp-coupling (https://github.com/DeltaE/dss-eulp-coupling) at the pin, and `ERCO_2018_fit_target.parquet` ([fit_target.md](fit_target.md)). It writes `<data_root>/texas/curves_<stamp>/`.

## 1. Year curve of a run feeder (K66)

Phase 5b of the coupling writes the recipe `TX_required_parquets_per_feeder.csv` with columns `Feeder, Parquet_Folder, Parquet_File, REAL_LOAD_COUNT` (https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/scale_feeder_curves_impl.py#L126-L133). For each row in file order, dx-sew reads the building file `<parquet root>/<Parquet_Folder>/<Parquet_File>` and adds

```
kW = out.electricity.total.energy_consumption x 4 x REAL_LOAD_COUNT
```

to the feeder total, as the coupling does (https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/find_max_day_curve_impl.py#L513-L549). The energy is kWh per 15 minutes, so 4 times it is the mean kW of the interval. The parquet root is the one the run used: `smart_ds_base` and the entry's `parquet_relpath` in the settings copied into the run folder. The same sum is made per end use, keyed by sector (`res` for the residential download folder, `com` for the commercial one) and end-use name.

ASSUMPTION: the end uses of a building file are its columns `out.electricity.<end use>.energy_consumption` other than `total` and `net`; `net` is total less PV output, not an end use, and the end uses add to `total` within the rounding of the files. `strata.csv` reports the largest gap between the end-use sum and the total of each stratum.

A building file that cannot be read, or whose timestamps differ from the first file's, stops that feeder with a message. The coupling instead prints `Error reading` and leaves an unreadable file out of its sum; dx-sew does not repeat that fallback, so a partial download cannot pass silently.

## 2. Check against the coupling (K66)

The rebuilt total is placed by position on the coupling's calendar: point 1 at 2025-01-01 00:00, 15 minutes apart. The coupling's rule is then applied (https://github.com/DeltaE/dss-eulp-coupling/blob/154fa8d378b3e96a9799a01f3b719abf4235ad83/5b_profile_generation/find_max_day_curve_impl.py#L137-L197): per month, the day with the highest 15-minute point, kept when it has 96 points; in each season, the kept month with the highest such point, winter among December, January and February and summer among June, July and August. Both seasons are checked against `TX_peak_days.csv` of the workspace: month and day must be equal and `Peak_kW` equal within a relative 1e-6, and every recipe row must have been read. A feeder that fails either season is left out of every sum. `curves_check.csv` holds, per feeder and season, the coupling's month, day and `Peak_kW`, the rebuilt ones, the relative difference, the verdict, the rows read and the rows in the recipe.

2025 and 2018 both start on a non-leap 1 January, so a position has the same month and day on both calendars; only the weekday differs (2025-01-01 is a Wednesday, 2018-01-01 a Monday).

## 3. Timestamps

The saved curves keep the building files' own `timestamp` values. `timestamps.csv` records, per sector, the first and last value, the count and the step, with the number of feeders that showed them.

ASSUMPTION: each timestamp of a building file marks the end of its 15-minute interval, so the date of a point is the date of its interval start and 2019-01-01 00:00 belongs to 31 December 2018. The README of the End Use Load Profiles collection on OEDI states that results timestamps are "15-minute period-ending in EST" (section "Units and timestamps", https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/README.md); the READMEs inside the two release folders state no convention. The dates are taken as written, with no shift from Eastern to Central time. A file whose first timestamp is 1 January 00:00 is read as period-beginning instead.

## 4. Stratum curves

Each group of the typical-feeder table adds its run feeder's curve times `customers / run_feeder_customers` of its row; a stratum's curve is the sum over its groups, for the total and for each sector and end use. Groups whose run feeder failed are left out; `strata.csv` gives, per stratum, the groups and customers left out and their share.

## 5 to 8. Common days

All three are dates of 2018.

- **A**, per stratum and season: the date of the stratum curve's highest 15-minute point in December to February (winter) or June to August (summer).
- **B**, per season, the same for every stratum: the date of ERCOT's highest `demand_mw` in the season's months, in Central Standard Time with the hour-ending convention of the fit target, so an hour ending 00:00 belongs to the day before.
- **C**, per stratum and season, reported only: the date on which the most distinct run feeders of the stratum have their own seasonal peak (each feeder's own peak date from its rebuilt curve on the 2018 calendar). A tie goes to the date with the higher stratum curve; the tied dates are recorded.

A tie inside A or B goes to the earliest point. `common_days.csv` has one row per stratum, season and option with: the date and weekday; the stratum curve's highest point on that date as a share of its seasonal highest; the share of the stratum's run feeders, by count and by customers of their groups, whose own seasonal peak falls on that date; ERCOT's highest `demand_mw` on that date as a share of its seasonal highest; the time of the stratum's highest point on that date (interval start) and the five largest end uses there with their kW and share of the total. `feeder_peaks.csv` lists each run feeder's own seasonal peak date.

## 9. Given days

`given_days_A_<stamp>.csv` and `given_days_B_<stamp>.csv` have the columns `Feeder, Season, Month, Day` that the coupling reads under `peak_day_rule: given` (coupling README, "Peak-day rules"): one row per run feeder and season. A feeder takes the day of its own stratum, the stratum of the features file, also when it is the run feeder of groups in another stratum; the run prints such feeders.

## Outputs

| File | Content |
|---|---|
| `feeder_curves.parquet` | `feeder`, `timestamp`, `kw` (float32), feeders that passed the check |
| `stratum_curves.parquet` | `stratum`, `timestamp`, `kw` and one column `<sector>:<end use>` each (float32) |
| `curves_check.csv` | step 2 |
| `timestamps.csv` | step 3 |
| `strata.csv` | step 4: groups, customers and feeders used and left out |
| `feeder_peaks.csv` | each run feeder's own seasonal peak date |
| `common_days.csv` | steps 5 to 8 |
| `given_days_A_<stamp>.csv`, `given_days_B_<stamp>.csv` | step 9 |
