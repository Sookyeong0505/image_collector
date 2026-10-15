"""reference 와 이식 버전의 검출 결과를 비교한다 (docs/DETECTION_SPEC.md 9-2).

    python scripts/compare_reference.py data/compare/reference.jsonl data/compare/ported.jsonl

두 파일 모두 이미지 1장 = JSON 한 줄이다 (python -m app detect-one --json 형식).
검출은 같은 이미지 안에서 '클래스가 같고 박스가 가장 많이 겹치는 것'끼리 짝을 짓는다.
"""

import json
import statistics
import sys
from pathlib import Path

SCORES = ("yoloConfidence", "swinv2", "dinov2", "dinov2X2")
MATCH_IOU = 0.5   # 이보다 덜 겹치면 다른 검출로 본다


def load(path: Path) -> dict[str, dict]:
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["file"]] = record
    return records


def iou(a: list[int], b: list[int]) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area = lambda box: (box[2] - box[0]) * (box[3] - box[1])
    return inter / (area(a) + area(b) - inter)


def pair_detections(left: list[dict], right: list[dict]) -> tuple[list[tuple[dict, dict, float]], int]:
    """(짝지은 목록, 짝을 못 찾은 검출 수)."""
    pairs, used = [], set()
    for a in left:
        best, best_iou = None, MATCH_IOU
        for index, b in enumerate(right):
            if index in used or a["classId"] != b["classId"]:
                continue
            overlap = iou(a["detectionBox"], b["detectionBox"])
            if overlap >= best_iou:
                best, best_iou = index, overlap
        if best is not None:
            used.add(best)
            pairs.append((a, right[best], best_iou))
    unmatched = (len(left) - len(pairs)) + (len(right) - len(pairs))
    return pairs, unmatched


def describe(values: list[float]) -> str:
    if not values:
        return "-"
    return f"평균 {statistics.mean(values):.4f}  최대 {max(values):.4f}  (n={len(values)})"


def main(reference_path: str, ported_path: str) -> int:
    reference, ported = load(Path(reference_path)), load(Path(ported_path))
    files = sorted(set(reference) & set(ported))
    only = sorted(set(reference) ^ set(ported))
    errors = [f for f in files if "error" in reference[f] or "error" in ported[f]]
    files = [f for f in files if f not in errors]

    verdict_diff, section_diff, count_diff, box_ious, reason_diff = [], [], [], [], []
    score_diffs = {name: [] for name in SCORES}
    unmatched_total = 0
    for name in files:
        r, p = reference[name], ported[name]
        if r["verdict"] != p["verdict"]:
            verdict_diff.append(name)
        if r["section"] != p["section"]:
            section_diff.append(name)
        if len(r["detections"]) != len(p["detections"]):
            count_diff.append(name)
        pairs, unmatched = pair_detections(r["detections"], p["detections"])
        unmatched_total += unmatched
        for a, b, overlap in pairs:
            box_ious.append(overlap)
            for score in SCORES:
                score_diffs[score].append(abs(a[score] - b[score]))
            if a["saveReasons"] != b["saveReasons"]:
                reason_diff.append(f"{name} #{a['detectionId'] + 1}: {a['saveReasons']} ↔ {b['saveReasons']}")

    total = len(files)
    positives = sum(reference[f]["verdict"] == "positive" for f in files)
    print(f"비교한 이미지 {total}장 (reference 양성 {positives}, 음성 {total - positives})")
    if only:
        print(f"한쪽에만 있는 이미지 {len(only)}장: {', '.join(only[:5])}")
    if errors:
        print(f"한쪽이라도 실패한 이미지 {len(errors)}장: {', '.join(errors[:5])}")
    print(f"\n이미지 판정 일치   {total - len(verdict_diff)}/{total} ({(total - len(verdict_diff)) / max(total, 1):.1%})")
    print(f"채택 구역 일치     {total - len(section_diff)}/{total}")
    print(f"검출 개수 일치     {total - len(count_diff)}/{total}")
    print(f"짝지은 검출        {len(box_ious)}개, 짝 없는 검출 {unmatched_total}개")
    print(f"박스 IoU           {describe(box_ious)}")
    for score in SCORES:
        print(f"{score:18} 차이 {describe(score_diffs[score])}")
    for title, items in (("판정이 다른 이미지", verdict_diff), ("구역이 다른 이미지", section_diff),
                         ("검출 개수가 다른 이미지", count_diff), ("판정 사유가 다른 검출", reason_diff)):
        if items:
            print(f"\n{title}:")
            for item in items[:20]:
                if item in reference:
                    r, p = reference[item], ported[item]
                    print(f"  {item}: reference {r['verdict']}/{r['section']}/{len(r['detections'])}개"
                          f"  이식 {p['verdict']}/{p['section']}/{len(p['detections'])}개")
                else:
                    print(f"  {item}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
