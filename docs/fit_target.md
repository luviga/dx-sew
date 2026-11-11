# Fit target

## What it is and why

The circuit weights of dx-sew are fitted to an observed hourly load curve for the balancing area. The bottom-up model builds that curve from SMART-DS circuits and EULP buildings, so what it produces is residential and commercial load measured at the customer meter. The observed curve published for ERCOT is different. It is total balancing-area demand, which also holds industrial and transportation load and the energy lost in transmission and distribution lines.

The fit target removes those two parts from the observed curve, so that the target and the model describe the same quantity: hourly residential and commercial load at the customer meter, for ERCOT in 2018.

## Formula

For each hour `t` of 2018:

```
T_t = D_t * (1 - lambda) - X / H
```

- `D_t` is ERCOT hourly demand in MW, which equals MWh in that hour. The year covers the hours ending 2018-01-01 01:00 through 2019-01-01 00:00 in Central Standard Time, which is UTC minus 6 hours with no daylight saving. In UTC these are the hours ending 2018-01-01 07:00 through 2019-01-01 06:00. The output holds both stamps. The run stops with an error if any UTC hour appears twice.
- `lambda = L / (S - U)` is the share of transmission and distribution losses, where `L` is estimated losses, `S` is total disposition and `U` is direct use, all for Texas in 2018.
- `X = I + Tr` is ERCOT industrial plus transportation sales to ultimate customers in 2018, in MWh.
- `H` is the number of hours in the year, 8,760 for 2018.

The run also checks the result. It reports annual energy, peak and minimum with their hours for `D_t` and `T_t`, the loss share with its three inputs, and EIA-861 sales by class with and without Delivery rows. It reports the ratio of `sum(T_t)` to EIA-861 residential plus commercial sales, flags that ratio when it falls outside 0.85 to 1.15 and rescales nothing. It stops with an error if any hour of `T_t` is negative. Last, it compares `D_t` with ERCOT's own published peak demand and annual energy.

## Sources

All retrieved on 11 November 2026. The download URLs are listed in `configs/sources.yaml`.

- **Hourly demand `D_t`.** EIA-930 six-month Balance files for 2018, January to June and July to December, rows of balancing authority `ERCO`. Files: https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_BALANCE_2018_Jan_Jun.csv and https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_BALANCE_2018_Jul_Dec.csv, listed in https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_File_List_Meta.csv, which feeds the Download Data window of the Hourly Electric Grid Monitor. The demand is EIA's column "Demand (MW) (Adjusted)", because the files have it. The run records every hour where it differs from the reported column "Demand (MW)". Timestamps come from the column "UTC Time at End of Hour". The local-time column is used only as a check. What EIA-930 demand is: https://www.eia.gov/electricity/930-content/about.html.
- **Loss share `lambda`.** EIA State Electricity Profile for Texas, full data tables, worksheet 10, Supply and disposition of electricity, year 2018: https://www.eia.gov/electricity/state/xls/SEP%20Tables%20for%20TX.xlsx, linked as "Full data tables" from https://www.eia.gov/electricity/state/texas/. EIA's loss-share formula: https://www.eia.gov/tools/faqs/faq.php?id=105.
- **Removed classes `X`.** EIA-861 2018, workbook Sales_Ult_Cust_2018.xlsx, sheet States, rows with state `TX` and balancing authority code `ERCO`: https://www.eia.gov/electricity/data/eia861/archive/zip/f8612018.zip, from https://www.eia.gov/electricity/data/eia861/. Service types Bundled and Energy are kept and Delivery is dropped, because adding Delivery rows to Energy rows counts the same customers' MWh twice (https://data.catalyst.coop/preview/pudl/core_eia861__yearly_sales). In ERCOT's competitive areas, EIA counts the customers of retail electric providers under full-service (Bundled) providers (note to Table 2.3, https://www.eia.gov/electricity/annual/table.php?t=epa_02_03.html).
- **ERCOT's own figures, for comparison only.** ERCOT, 2018 State of the Grid report, page 2: https://www.ercot.com/files/docs/2019/04/16/ERCOT_2018_State_of_the_Grid_Report.pdf.
- **Central Standard Time.** 15 U.S.C. 261: https://www.law.cornell.edu/uscode/text/15/261.

## Assumptions

The run fills the bracketed values with the computed numbers and writes the filled texts to the summary file.

- ASSUMPTION: ERCOT industrial and transportation load is a constant {X/8760} MW in every hour of 2018, equal to their EIA-861 2018 sales in balancing authority ERCO divided by 8,760 hours. Hourly industrial load for ERCOT is not published; the constant removes the right annual energy and leaves its hourly shape inside the target.
- ASSUMPTION: transmission and distribution losses are a constant {lambda} of ERCOT demand in every hour of 2018, the Texas 2018 share from EIA's State Electricity Profile, Table 10. Losses rise with the square of current, so the true share is higher at peak and lower at night.
- ASSUMPTION: hours with missing or non-positive ERCO demand are filled by linear interpolation for gaps of up to 3 hours and by the same hour of the previous day for longer gaps. The run counts and lists every filled hour.
- ASSUMPTION: the ratio of `sum(T_t)` to EIA-861 residential plus commercial sales is flagged when it lies outside 0.85 to 1.15. The band only marks the ratio for review.

## How to run

```
uv sync
cp configs/local.yaml.example configs/local.yaml   # set data_root
uv run python -m dxsew.fit_target
```

The run downloads any missing source file into `<data_root>/eia/` and reuses a file already there when its size matches what the server reports. It writes to `<data_root>/derived/`:

- `ERCO_2018_fit_target.parquet` and a `.csv` copy, with columns `utc_hour_ending`, `cst_hour_ending`, `demand_mw`, `filled`, `loss_share`, `removed_mw` and `target_mw`;
- `ERCO_2018_fit_target_summary.json`, with every scalar input, its source URL and its value, the derived values with their formulas, the filled hours, the checks and the filled ASSUMPTION texts.

Nothing the run writes is committed.
