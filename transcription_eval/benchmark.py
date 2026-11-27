"""Rank meeting runs offline by quality, then full cost, then wall time."""

import argparse
import json
import math
from pathlib import Path

from .compare import by_speaker, edits, segments, words

QUALITY_FIELDS = ("critical_errors", "word_errors", "speaker_word_errors")


def assignment_cost(costs: list[list[int]]) -> int:
    """Minimum total under a one-to-one assignment, including dummy speakers."""
    n = len(costs)
    row_price = [0] * (n + 1)
    col_price = [0] * (n + 1)
    matched_row = [0] * (n + 1)
    for row in range(1, n + 1):
        matched_row[0] = row
        best_cost = [math.inf] * (n + 1)
        previous = [0] * (n + 1)
        visited = [False] * (n + 1)
        col = 0
        while True:
            visited[col] = True
            current = matched_row[col]
            for candidate in range(1, n + 1):
                if visited[candidate]:
                    continue
                cost = costs[current - 1][candidate - 1] - row_price[current] - col_price[candidate]
                if cost < best_cost[candidate]:
                    best_cost[candidate] = cost
                    previous[candidate] = col
            next_col = min((j for j in range(1, n + 1) if not visited[j]), key=best_cost.__getitem__)
            increase = best_cost[next_col]
            for j in range(n + 1):
                if visited[j]:
                    row_price[matched_row[j]] += increase
                    col_price[j] -= increase
                else:
                    best_cost[j] -= increase
            col = next_col
            if matched_row[col] == 0:
                break
        # Follow the cheapest augmenting path and assign its columns to rows.
        while col:
            prior = previous[col]
            matched_row[col] = matched_row[prior]
            col = prior
    return sum(costs[matched_row[j] - 1][j - 1] for j in range(1, n + 1))


def score(reference: list[dict], candidate: list[dict]) -> dict[str, int]:
    reference_words = words(" ".join(row["text"] for row in reference))
    candidate_words = words(" ".join(row["text"] for row in candidate))
    if not reference_words or not candidate_words:
        raise ValueError("Le verbatim doit contenir des paroles.")
    left, right = (list(by_speaker(rows).values()) for rows in (reference, candidate))
    n = max(len(left), len(right))
    left.extend([[]] * (n - len(left)))
    right.extend([[]] * (n - len(right)))
    return {
        "reference_words": len(reference_words),
        "word_errors": edits(reference_words, candidate_words),
        "speaker_word_errors": assignment_cost([[edits(a, b) for b in right] for a in left]),
    }


def quantity(result: dict, key: str) -> float:
    value = result[key]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"Mesure absente ou invalide : {key}.")
    return value


def measure_run(path: Path, reference_path: Path, reference: dict) -> dict:
    run = json.loads(path.read_text(encoding="utf-8"))
    if run.get("audio_sha256") != reference["audio_sha256"]:
        raise ValueError("L'empreinte audio du résultat diffère de celle de la référence.")
    if run.get("status") != "complete":
        return {"status": "failed"}
    if type(run.get("critical_errors")) is not int or run["critical_errors"] < 0:
        raise ValueError("Le décompte des erreurs critiques doit être renseigné après réécoute.")
    return {
        "status": "complete", **score(segments(reference_path), segments(path)),
        "critical_errors": run["critical_errors"],
        "cost_usd": quantity(run, "cost_usd"), "latency_seconds": quantity(run, "latency_seconds"),
    }


def benchmark(path: Path, split: str) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    baseline = corpus["baseline"]
    cases = {alias: case for alias, case in corpus["cases"].items() if case["split"] == split}
    if not cases:
        raise ValueError("Cette partition du corpus est vide.")
    variants = corpus["variants"]
    if baseline not in variants or len(variants) < 2:
        raise ValueError("Une référence de production et au moins une variante sont requises.")

    metrics: dict[str, dict] = {variant: {} for variant in variants}
    for alias, case in cases.items():
        reference_path = path.parent / case["reference"]
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        digest = reference.get("audio_sha256")
        if (reference.get("reviewed_at_audio") is not True or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise ValueError(f"Référence à réécouter ou empreinte manquante : {alias}.")
        for variant, runs in variants.items():
            run_path = runs.get(alias)
            metrics[variant][alias] = (
                measure_run(path.parent / run_path, reference_path, reference) if run_path else {"status": "missing"}
            )
    baseline_runs = metrics[baseline]
    if any(run["status"] != "complete" for run in baseline_runs.values()):
        raise ValueError("Des résultats de production manquent pour cette partition.")

    report = {}
    for variant, runs in metrics.items():
        regressions = {}
        for alias, run in runs.items():
            if run["status"] != "complete":
                regressions[alias] = [run["status"]]
            else:
                worse = [field for field in QUALITY_FIELDS if run[field] > baseline_runs[alias][field]]
                if worse:
                    regressions[alias] = worse
        eligible = not regressions
        totals = (
            {field: sum(row[field] for row in runs.values())
             for field in (*QUALITY_FIELDS, "cost_usd", "latency_seconds")}
            if all(row["status"] == "complete" for row in runs.values()) else None
        )
        if totals is not None:
            # The CLI consumes per-run dollars and seconds at recorded precision.
            totals["cost_usd"] = round(totals["cost_usd"], 6)
            totals["latency_seconds"] = round(totals["latency_seconds"], 3)
        report[variant] = {"eligible": eligible, "regressions": regressions, "totals": totals, "cases": runs}
    order = sorted(
        (variant for variant, result in report.items() if result["eligible"]),
        key=lambda v: tuple(report[v]["totals"][f] for f in (*QUALITY_FIELDS, "cost_usd", "latency_seconds")),
    )
    return {"split": split, "baseline": baseline, "ranking": order, "variants": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--split", choices=("dev", "holdout"), default="dev")
    args = parser.parse_args()
    try:
        result = benchmark(args.corpus, args.split)
    except (OSError, TypeError, ValueError, KeyError) as exc:
        parser.exit(1, f"Banc d'essai invalide ({type(exc).__name__}). {exc if type(exc) is ValueError and not isinstance(exc, json.JSONDecodeError) else ''}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
