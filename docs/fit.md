# Fit of the group weights to ERCOT's 2018 hourly load

`dxsew.fit` fits one weight per fit column so that the weighted sum of the groups' hourly curves matches ERCOT's 2018 hourly fit target (decision K43): building profiles summed hour by hour, no power flow, weights fitted on three weeks of every month and energy, peak and daily range errors reported on the rest. The code is in `src/dxsew/fit.py` and its tests are in `tests/test_fit.py`.

```
uv run python -m dxsew.fit --stamp <stamp> --curves <feeder_curves.parquet> --typical <typical_feeders csv> \
    --target <fit target parquet> [--out-root <folder>]
```

Inputs: the run feeders' year curves of [curves.md](curves.md) (`feeder_curves.parquet`, long table `feeder, timestamp, kw`), the typical-feeder table of [groups.md](groups.md) (columns `stratum`, `group`, `customers`, `starting_weight`, `run_feeder`, `run_is_placeholder`, `run_feeder_customers`) and the fit target of [fit_target.md](fit_target.md). The target already has transmission and distribution losses and the industrial and transportation load removed (K45), so no loss term enters the fit. It writes `<out-root>/fit_<stamp>/`, by default under `<data_root>/texas`.

## 1. Hourly curves in Central Standard Time

The building files' timestamps are "15-minute period-ending in EST" (README of the End Use Load Profiles collection on OEDI, section "Units and timestamps", https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/README.md). Eastern Standard Time is UTC minus 5 hours and Central Standard Time (CST) is UTC minus 6 hours (the fifth and sixth zones of 15 U.S.C. 261, https://www.law.cornell.edu/uscode/text/15/261), and the fit target is stamped by CST hour ending.

- The 15-minute point stamped `t` in EST covers `(t - 15 min, t]` EST.
- The CST hour ending `h` covers `(h - 1 h, h]` CST, which is `(h, h + 1 h]` EST.
- So the CST hour ending `h` takes the four EST points stamped `h + 15 min`, `h + 30 min`, `h + 45 min` and `h + 60 min`. In code: `h = ceil_to_hour(t) - 1 h`.

A feeder's hourly value is the sum of those four kW values divided by 4 (the energy of the hour in kWh, which is the mean kW over the hour), divided by 1,000 to give MW. An hour of the target that lacks any of its four points for any feeder is left out. For 2018 that is the hour ending 2019-01-01 00:00 CST, which needs EST points of 2019-01-01 01:15 to 02:00 that the curves do not hold. The four points stamped 2018-01-01 00:15 to 01:00 EST fall in the CST hour ending 2018-01-01 00:00, which is not a target hour, and are not used. Both are listed in `hours_left_out.csv`.

## 2. Fit columns

Each group's curve is its run feeder's curve times `customers / run_feeder_customers` of its row (K47 as amended by K60). Groups that share a run feeder therefore have proportional curves, and a fit cannot tell their weights apart, so they are merged: there is one fit column per distinct run feeder, equal to the sum over its groups of `customers / run_feeder_customers` times the run feeder's hourly curve. Groups whose run feeder is a stand-in (`run_is_placeholder`, K62) are treated like any other group. Every run feeder of the table must have a curve, or the run stops.

## 3. Fit and test hours

ASSUMPTION: "three weeks of every month" in K43 is read as days 1 to 21 of each month, and the fourth week as day 22 to the month's end, so every hour of the year is used once.

ASSUMPTION: the day of an hour is the CST date of its hour start, so an hour ending 00:00 belongs to the day before. This is the hour-ending convention of the fit target and of the common days in [curves.md](curves.md).

With these rules 2018 has 21 x 24 x 12 = 6,048 fit hours and 8,760 - 6,048 = 2,712 test hours, less the hour left out in step 1.

## 4. Three models

Let `X_j(h)` be fit column `j` in MW and `T(h)` the target `target_mw`.

- **Starting weights**: every column at weight 1, so every group counts its own customers once. Within a stratum the groups then stand in the proportions of their starting weights (customer shares).
- **Common scale**: every column at the same weight `s = max(0, sum_h X(h) T(h) / sum_h X(h)^2)` with `X(h) = sum_j X_j(h)`, the least-squares scale on the fit hours, kept non-negative.
- **Fitted weights**: `w = argmin over w >= 0 of sum_h (sum_j w_j X_j(h) - T(h))^2` on the fit hours, without intercept, by non-negative least squares (`scipy.optimize.nnls`, https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.nnls.html).

## 5. Errors

Each model is scored on the test hours and, separately, on the fit hours. For a set of hours:

- **energy error**: `(sum model - sum target) / sum target`;
- **peak error**, per month: `(max model - max target) / max target` over that month's hours of the set; reported per month and as the mean of the absolute values over the months;
- **daily range error**, per day: `((max model - min model) - (max target - min target)) / (max target - min target)`; reported as the mean of the absolute values over the days, and per month as the mean of the absolute values over that month's days;
- **CV(RMSE)**: `sqrt(mean((model - target)^2)) / mean(target)`. This metric is a hub addition to K43;
- **hour of the highest value**: the hour of the set with the highest model value against the hour with the highest target value, and the hours between them.

## 6. Weights

For each group, the weight table gives its fit column, the groups and strata the column holds, the column's customers and fitted weight. Its implied weight is the column's fitted weight times the group's starting weight, and its fitted share is the implied weight over the sum of implied weights in its stratum, beside the starting weight. A fitted weight of 1 for every column gives back the starting weights as shares.

## Outputs

| File | Content |
|---|---|
| `hourly.parquet` | One row per hour used: `cst_hour_ending`, `utc_hour_ending`, `split` (`fit` or `test`), `target_mw`, `starting_mw`, `common_scale_mw`, `fitted_mw`, and one column per fit column at weight 1 in MW, named by its run feeder. |
| `weights.csv` | One row per group: `stratum`, `group`, `customers`, `starting_weight`, `run_feeder`, `run_is_placeholder`, `run_feeder_customers`, `factor` (customers over run feeder customers), `column`, `column_groups` (`stratum#group` joined by `;`), `column_strata`, `column_customers`, `fitted_weight`, `implied_weight`, `fitted_share`. |
| `errors.csv` | One row per model (`starting`, `common_scale`, `fitted`) and split (`test`, `fit`): hours, days, model and target MWh, `energy_error`, `peak_error_mean_abs`, `range_error_mean_abs`, `cv_rmse`, the model's and the target's highest MW and their hours, `peak_hour_gap_hours` (model hour minus target hour). |
| `months.csv` | One row per model, split and month: hours, `energy_error`, `peak_error`, `range_error_mean_abs`. |
| `hours_left_out.csv` | CST hours left out (step 1): whether the hour is a target hour, the fewest and most 15-minute points found per feeder, and why. |
| `summary.json` | Hours in the target, used, fit and test; columns and groups; the common scale; the residual norm of the fit; the number of columns at weight zero; the input paths. |

Errors are shares, so 0.05 means 5 percent; a negative energy or peak error means the model is below the target. Times are CST hour ending (UTC minus 6 hours). No figures are drawn; the hourly table holds everything a plot needs.
