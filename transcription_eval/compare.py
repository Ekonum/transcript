"""Paired meeting evaluation against an independently checked verbatim."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import unicodedata


def segments(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows or any(
        not isinstance(s, dict) or not isinstance(s.get("text"), str)
        or not isinstance(s.get("speaker"), str) for s in rows
    ):
        raise ValueError(f"{path}: expected non-empty speaker/text segments")
    return rows


def words(text):
    return re.findall(r"[^\W_]+(?:[@.][^\W_]+)*", unicodedata.normalize("NFC", text).casefold())


def edits(reference, hypothesis):
    previous = list(range(len(hypothesis) + 1))
    for index, word in enumerate(reference, 1):
        row = [index]
        for j, other in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j-1] + (word != other)))
        previous = row
    return previous[-1]


def by_speaker(rows):
    streams = defaultdict(list)
    for seg in rows:
        streams[seg["speaker"].strip().casefold()].extend(words(seg["text"]))
    return streams


def measure(reference, hypothesis, terms):
    ref = " ".join(s["text"] for s in reference)
    hyp = " ".join(s["text"] for s in hypothesis)
    n = len(words(ref))
    if not n:
        raise ValueError("The reference is empty")
    speakers, proposed = by_speaker(reference), by_speaker(hypothesis)
    speaker_edits = sum(edits(speakers.get(name, []), proposed.get(name, []))
                        for name in speakers.keys() | proposed.keys())
    result = {"word_errors": edits(words(ref), words(hyp)), "reference_words": n,
              "speaker_attributed_word_errors": speaker_edits, "term_count_errors": {}}
    for term in terms:
        pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)
        expected = len(pattern.findall(ref))
        if not expected:
            raise ValueError(f"Reference does not contain the declared term: {term}")
        result["term_count_errors"][term] = abs(expected - len(pattern.findall(hyp)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("reference", "gemini", "openai"):
        parser.add_argument("--" + arg, type=Path, required=True)
    parser.add_argument("--term", action="append", default=[])
    args = parser.parse_args()
    ref, gemini, openai = [segments(p) for p in (args.reference, args.gemini, args.openai)]
    baseline, candidate = [measure(ref, output, args.term) for output in (gemini, openai)]
    regressions = [k for k in ("word_errors", "speaker_attributed_word_errors")
                   if candidate[k] > baseline[k]]
    regressions.extend(term for term in args.term
                       if candidate["term_count_errors"][term] > baseline["term_count_errors"][term])
    print(json.dumps({"gemini": baseline, "openai": candidate, "regressions": regressions},
                     ensure_ascii=False, indent=2))
    return int(bool(regressions))


if __name__ == "__main__":
    raise SystemExit(main())
