"""Чи відрізняє міра схожості лабу від лаби — на РЕАЛЬНИХ студентських звітах (З7).

`shapecheck` рахує те саме на живому журналі й зі зразками викладача. Тут ні
того, ні того немає: на вхід — запити, вибрані `scripts/extract-lab-queries.py`
зі звітів, зданих у LMS. Кожна тека звітів = одна лабораторна; зразків
викладача немає, тож роль «еталона» лаби грають запити решти студентів тієї
самої теки.

Порівнює **дві міри** (docs/research/05):

* **v1** — `sqlshape` як є (Aligon, `tables 0.45`, без канонізації, звичайний
  Жаккар);
* **v2+idf** — `sqlshape2`: компоненти Aligon із резолвінгом псевдонімів і
  розцукренням, ваги за консенсусом, **зважений Жаккар з IDF по лабах**.

Плюс: `--ablation` додає v1+idf і v2-без-idf, щоб бачити внесок кожного кроку.

Жодного авто-вердикту — вирішує людина. Пункт «правильний шлях не один»
врахований: рахується не лише top-1, а **recall@k** (чи потрапляє правильна
лаба в топ-2 / топ-3, коли роботі дозволено бути схожою й на інші).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, median

_ROOT = Path(__file__).resolve().parent.parent.parent
if not __package__:
    sys.path.insert(0, str(_ROOT / "front"))
    from app import sqlshape, sqlshape2
else:
    from . import sqlshape, sqlshape2

DEFAULT_JSONL = _ROOT / "labs" / "labs" / "_extracted" / "queries.jsonl"

TOP_K = 5
SUSPECT = 0.35
THRESHOLDS = (0.30, 0.40, 0.45, 0.50, 0.55, 0.60)
POOL_CAP = 160
STUDENT_CAP = 30
# Смуги вердикту (docs/research/05 §3.4).
T_HI, T_LO, DELTA = 0.55, 0.40, 0.08


class Measure:
    def __init__(self, name, shape_fn, sim_fn, weights, use_idf):
        self.name = name
        self.shape = shape_fn
        self._sim = sim_fn
        self.weights = weights
        self.use_idf = use_idf
        self.keys = [k for k, _w in weights]

    def sim(self, a, b, idf=None):
        return self._sim(a, b, self.weights, idf if self.use_idf else None)


def _v1_sim(a, b, weights, idf):
    return sqlshape2.similarity(a, b, weights, idf)


MEASURES = {
    "v1":     Measure("v1",     sqlshape.shape,  _v1_sim, sqlshape2.WEIGHTS_V1, False),
    "v1+idf": Measure("v1+idf", sqlshape.shape,  _v1_sim, sqlshape2.WEIGHTS_V1, True),
    "v2":     Measure("v2",     sqlshape2.shape, sqlshape2.similarity, sqlshape2.WEIGHTS_V2, False),
    "v2+idf": Measure("v2+idf", sqlshape2.shape, sqlshape2.similarity, sqlshape2.WEIGHTS_V2, True),
}


def _cohort(lab_dir: str) -> str:
    return "ІН-31" if lab_dir.startswith("2494") else "ІН-32"


def _sig(sh, keys):
    return tuple(frozenset(sh.get(k, ())) for k in keys)


def _dedup(shs, keys):
    seen, out = set(), []
    for sh in shs:
        s = _sig(sh, keys)
        if s not in seen:
            seen.add(s)
            out.append(sh)
    return out


def _cap(shs, n):
    if len(shs) <= n:
        return list(shs)
    step = len(shs) / n
    return [shs[int(i * step)] for i in range(n)]


def _score(qs, pool, m: Measure, idf=None) -> float:
    if not qs or not pool:
        return 0.0
    per = sorted((max(m.sim(q, p, idf) for p in pool) for q in qs), reverse=True)
    return mean(per[:TOP_K] or per)


def _dist(xs) -> str:
    if not xs:
        return "—"
    s = sorted(xs)
    return (f"n={len(s):4d} сер={mean(s):.2f} мед={median(s):.2f} "
            f"мін={s[0]:.2f} макс={s[-1]:.2f}")


def _bar(v, width=20) -> str:
    n = max(0, min(width, int(round(v * width))))
    return "█" * n + "·" * (width - n)


def _load(path: Path, m: Measure):
    raw: dict[tuple[str, str], list[str]] = {}
    cohort_of: dict[str, str] = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        raw.setdefault((r["lab"], r["student"]), []).append(r["sql"])
        cohort_of[r["lab"]] = _cohort(r["lab_dir"])
    per_student = {k: _cap(_dedup([m.shape(s) for s in v], m.keys), STUDENT_CAP)
                   for k, v in raw.items()}
    return per_student, cohort_of


def _pools(per_student, labs, keys):
    pool = {}
    for lab in labs:
        rawp = [q for (l, _s), v in per_student.items() if l == lab for q in v]
        pool[lab] = _cap(_dedup(rawp, keys), POOL_CAP)
    return pool


def _own_pool(per_student, lab, stu, keys):
    return _cap(_dedup([q for (l, s2), v in per_student.items()
                        if l == lab and s2 != stu for q in v], keys), POOL_CAP)


# ---------------------------------------------------------------- 1. відбраковка
def reject_layer(per_student, m: Measure, show=6):
    print("=" * 74)
    print(f"1. ВІДБРАКОВКА ВСЕРЕДИНІ ТЕКИ — міра {m.name}, дивитися очима")
    print("=" * 74)
    print(f"Бал = схожість запитів студента на решту тієї самої теки (середнє з "
          f"{TOP_K} найкращих).\nНижче {SUSPECT:.2f} — під питання: не з цієї "
          f"теки / незвичний розвʼязок / скріншоти.\n")
    by_lab: dict[str, list[str]] = {}
    for (lab, stu) in per_student:
        by_lab.setdefault(lab, []).append(stu)
    flagged_all = []
    for lab in sorted(by_lab):
        stus = by_lab[lab]
        scored = []
        for stu in stus:
            others = _cap(_dedup([q for s2 in stus if s2 != stu
                                  for q in per_student[(lab, s2)]], m.keys), POOL_CAP)
            scored.append((_score(per_student[(lab, stu)], others, m), stu,
                           len(per_student[(lab, stu)])))
        scored.sort()
        flag = [s for s in scored if s[0] < SUSPECT]
        flagged_all += [(lab, stu, sc) for sc, stu, _n in flag]
        print(f"[{lab}]  студентів {len(stus)}, під питанням {len(flag)}")
        for sc, stu, nq in scored[:show]:
            print(f"   {sc:.2f}  {stu:<20s} запитів {nq}{'  ⚑' if sc < SUSPECT else ''}")
        if len(scored) > show:
            print(f"   … решта {len(scored) - show} ≥ {scored[show][0]:.2f}")
        print()
    return flagged_all


def write_suspects(path: Path, flagged):
    want = {(lab, stu) for lab, stu, _ in flagged}
    texts: dict[tuple[str, str], list[str]] = {}
    for line in path.read_text().splitlines():
        r = json.loads(line)
        k = (r["lab"], r["student"])
        if k in want:
            texts.setdefault(k, []).append(r["sql"])
    out = path.parent / "SUSPECTS.md"
    with out.open("w") as fh:
        fh.write("# Роботи під питанням — перевірити очима (З7)\n\n")
        fh.write("Бал = схожість на решту робіт тієї самої теки. Низький бал ≠ "
                 "провина:\nчасто це скріншот-звіт, seed-скрипт або незвичний "
                 "чесний розвʼязок.\n\n")
        for lab, stu, sc in sorted(flagged):
            fh.write(f"## [{lab}] · {stu} · {sc:.2f}\n\n")
            for q in sorted(set(texts.get((lab, stu), []))):
                fh.write(f"    {q}\n")
            fh.write("\n")
    print(f"→ зведення текстів: {out}\n")


# ----------------------------------------------- 2. привʼязка: матриця + recall@k
def attribution(per_student, cohort_of, m: Measure, scope: str):
    labs = sorted({l for (l, _s) in per_student
                   if scope == "всі" or cohort_of[l] == scope})
    if len(labs) < 2:
        return None
    pool = _pools(per_student, labs, m.keys)
    idf = (sqlshape2.collect_df(dict(enumerate(pool.values())), m.weights)
           if m.use_idf else None)
    idx = {l: i for i, l in enumerate(labs)}
    conf = [[0] * len(labs) for _ in labs]          # куди тягне серед ЧУЖИХ (для ока)
    conf_full = [[0] * len(labs) for _ in labs]      # СПРАВЖНЯ матриця плутанини: рядок — істина, стовпець — argmax по всіх
    ranks, own_s, alien_s = [], [], []
    bands = {"певно_вірно": 0, "певно_хибно": 0, "під_питанням": 0, "відкинуто": 0}
    pulls = []
    for (lab, stu), qs in per_student.items():
        if lab not in idx:
            continue
        sc_all = {lab: _score(qs, _own_pool(per_student, lab, stu, m.keys), m, idf)}
        for o in labs:
            if o != lab:
                sc_all[o] = _score(qs, pool[o], m, idf)
        own_s.append(sc_all[lab])
        alien_only = {o: v for o, v in sc_all.items() if o != lab}
        alien_s += alien_only.values()
        # ранг власної лаби серед усіх
        order = sorted(sc_all, key=lambda k: sc_all[k], reverse=True)
        ranks.append(order.index(lab) + 1)
        conf_full[idx[lab]][idx[order[0]]] += 1
        # матриця: куди тягне серед ЧУЖИХ (лише коли argmax і так хибний)
        pred = max(alien_only, key=lambda k: alien_only[k])
        conf[idx[lab]][idx[pred]] += 1
        if alien_only[pred] >= 0.45:
            pulls.append((alien_only[pred], lab, stu, pred))
        # смуга вердикту
        top1, top2 = sc_all[order[0]], (sc_all[order[1]] if len(order) > 1 else 0.0)
        if top1 >= T_HI and top1 - top2 > DELTA:
            bands["певно_вірно" if order[0] == lab else "певно_хибно"] += 1
        elif top1 >= T_LO:
            bands["під_питанням"] += 1
        else:
            bands["відкинуто"] += 1

    n = len(ranks)
    r1 = sum(1 for r in ranks if r == 1) / n
    r2 = sum(1 for r in ranks if r <= 2) / n
    r3 = sum(1 for r in ranks if r <= 3) / n
    mrr = mean(1 / r for r in ranks)
    f1 = _f1_from_confusion(conf_full, labs)
    return dict(labs=labs, conf=conf, conf_full=conf_full, pulls=pulls, own=own_s,
                alien=alien_s, r1=r1, r2=r2, r3=r3, mrr=mrr, bands=bands, n=n, f1=f1)


def _f1_from_confusion(conf_full, labs):
    """Precision/recall/F1 на клас (argmax-класифікація: рядок — істина,
    стовпець — передбачення) + macro-F1. micro-F1 багатокласової
    однокласової задачі дорівнює accuracy, тобто recall@1 (r1) — окремо не
    рахуємо, щоб не дублювати те саме число під іншою назвою."""
    n = len(labs)
    per_lab = {}
    for i, lab in enumerate(labs):
        tp = conf_full[i][i]
        fn = sum(conf_full[i][j] for j in range(n) if j != i)
        fp = sum(conf_full[j][i] for j in range(n) if j != i)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_lab[lab] = dict(precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn)
    macro_f1 = mean(v["f1"] for v in per_lab.values())
    return dict(per_lab=per_lab, macro_f1=macro_f1)


def print_attr(res, m: Measure, scope: str):
    labs = res["labs"]
    print("=" * 74)
    print(f"2. ПРИВʼЯЗКА — міра {m.name}, зріз «{scope}», {len(labs)} лаб, {res['n']} робіт")
    print("=" * 74)
    print(f"   recall@1 {res['r1']:.0%}   recall@2 {res['r2']:.0%}   "
          f"recall@3 {res['r3']:.0%}   MRR {res['mrr']:.2f}")
    print(f"   вердикт: певно вірно {res['bands']['певно_вірно']}, "
          f"певно ХИБНО {res['bands']['певно_хибно']}, "
          f"під питанням {res['bands']['під_питанням']}, "
          f"відкинуто {res['bands']['відкинуто']}")
    print(f"   своя  : {_dist(res['own'])}")
    print(f"   чужа  : {_dist(res['alien'])}")
    print(f"   проміжок медіан: {median(res['own']) - median(res['alien']):+.2f}\n")
    print("   куди тягне серед ЧУЖИХ (рядок — де лежить, стовпець — приймач):")
    print("      " + "".join(f"{i:>4d}" for i in range(len(labs))))
    for i, a in enumerate(labs):
        row = "".join(f"{res['conf'][i][j] or '·':>4}" for j in range(len(labs)))
        print(f"  {i:2d} {row}   {a}")
    if res["pulls"]:
        print("\n   тягне до чужої з балом ≥ 0.45:")
        for sc, lab, stu, pred in sorted(res["pulls"], reverse=True)[:12]:
            print(f"     {sc:.2f}  {stu:<16s} [{lab}] → [{pred}]")
    print()
    print_confusion(res, m)


def print_confusion(res, m: Measure):
    """Справжня матриця плутанини (argmax по всіх лабах, own включно) + F1
    на клас. На відміну від print_attr вище (там — «куди тягне серед чужих»,
    для ока), це саме те, що йде в сегментаційний експеримент П4."""
    labs, conf = res["labs"], res["conf_full"]
    f1 = res["f1"]
    print(f"   МАТРИЦЯ ПЛУТАНИНИ (argmax, own включно) — міра {m.name}")
    print("      " + "".join(f"{i:>4d}" for i in range(len(labs))))
    for i, a in enumerate(labs):
        row = "".join(f"{conf[i][j] or '·':>4}" for j in range(len(labs)))
        print(f"  {i:2d} {row}   {a}")
    print(f"\n   {'лаба':<28s} {'P':>6s} {'R':>6s} {'F1':>6s}   TP/FP/FN")
    for lab in labs:
        v = f1["per_lab"][lab]
        print(f"   {lab:<28s} {v['precision']:6.2f} {v['recall']:6.2f} {v['f1']:6.2f}   "
              f"{v['tp']}/{v['fp']}/{v['fn']}")
    print(f"   {'macro-F1':<28s} {'':>6s} {'':>6s} {f1['macro_f1']:6.2f}")
    print(f"   (micro-F1 = accuracy = recall@1 = {res['r1']:.2%}, вище)\n")


def threshold_sweep(res, m: Measure):
    own, alien = res["own"], res["alien"]
    print(f"   поріг    своїх нижче (хибний відкид)   чужих вище (хибний прийом)  [{m.name}]")
    for t in THRESHOLDS:
        fr = sum(1 for x in own if x < t) / len(own)
        fa = sum(1 for x in alien if x >= t) / len(alien)
        print(f"   {t:5.2f}   {_bar(fr)} {fr:5.0%}    {_bar(fa)} {fa:5.0%}")
    print()


# --------------------------------------------------------------------------- run
def run(path: Path, measures: list[str]) -> int:
    if not path.exists():
        print(f"немає {path} — спершу python scripts/extract-lab-queries.py", file=sys.stderr)
        return 2
    ms = [MEASURES[x] for x in measures]

    # відбраковку і suspects — за рекомендованою мірою (остання в списку)
    rec = ms[-1]
    per_rec, cohort_of = _load(path, rec)
    print(f"Джерело: {path}")
    print(f"Студентів {len(per_rec)}, запитів (деду́п) "
          f"{sum(len(v) for v in per_rec.values())}, лаб {len(set(cohort_of))}")
    print(f"Міри: {', '.join(measures)}   (відбраковка — за {rec.name})\n")
    flagged = reject_layer(per_rec, rec)
    write_suspects(path, flagged)

    per_by_measure = {rec.name: per_rec}
    for m in ms:
        if m.name not in per_by_measure:
            per_by_measure[m.name], _ = _load(path, m)

    for scope in ("ІН-31", "ІН-32", "всі"):
        for m in ms:
            res = attribution(per_by_measure[m.name], cohort_of, m, scope)
            if res:
                print_attr(res, m, scope)
                if scope == "всі":
                    threshold_sweep(res, m)

    print("НЕ доводить: що поріг у продакшні правильний — пул тут зі студентів, а")
    print("не зі зразків викладача. Доводить: чи існує в цих даних межа лаба/лаба")
    print("і наскільки її совають ваги, канонізація та IDF.")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", nargs="?", type=Path, default=DEFAULT_JSONL)
    ap.add_argument("--ablation", action="store_true",
                    help="усі 4 міри (v1, v1+idf, v2, v2+idf)")
    ap.add_argument("--measures", help="через кому: v1,v2+idf")
    a = ap.parse_args()
    if a.measures:
        picks = [x.strip() for x in a.measures.split(",")]
    elif a.ablation:
        picks = ["v1", "v1+idf", "v2", "v2+idf"]
    else:
        picks = ["v1", "v2+idf"]
    sys.exit(run(a.jsonl, picks))
