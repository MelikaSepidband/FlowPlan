"""Grade SWE-bench Verified agent runs with the OFFICIAL swebench harness.

The Pro grade.py needs a per-instance `dockerhub_tag` that Verified instances
don't carry. Verified uses the official evaluator instead: collect each run's
patch.diff into a predictions JSON (instance_id -> model_patch) and invoke
`python3 -m swebench.harness.run_evaluation` (system python; images build/pull
via Docker). Parse the resulting report for resolved status.

Usage:
    python3 -m experiments.swebench_verified.grade_official \
        --results-dir results/<exp> --conditions baseline cfg_perrep_flow3 \
        --run-id <run_id> --cache-level base

Per condition, the resolved instance ids are written (merged across
invocations) to results/<exp>/official_grade_summary.json.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
SYS_PY = "python3"  # official swebench lives in system python, not the venv


def collect_predictions(results_dir: Path, condition: str) -> dict:
    """instance_id -> {model_patch, model_name_or_path, instance_id} for a cond."""
    preds = {}
    for cell in sorted(results_dir.glob("*__%s" % condition)):
        iid = cell.name[: -len("__%s" % condition)]
        pf = cell / "patch.diff"
        patch = pf.read_text("utf-8") if pf.is_file() else ""
        preds[iid] = {
            "instance_id": iid,
            "model_patch": patch,
            "model_name_or_path": condition,
        }
    return preds


def run_official(preds: dict, condition: str, run_id: str,
                 cache_level: str = "env", max_workers: int = 4) -> Path:
    """Write predictions, invoke the official evaluator, return report path.

    cache_level is the harness's own knob (images above it are removed after
    each instance). The 218-instance runs use "base": the per-(repo, version)
    env images of 218 instances need a lot of disk, so they are rebuilt per
    instance -- grading is slower but the verdicts are the same official
    harness verdicts.
    """
    work = STUDY_ROOT / "results" / "_official_grade" / ("%s__%s" % (run_id, condition))
    work.mkdir(parents=True, exist_ok=True)
    pred_path = work / "predictions.json"
    pred_path.write_text(json.dumps(list(preds.values())), "utf-8")
    ids = list(preds)
    cmd = [
        SYS_PY, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", "princeton-nlp/SWE-bench_Verified",
        "--split", "test",
        "--predictions_path", str(pred_path),
        "--run_id", "%s_%s" % (run_id, condition),
        "--max_workers", str(max_workers),
        "--cache_level", cache_level,
        "--instance_ids", *ids,
    ]
    print("[official-grade] %s: grading %d instances ..." % (condition, len(ids)))
    subprocess.run(cmd, cwd=str(work), check=False)
    # report lands as <condition>.<run_id>_<condition>.json in cwd
    reports = list(work.glob("*.json"))
    reports = [r for r in reports if r.name != "predictions.json"]
    return reports[0] if reports else None


def parse_report(report_path: Path) -> dict:
    if not report_path or not report_path.is_file():
        return {}
    rep = json.loads(report_path.read_text("utf-8"))
    return {
        "resolved_ids": rep.get("resolved_ids", []),
        "unresolved_ids": rep.get("unresolved_ids", []),
        "error_ids": rep.get("error_ids", []),
        "completed_ids": rep.get("completed_ids", []),
        "total": rep.get("total_instances"),
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", required=True)
    p.add_argument("--conditions", nargs="+", required=True)
    p.add_argument("--run-id", default="verified")
    p.add_argument("--cache-level", default="env",
                   choices=["none", "base", "env", "instance"])
    p.add_argument("--max-workers", type=int, default=4)
    a = p.parse_args(argv)
    rd = (STUDY_ROOT / a.results_dir) if not Path(a.results_dir).is_absolute() \
        else Path(a.results_dir)

    summary = {}
    for cond in a.conditions:
        preds = collect_predictions(rd, cond)
        nonempty = sum(1 for v in preds.values() if v["model_patch"].strip())
        print("[official-grade] %s: %d cells, %d non-empty patches"
              % (cond, len(preds), nonempty))
        report = run_official(preds, cond, a.run_id,
                              cache_level=a.cache_level, max_workers=a.max_workers)
        res = parse_report(report)
        n_res = len(res.get("resolved_ids", []))
        n_tot = len(preds)
        summary[cond] = {"resolved": n_res, "total": n_tot,
                         "resolved_ids": res.get("resolved_ids", []),
                         "rate": n_res / n_tot if n_tot else 0.0}

    print("\n===== OFFICIAL RESOLVE RATE =====")
    for cond, s in summary.items():
        print("  %-16s %d/%d = %.1f%%"
              % (cond, s["resolved"], s["total"], 100 * s["rate"]))
    out = rd / "official_grade_summary.json"
    # merge: conditions may be graded one invocation at a time
    merged = {}
    if out.is_file():
        try:
            merged = json.loads(out.read_text("utf-8"))
        except Exception:
            merged = {}
    merged.update(summary)
    out.write_text(json.dumps(merged, indent=2), "utf-8")
    print("\nsaved", out)
    return summary


if __name__ == "__main__":
    main()
