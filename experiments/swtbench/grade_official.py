"""Grade SWT-bench (Lite) test-generation runs with the OFFICIAL harness.

Modeled on experiments/swebench_verified/grade_official.py: our study cells
on SWE-bench_Lite instances get a `pending_official` grade.json stub from
run_experiment.py (swt_grade.grade is SWE-Pro-docker-specific); this script
fills in the REAL verdicts by invoking the official logic-star-ai/swt-bench
harness (external clone with its own venv, builds eval images locally) and
merging its per-instance outcomes back into each cell's grade.json.

Per condition:
  1. collect <results-dir>/<iid>__<cond>/test_patch.diff for every id in
     --ids-file into a predictions JSONL ({"instance_id",
     "model_name_or_path": <cond>, "model_patch": <diff>}); cells with an
     empty/missing diff are NOT submitted but are counted (and their
     grade.json is closed out as unresolved -- an empty test patch has no
     official verdict to wait for);
  2. run  <harness>/.venv/bin/python -m src.main --dataset_name <ds>
     --predictions_path <preds.jsonl> --filter_swt --run_id <run-id>
     --max_workers N --instance_ids <submitted ids>   (cwd = harness root,
     stdout+stderr -> a per-condition harness.log);
  3. parse each submitted instance's official
     run_instance_swt_logs/<run-id>/<cond>/<iid>/report.json and MERGE into
     the cell's grade.json:
       {"pending_official": false, "reproduced": <official S>,
        "applied": <official W>, "official": <raw per-instance record>,
        "resolved": <S>}
     Submitted cells the harness produced NO report for (its error_ids) stay
     pending_official=true -- there is no official verdict to record.
  4. write _official_summary.json (+ predictions.jsonl, harness.log) under
     <results-dir>/_official_swt/<cond>/.

S ("Success Rate (S)") is the official per-instance report's `resolved`
field. W ("Applicability (W)") is the official `no_error_filter` from the
harness's figures/util.py, applied to the same record -- copied VERBATIM
below (>=1 of the predicted suite's tests actually executed, i.e. >=10% of
the base run's test count; guards against patches that syntax-error the
suite). No grading logic is reimplemented: the harness's report.json is
authoritative and is stored raw under "official".

Usage (study venv, study root):
    python -m experiments.swtbench.grade_official \
        --results-dir results/<exp> --conditions baseline cfg_perrep_flow3 \
        --ids-file results/_swt_prevpaper_161_ids.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = Path("/home/tempadmin/tools_scratch/swt-bench")
DATASET_DEFAULT = "princeton-nlp/SWE-bench_Lite"


def _resolve(p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (STUDY_ROOT / q)


def no_error_filter(r: dict) -> bool:
    """Official W ("Applicability") -- copied VERBATIM from the harness's
    figures/util.py::no_error_filter (only the docstring differs). True iff
    at least one test was executed, i.e. the patch introduced no
    syntax/name error that killed collection."""
    if "tests_pred" not in r:
        return False
    original_number_tests = sum(len(t) for t in r["tests_base"].values())
    tests_after_pred = sum(len(t) for t in r["tests_pred"].values())
    # if tests after pred is less than 10% of the original number of tests
    # this counts as error
    return tests_after_pred >= 0.1 * original_number_tests


def collect_predictions(results_dir: Path, condition: str, ids: list) -> dict:
    """Classify every id's cell; return the collection record.

    Returns {"preds": {iid: pred}, "empty": [iid], "missing_cell": [iid]}.
    """
    preds, empty, missing_cell = {}, [], []
    for iid in ids:
        cell = results_dir / ("%s__%s" % (iid, condition))
        if not cell.is_dir():
            missing_cell.append(iid)
            continue
        pf = cell / "test_patch.diff"
        patch = pf.read_text("utf-8") if pf.is_file() else ""
        if not patch.strip():
            empty.append(iid)
            continue
        preds[iid] = {"instance_id": iid,
                      "model_name_or_path": condition,
                      "model_patch": patch}
    return {"preds": preds, "empty": empty, "missing_cell": missing_cell}


def filtered_out_ids(ids: list, dataset_name: str, harness: Path) -> set:
    """Ids the harness's --filter_swt would drop from the dataset (submitting
    one makes get_dataset_from_preds raise). Read from the harness's own
    filter file -- the same file --filter_swt reads -- so semantics match."""
    name = dataset_name.lower()
    if "lite" in name:
        ff = harness / "dataset" / "filter_cases_lite.txt"
    elif "verified" in name:
        ff = harness / "dataset" / "filter_cases_verified.txt"
    else:
        ff = harness / "dataset" / "filter_cases_full.txt"
    if not ff.is_file():
        return set()
    bad = set(ff.read_text("utf-8").splitlines())
    return {i for i in ids if i in bad}


def run_harness(pred_path: Path, ids: list, condition: str, run_id: str,
                dataset_name: str, max_workers: int, timeout: int,
                harness: Path, log_path: Path) -> int:
    cmd = [
        str(harness / ".venv" / "bin" / "python"), "-m", "src.main",
        "--dataset_name", dataset_name,
        "--predictions_path", str(pred_path),
        "--filter_swt",
        "--run_id", run_id,
        "--max_workers", str(max_workers),
    ]
    if timeout:
        cmd += ["--timeout", str(timeout)]
    cmd += ["--instance_ids", *ids]
    print("[swt-official] %s: running harness on %d instances (log: %s)"
          % (condition, len(ids), log_path))
    with log_path.open("a", encoding="utf-8") as log:
        log.write("+ %s\n" % " ".join(cmd))
        log.flush()
        proc = subprocess.run(cmd, cwd=str(harness), stdout=log,
                              stderr=subprocess.STDOUT, check=False)
    return proc.returncode


def official_record(harness: Path, run_id: str, condition: str,
                    iid: str) -> dict | None:
    """The raw per-instance record from the official report.json, or None."""
    patch_id = condition.replace("/", "__")
    rf = (harness / "run_instance_swt_logs" / run_id / patch_id / iid /
          "report.json")
    if not rf.is_file():
        return None
    try:
        data = json.loads(rf.read_text("utf-8"))
    except ValueError:
        return None
    return data.get(iid)


def merge_grade(cell: Path, iid: str, condition: str, patch: dict) -> None:
    """Merge official keys into the cell's grade.json (preserving the rest)."""
    gf = cell / "grade.json"
    try:
        g = json.loads(gf.read_text("utf-8")) if gf.is_file() else {}
    except ValueError:
        g = {}
    g.setdefault("instance_id", iid)
    g.setdefault("condition", condition)
    g.update(patch)
    gf.write_text(json.dumps(g, indent=2), "utf-8")


def grade_condition(results_dir: Path, condition: str, ids: list,
                    run_id: str, dataset_name: str, max_workers: int,
                    timeout: int, harness: Path) -> dict:
    work = results_dir / "_official_swt" / condition
    work.mkdir(parents=True, exist_ok=True)

    col = collect_predictions(results_dir, condition, ids)
    preds = col["preds"]
    filtered = sorted(filtered_out_ids(list(preds), dataset_name, harness))
    submitted = [i for i in preds if i not in set(filtered)]
    print("[swt-official] %s: %d ids -> %d with patches, %d empty, "
          "%d missing cell, %d filtered by --filter_swt"
          % (condition, len(ids), len(preds), len(col["empty"]),
         len(col["missing_cell"]), len(filtered)))

    pred_path = work / "predictions.jsonl"
    with pred_path.open("w", encoding="utf-8") as f:
        for iid in submitted:
            f.write(json.dumps(preds[iid]) + "\n")

    t0 = time.time()
    rc = None
    if submitted:
        rc = run_harness(pred_path, submitted, condition, run_id,
                         dataset_name, max_workers, timeout, harness,
                         work / "harness.log")
    wall = time.time() - t0

    # merge official verdicts back into the cells
    per_instance, resolved_ids, applied_ids, error_ids = {}, [], [], []
    for iid in submitted:
        rec = official_record(harness, run_id, condition, iid)
        cell = results_dir / ("%s__%s" % (iid, condition))
        if rec is None:
            error_ids.append(iid)      # no official verdict: stays pending
            per_instance[iid] = {"status": "no_official_report"}
            continue
        s = bool(rec.get("resolved"))
        w = no_error_filter(rec)
        merge_grade(cell, iid, condition, {
            "pending_official": False, "reproduced": s, "applied": w,
            "official": rec, "resolved": s})
        per_instance[iid] = {"resolved": s, "applied": w,
                             "patch_successfully_applied":
                                 rec.get("patch_successfully_applied")}
        if s:
            resolved_ids.append(iid)
        if w:
            applied_ids.append(iid)
    # close out unsubmittable cells: an empty test patch is unresolved by
    # definition (no official verdict pending for it)
    for iid in col["empty"]:
        merge_grade(results_dir / ("%s__%s" % (iid, condition)), iid,
                    condition, {
                        "pending_official": False, "reproduced": False,
                        "applied": False, "official": None, "resolved": False,
                        "reason": "empty_test_patch_not_submitted"})
        per_instance[iid] = {"resolved": False, "applied": False,
                             "status": "empty_test_patch_not_submitted"}

    n_sub = len(submitted)
    summary = {
        "condition": condition,
        "run_id": run_id,
        "dataset": dataset_name,
        "harness_root": str(harness),
        "harness_returncode": rc,
        "harness_wall_seconds": round(wall, 1),
        "n_ids": len(ids),
        "n_submitted": n_sub,
        "n_empty_test_patch": len(col["empty"]),
        "n_missing_cell": len(col["missing_cell"]),
        "n_filtered_swt": len(filtered),
        "n_resolved": len(resolved_ids),
        "n_applied": len(applied_ids),
        "n_no_official_report": len(error_ids),
        "resolve_rate_submitted": len(resolved_ids) / n_sub if n_sub else 0.0,
        "resolved_ids": resolved_ids,
        "applied_ids": applied_ids,
        "no_official_report_ids": error_ids,
        "empty_test_patch_ids": col["empty"],
        "missing_cell_ids": col["missing_cell"],
        "filtered_swt_ids": filtered,
        "per_instance": per_instance,
    }
    out = work / "_official_summary.json"
    out.write_text(json.dumps(summary, indent=2), "utf-8")
    print("[swt-official] %s: S %d/%d, W %d/%d, %d no-report; saved %s"
          % (condition, len(resolved_ids), n_sub, len(applied_ids), n_sub,
             len(error_ids), out))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(
        "swtbench.grade_official",
        description="Merge OFFICIAL swt-bench harness verdicts into study "
                    "cells' grade.json (see module docstring).")
    p.add_argument("--results-dir", required=True,
                   help="results/<exp> with <iid>__<cond> cell dirs")
    p.add_argument("--conditions", nargs="+", required=True)
    p.add_argument("--ids-file", required=True,
                   help="JSON list of instance ids to grade")
    p.add_argument("--run-id", default=None,
                   help="official harness run id (default: official-<exp>)")
    p.add_argument("--dataset-name", default=DATASET_DEFAULT)
    p.add_argument("--max-workers", type=int, default=2)
    p.add_argument("--timeout", type=int, default=None,
                   help="per-instance test timeout secs (harness default "
                        "1800 if omitted)")
    p.add_argument("--harness-root", default=str(HARNESS_ROOT))
    a = p.parse_args(argv)

    results_dir = _resolve(a.results_dir)
    if not results_dir.is_dir():
        sys.exit("results dir not found: %s" % results_dir)
    harness = Path(a.harness_root)
    if not (harness / ".venv" / "bin" / "python").is_file():
        sys.exit("harness venv python not found under %s" % harness)
    ids = json.loads(_resolve(a.ids_file).read_text("utf-8"))
    run_id = a.run_id or ("official-%s" % results_dir.name)

    summaries = {}
    for cond in a.conditions:
        summaries[cond] = grade_condition(
            results_dir, cond, ids, run_id, a.dataset_name, a.max_workers,
            a.timeout, harness)

    print("\n===== OFFICIAL SWT-BENCH (S = reproduced) =====")
    for cond, s in summaries.items():
        print("  %-24s S %d/%d  W %d/%d  (empty %d, missing %d, no-report %d)"
              % (cond, s["n_resolved"], s["n_submitted"], s["n_applied"],
                 s["n_submitted"], s["n_empty_test_patch"],
                 s["n_missing_cell"], s["n_no_official_report"]))
    return summaries


if __name__ == "__main__":
    main()
