# Glossary

Day A
: For a stratum and season, the day of the stratum curve's own seasonal peak ([curves.md](curves.md)).

Day B
: For a season, the day of the balancing area's observed seasonal peak, from the fit target's demand, by Central Standard Time hour ending ([curves.md](curves.md)).

Check set
: Substations run whole through the coupling beside the run feeders (setup key `trial.check_set`), to set the group curves scaled by customers against a substation's own run (decision K73) ([assembly.md](assembly.md)).

Frame
: The list of SMART-DS substations of a balancing area from which the sample and the groups are drawn: substations whose load-bus centroid lies in one of the area's map polygons and whose county has a climate zone, less those the EIA-861 veto removes ([frame_procedure.md](frame_procedure.md)).

Group
: A set of frame feeders of one stratum that the nested Ward tree joins at the dial's number of groups; each group has a starting weight, its share of the stratum's customers ([groups.md](groups.md)).

Loading
: A substation's peak kW over its rating in kVA, `loading_kw_over_kva_lower_bound`. It is a lower bound of the apparent loading, because the apparent power S is at least the active power P and no power factor is assumed ([assembly.md](assembly.md)).

Pass
: One run of every case of the trial under one peak-day rule of the coupling: the first under `per_circuit`, each mix on the feeder's own worst day; the second under `given`, every mix on day B ([trial.md](trial.md)).

Pin
: The one commit of DeltaE/dss-eulp-coupling that dx-sew runs, recorded in `config/coupling_pin.yaml` ([coupling.md](coupling.md)).

Placeholder
: For a group with no feeder that the coupling can run alone, the typical feeder of the nearest group of the same stratum that has one (decision K62) ([groups.md](groups.md)).

Regulator
: A transformer of a substation folder's `Transformers.dss` whose windings all carry the same kV; it adds nothing to the substation's rating ([assembly.md](assembly.md)).

Run feeder
: The feeder run through the coupling for a group: its typical feeder, or its placeholder when it has none. Several groups can share one run feeder ([runs.md](runs.md)).

Setup
: One balancing area and year, the file `config/setups/<setup>.yaml` named in `config/setups/registry.json`. It holds every value the code uses for that area, next to its source or its ASSUMPTION sentence ([configuration.md](configuration.md)).

Split buses
: A substation whose units feed two or more distinct low-side buses (column `split_buses`); its loading is also reported against its largest unit ([assembly.md](assembly.md)).

Stratum
: The kind of a sub-region (U or R, the last letter of its name) crossed with the Building America climate zone of the substation's county, written `<kind>|<zone>` ([texas_sample.md](texas_sample.md)).

Unit
: A transformer of a substation folder's `Transformers.dss` whose windings carry two or more distinct kV values. A substation's rating is the sum of its units' kVA; units with the same high-side and low-side buses are parallel ([assembly.md](assembly.md)).

Typical feeder
: The feeder of a group nearest the group's centre among those the coupling can run alone ([groups.md](groups.md)).

Workspace
: The folder `data_root` of `config/local.yaml`, outside the repository. It holds the downloads and every generated file; it holds tens of GB and is never committed ([file-map.md](file-map.md)).
