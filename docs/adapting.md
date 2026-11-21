# Adapting dx-sew to another balancing area

The package names no region. A balancing area and year is one setup file; the code reads every value it needs from that file and from the shared files under `config/`. This page lists what a new setup needs, and the parts of the code that today exist only for the ERCOT setup.

## Steps

1. Add the area to `config/areas.yaml` if it is not there (NYISO and CAISO already are): `name`, `states`, `rto`, `ba_code`, `ba_prefix`, `veto_ignore_marks`. Check the `ba_code` against the EIA-861 sales rows of its states. Run the veto report for it, which needs no SMART-DS data: `uv run python -m dxsew.frame.veto --area <name>`.
2. Add its names, state codes and codes to `scan.forbidden_terms` in `config/scan.yaml`; the scan test fails until the list holds every name of `config/areas.yaml`.
3. Copy `config/setups/ercot_2018.yaml` to `config/setups/<area>_<year>.yaml`, set `setup_id`, `area`, `label`, `year` and `output_folder`, and replace every value that belongs to ERCOT or to 2018: the SMART-DS release block and the staging folder, the Census state code, the EIA-861 block (`geography.balancing_area`, `eia861`), the map prefix, the climate zone metadata, the coupling's state code and file names (`coupling`, `curves.recipe_file`, `curves.peak_days_file`), the fit target block (balancing area, state, standard time offset, prevailing time zone, published figures) and the ASSUMPTION texts that name the area. Write each new value next to its source or its ASSUMPTION sentence. The loader checks the file against `config/areas.yaml` and stops on any key it does not know.
4. Add the setup to `config/setups/registry.json`. Keep `default` unless the new setup should be the one used without `--setup`.
5. Add the source URLs the new setup downloads to `config/sources.yaml` (EIA-930 halves of the year, EIA-861 zip, the state's electricity profile).
6. Run each step with `--setup <id>`, in the order of the commands table of the README.

A data format that differs from the ones read today gets its reader in `dxsew/sources/`, with its file names and layout as keys of the setup.

## Parts written for the ERCOT setup only

These code paths have no second area yet and may need work for another one.

- SMART-DS zone endings. The Full Texas release names load shapes `<type>_<qty>_<digits>-<zone>_pu` with ERCOT weather-zone endings (`-Coast`, `-North-Central`). The digit-only renaming of `dxsew.sources.smartds_names` and the zone code table `inputs/smartds_zone_codes.csv` exist because the coupling cannot read those endings. Another SMART-DS release may have no endings, or other ones; the table would then be rebuilt with `uv run python -m dxsew.sources.smartds_names --setup <id>`, and its path set in `inputs.zone_codes`.
- Zone agreement of `dxsew.frame.trust` (check 1). It pairs each map polygon `<prefix>-<code>` with the zone ending whose initials are `<code>` (`ERCO-NC` with `North-Central`). Only the ERCOT polygons and the Texas zone endings follow that pattern.
- PUCT cross-check of `dxsew.frame.trust` (check 2). It reads a county list of the Public Utility Commission of Texas, given by hand on the command line. Another state has no such list in this form.
- Central Standard Time. The fit and the curves use the CST hour ending and the EST stamps of the building files (`EST_MINUS_CST` in `dxsew.fit.weights`, one hour). An area in another time zone needs the setup's `fit_target.standard_utc_offset_hours` and that one-hour offset revisited together.
- Function and column names. Some function names in `dxsew.curves.year` still say `ercot` (`ercot_days`); the output columns take the setup's `label`.
- The source texts of the fit (`fit.sources`) name the old module `dxsew.texas.curves`, kept word for word so that outputs of record reproduce.
