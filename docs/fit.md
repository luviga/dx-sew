# Fit of the group weights to ERCOT's 2018 hourly load

`dxsew.fit.weights` fits one weight per fit column so that the weighted sum of the groups' hourly curves matches ERCOT's 2018 hourly fit target (decision K43): building profiles summed hour by hour, no power flow, weights fitted on three weeks of every month and energy, peak and daily range errors reported on the rest. Decision K71 (21 November 2026) replaced the fitted weights: two sector factors are computed from EIA-861, nothing is fitted to the hourly curve, and the hourly curve becomes the test (section 7). Under decision K74 (21 November 2026) the sector factors are report-only diagnostics: no factor reaches any feeder, substation or curve. The code is in `dxsew/fit/weights.py` and its tests are in `tests/regression/test_fit.py`.

```
uv run python -m dxsew.fit.weights --stamp <stamp> --curves <feeder_curves.parquet> --typical <typical_feeders csv> \
    --target <fit target parquet> --stratum-curves <stratum_curves.parquet> --features <features parquet> \
    --eia861 <EIA-861 zip> [--target-summary <fit target summary json>] [--out-root <folder>]
```

Inputs: the run feeders' year curves of [curves.md](curves.md) (`feeder_curves.parquet`, long table `feeder, timestamp, kw`), the typical-feeder table of [groups.md](groups.md) (columns `stratum`, `group`, `customers`, `starting_weight`, `run_feeder`, `run_is_placeholder`, `run_feeder_customers`) and the fit target of [fit_target.md](fit_target.md). For section 7 also: the stratum curves of [curves.md](curves.md) (`stratum_curves.parquet`, one row per stratum and 15-minute point with `kw` and one column per sector and end use, `res:<end use>` or `com:<end use>`), the features file of the frame feeders (`feeder`, `res_customers`, `com_customers`, one row per frame feeder), the EIA-861 zip of the target's year, and the fit target's summary file (default `<target stem>_summary.json` beside the target), from which the year, state and balancing authority are read. The target already has transmission and distribution losses and the industrial and transportation load removed (K45), so no loss term enters the fit. It writes `<out-root>/fit_<stamp>/`, by default under `<data_root>/texas`.

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
- **Fitted weights**: `w = argmin over w >= 0 of sum_h (sum_j w_j X_j(h) - T(h))^2` on the fit hours, without intercept, by non-negative least squares (`scipy.optimize.nnls`, https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.nnls.html). Since K71 this free fit is a measurement only: its weights are not used as group weights anywhere.

## 5. Errors

Each model is scored on the test hours and, separately, on the fit hours. For a set of hours:

- **energy error**: `(sum model - sum target) / sum target`;
- **peak error**, per month: `(max model - max target) / max target` over that month's hours of the set; reported per month and as the mean of the absolute values over the months;
- **daily range error**, per day: `((max model - min model) - (max target - min target)) / (max target - min target)`; reported as the mean of the absolute values over the days, and per month as the mean of the absolute values over that month's days;
- **CV(RMSE)**: `sqrt(mean((model - target)^2)) / mean(target)`. This metric is a hub addition to K43;
- **hour of the highest value**: the hour of the set with the highest model value against the hour with the highest target value, and the hours between them.

## 6. Weights

For each group, the weight table gives its fit column, the groups and strata the column holds, the column's customers and fitted weight. Its implied weight is the column's fitted weight times the group's starting weight, and its fitted share is the implied weight over the sum of implied weights in its stratum, beside the starting weight. A fitted weight of 1 for every column gives back the starting weights as shares. The table is a record of the free fit; under K71 no weight in it is used.

## 7. Sector factors (K71)

The frame's load is corrected by two numbers only, one for homes and one for businesses, computed by formula from EIA-861 sales and customers of the target's year, state and balancing authority. Nothing is fitted to the hourly curve; the hourly curve is the test. Group shares inside every stratum stay at customer counts.

### Sector curves

From `stratum_curves.parquet`, at starting weights, per stratum and 15-minute point: `R` is the sum of the `res:` columns other than the end uses of the setup key `fit.excluded_end_uses` (the PV column `res:pv`, which the building total leaves out; the sentence beside the key gives the evidence) and `B` the sum of the `com:` columns. The rounding gap `g = kw - (R + B)` is shared in proportion, `R' = R + g R / (R + B)` and `B' = B + g B / (R + B)`, so `R' + B' = kw`; a point with `R + B = 0` stops the run. `R(t)` and `B(t)` are the sums of `R'` and `B'` over the strata, in kW on the building files' own timestamps (15-minute period-ending EST). Both are mapped to CST hours ending by the rule of section 1. Two checks are reported, each with its largest gap: in every 15-minute point and stratum, `|kw - (R' + B')|` against 0.1 % of `kw`, with the largest shared gap `|g| / |kw|` beside it; and hourly `R + B` against the starting model `starting_mw` within a relative 1e-4. Both tolerances are hub settings, labelled ASSUMPTION in the setup (`fit.end_use_tolerance`, `fit.starting_tolerance`). The run reports failures and does not stop on them.

### Inputs per sector

For each sector `k` (homes `res`, businesses `com`):

- `S_k`: EIA-861 sales to ultimate customers of the year, MWh, in the target's state and balancing authority, Bundled and Energy rows (residential for homes, commercial for businesses; workbook `Sales_Ult_Cust_<year>.xlsx`, sheet States, measure Sales; the URL is `eia861_<year>` in `config/sources.yaml`). Delivery rows are dropped as in [fit_target.md](fit_target.md); the totals with them are reported beside.
- `N_eia_k`: EIA-861 customers of the same rows (measure Customers, unit Count, under the same class header).
- `E_k`: the frame's annual energy of sector `k` at starting weights, MWh: the sum of all 15-minute points of `R` or `B` (35,040 for a full year) times 0.25 h, divided by 1,000. The full year of points is used, so the year matches the calendar year of the sales.
- `N_model_k`: customers of sector `k` implied by the starting model: the sum over the groups of `customers / run_feeder_customers` times the run feeder's own `res_customers` or `com_customers` in the features file.
- `N_frame_k`: frame customers of sector `k`: the sum of `res_customers` or `com_customers` over every feeder of the features file.

### Factors

- `M_k = S_k / E_k`, the multiplier on the frame's sector-k curve for the ERCOT total.
- `e_k = (S_k / N_eia_k) / (E_k / N_model_k)`, the energy factor: EIA-861 sales per customer over the model's energy per customer. Under decision K74 (21 November 2026) it is a report-only diagnostic and reaches no feeder, substation or curve.
- `m_k = N_frame_k / N_model_k`, the mix factor: the frame's own sector mix over the run feeders' mix. It is carried by the frame's own customer counts when feeders are assembled and is not applied to any feeder.
- `c_k = N_eia_k / N_frame_k`, the count factor: ERCOT customers over frame customers. It measures how much of ERCOT the frame covers and is not applied to any feeder.

By construction `e_k * m_k * c_k = M_k`; the run checks it within 1e-12 relative and stops if it fails.

ASSUMPTION: a SMART-DS customer, one load name without its _1 or _2 phase ending, counts as one EIA-861 customer of the same class. The frame holds {N_frame_res} homes and {N_frame_com} businesses against {N_eia_res} residential and {N_eia_com} commercial customers that EIA-861 reports for ERCOT in 2018.

ASSUMPTION: every home in ERCOT takes the same energy factor, {e_res} in 2018, and every business the same factor, {e_com}, in all seven strata, because EIA-861 reports sales and customers by balancing authority and class and not by climate zone or by urban and rural area.

ASSUMPTION: EIA-861 residential sales and customers in ERCOT describe the homes that ResStock models, and commercial sales and customers the buildings that ComStock models.

The run fills the placeholders with its computed values and writes the filled texts to `summary.json` under `sector.assumptions`. The templates sit in the block `fit` of `config/setups/ercot_2018.yaml` (key `assumptions`), beside the values they qualify.

### Models added

Scored like the models of section 4, on both splits:

- **sector_formula**: `M_res * R(h) + M_com * B(h)`. This is the K71 model. Its energy over the full year of 15-minute points equals `S_res + S_com`; the target is the sales less transmission and distribution losses ([fit_target.md](fit_target.md)), so its energy error against the target is expected to be positive by about the loss share.
- **sector_fit**: `s_res * R(h) + s_com * B(h)` with `s_res, s_com >= 0` by least squares on the fit hours (`scipy.optimize.nnls` on the two columns). Report only: it never enters any other output.
- **scale_plus_constant**: `a * X(h) + b`, `X` the starting model, least squares on the fit hours (`numpy.linalg.lstsq`, https://numpy.org/doc/stable/reference/generated/numpy.linalg.lstsq.html), without bounds. Report only.

## Outputs

| File | Content |
|---|---|
| `hourly.parquet` | One row per hour used: `cst_hour_ending`, `utc_hour_ending`, `split` (`fit` or `test`), `target_mw`, `starting_mw`, `common_scale_mw`, `fitted_mw`, and one column per fit column at weight 1 in MW, named by its run feeder; then `res_mw` and `com_mw` (the sector curves `R` and `B` at weight 1), `sector_formula_mw`, `sector_fit_mw` and `scale_plus_constant_mw`. |
| `weights.csv` | One row per group: `stratum`, `group`, `customers`, `starting_weight`, `run_feeder`, `run_is_placeholder`, `run_feeder_customers`, `factor` (customers over run feeder customers), `column`, `column_groups` (`stratum#group` joined by `;`), `column_strata`, `column_customers`, `fitted_weight`, `implied_weight`, `fitted_share`. |
| `errors.csv` | One row per model (`starting`, `common_scale`, `fitted`, `sector_formula`, `sector_fit`, `scale_plus_constant`) and split (`test`, `fit`): hours, days, model and target MWh, `energy_error`, `peak_error_mean_abs`, `range_error_mean_abs`, `cv_rmse`, the model's and the target's highest MW and their hours, `peak_hour_gap_hours` (model hour minus target hour). |
| `months.csv` | One row per model, split and month: hours, `energy_error`, `peak_error`, `range_error_mean_abs`. |
| `hours_left_out.csv` | CST hours left out (step 1): whether the hour is a target hour, the fewest and most 15-minute points found per feeder, and why. |
| `sector_factors.csv` | One row per sector (`res`, `com`): the EIA-861 class, `S_mwh` and `N_eia` with their source URL and their totals with Delivery rows (report only), `E_mwh` and its number of points, `N_model`, `N_frame`, each with the role of its source file, `M`, `e`, `m`, `c`, the relative gap of `e * m * c` to `M`, the fitted scale `s_k` of `sector_fit` and its relative gap to `M`, and `a` and `b` of `scale_plus_constant` (the same on both rows). |
| `summary.json` | Hours in the target, used, fit and test; columns and groups; the common scale; the residual norm of the fit; the number of columns at weight zero; under `sector`: the EIA-861 sales and customers by class with and without Delivery rows, the rows per service type, `E`, `N_model`, `N_frame`, the factors, `s_res`, `s_com`, `a`, `b`, the three filled ASSUMPTION texts, both checks of the sector curves with their largest gaps, the full-year energy of `sector_formula` against the sales, and whether the EIA-861 sales equal those in the fit target's summary; the input paths. |

Errors are shares, so 0.05 means 5 percent; a negative energy or peak error means the model is below the target. Times are CST hour ending (UTC minus 6 hours). No figures are drawn; the hourly table holds everything a plot needs.
