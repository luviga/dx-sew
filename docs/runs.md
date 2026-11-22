# Running the typical feeders alone

The typical-feeder table (`typical_feeders_<stamp>.csv`, see [groups.md](groups.md)) names one run feeder per group (decisions K61 and K62). `dxsew.texas.runs` prepares every distinct run feeder to run alone through DeltaE/dss-eulp-coupling (https://github.com/DeltaE/dss-eulp-coupling) at the pin, one case per feeder. The code is in `src/dxsew/texas/runs.py` and its tests are in `tests/texas/test_texas_runs.py`.

```
uv run python -m dxsew.texas.runs --stamp <stamp> --typical <typical_feeders csv> --features <features parquet> \
    --runs-root <absolute folder> --base-local <coupling local.yaml> --max-staging-gb <GB> --min-free-gb <GB>
```

`--features` gives each run feeder's subregion, substation, county and stratum; the typical-feeder table holds only feeder names.

## Settings

The block `runs` of `configs/texas_sample.yaml`:

- `staging_folder` (`dxsew_runs`): folder under `smart_ds_base` that receives the staged substations of the run feeders and their load parquets;
- `alone_folder` (`dxsew_alone`): folder under `smart_ds_base` that holds one tree per run feeder;
- `parallel` (4): coupling processes at once for phase 5b (schedule C of decision K69).

## Steps

1. **Staging.** The substations of the run feeders are staged with `stage.stage()` ([texas_sample.md](texas_sample.md), section on staging) with digit-only load shape names (K55) and renamed load parquets (K50). The function is given a copy of the settings whose `staging.folder` is `runs.staging_folder`, so the staged tree, the parquet folder and `name_map.csv` are new and the staging of the sample under `staging.folder` is never written. Before the staging, the released copies are placed and the released load parquets they use are downloaded in parallel into the new parquet folder under their released names; the staging then renames each in place. Both disk limits apply to every download. The outputs are `<staging>/name_map.csv` with its copy `<data_root>/texas/name_map_runs_<stamp>.csv`, the staging table `staging_runs_<stamp>.csv` and the checks table `checks_runs_<stamp>.csv`. A zone ending with no code in `configs/texas_zone_codes.csv` is added with `names.extend_zone_codes`, which keeps the codes already there, and the substations are staged again; the staging table records the endings added.
2. **Alone trees.** Each run feeder gets a copy of its staged feeder folder only, at `<smart_ds_base>/<alone_folder>/<feeder>/<subregion>/base_timeseries/opendss/<substation>/<feeder>/`, each file checked by SHA-256 against the staged file. The feeder folder keeps its staged name `<substation>--<feeder>`. The coupling discovers the feeders of a case as the folders under its circuit root, so it finds that one feeder.
3. **Settings folder** `<data_root>/texas/coupling_settings_runs_<stamp>/`, merged by `run_case.py --settings` over the coupling's own files (coupling README, "Settings from a caller"):
   - `local.yaml`: every key of the coupling `local.yaml` given, and `runs_root` set to the absolute folder given;
   - `topology_registry.yaml`: one entry per run feeder, built by `settings.registry_entry` with `smart_ds_relpath` `<alone_folder>/<feeder>/<subregion>/base_timeseries/opendss`, `circuit_folder` `<substation>` and `parquet_relpath` `<staging_folder>/parquet_data`. The topology id is `TX_<subregion>_<substation>_<short feeder>`, where the short feeder is the part of the folder name after `--`, so that ids hold no `--`;
   - `pipeline_config.yaml`: only `eulp_release: {residential: resstock_amy2018_release_2}`, the ResStock 2024 release 2 on actual meteorological year 2018 weather (K22, https://oedi-data-lake.s3.amazonaws.com/nrel-pds-building-stock/end-use-load-profiles-for-us-building-stock/2024/resstock_amy2018_release_2/). The coupling then reads the metadata folder `Metadata_TX_Residential_2024_resstock_amy2018_release_2` under `eulp_metadata_root` and downloads homes into a folder of their own (coupling README, `eulp_release`). There is no `given_days.csv` and no `peak_day_rule` key.
4. **Cases.** One case file per run feeder in `<data_root>/texas/cases_runs_<stamp>/`, built by `settings.case_file` with the topology id above and `execution: max_feeders: 1`.
5. **Dry run.** `run_case.py --case <first case> --phase pre --dry-run --settings <folder>` with the pin checked before and after. Its output, saved as `dry_run_runs_<stamp>.txt`, must show `PIPELINE_SMART_DS_ROOT` equal to the first feeder's alone tree down to its substation folder and `PIPELINE_PARQUET_ROOT` equal to the new parquet folder.

`runs_<stamp>.csv` lists each run feeder with its strata, groups, customers of its groups, loads, topology id, case file, alone folder, the files and bytes copied, and a note when its substation did not stage.

## Running the cases

Each case runs one phase at a time with `dxsew.coupling.run_case(case, settings, seasons, phase=<id>)` ([coupling.md](coupling.md)). The phases that download (`pre` to `5a`) run one feeder at a time: phase 5a of the coupling writes each building file under its final name and skips a name that exists, so two cases at once, or a failed download, can leave a partial file that later runs skip (problem log P7). Phase `5b` reads only, so it runs `runs.parallel` feeders at once, with `check=False` and the pin checked once before and once after the batch.
