"""Fuzzy-threshold experiment: positives (perturbed true spans) vs negatives (unrelated text from the same vocabulary).
usage: python threshold_experiment.py big300.pdf   (generate with gen_big_pdf.py)"""
import sys, random, statistics as st, time
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import quote_locator_prototype as ql
doc = ql.PdfiumDoc(open(sys.argv[1] if len(sys.argv) > 1 else "big300.pdf", "rb").read())
random.seed(3)
VOC = ("capital investment network regulated revenue operating profit adjusted underlying group dividend share pence million billion cash flow debt interest tax disposal asset growth transmission distribution electricity gas customers reliability decarbonisation climate board governance remuneration committee director audit risk strategy sustainability emissions scope reduction target statutory result increase decrease year period compared principally driven timing movements receipts payments").split()
pages = random.sample(range(1, 301), 30)
pos = {k: [] for k in ["exact", "replace2", "drop2", "extra5", "swap", "half"]}
neg = {"random-vocab": [], "other-page-same-span": []}
t_f = []
for pg in pages:
    pw = doc.page_words(pg)
    idx = ql._Index(pw.words)
    ws = [w.text for w in pw.words]
    for L in (12, 25, 45):
        i = random.randrange(0, len(ws) - L - 1)
        span = ws[i:i+L]
        def sc(words, bucket, where):
            q = " ".join(words)
            t = time.perf_counter(); c = ql._fuzzy(idx, ql.squash(q)); t_f.append((time.perf_counter()-t)*1000)
            where[bucket].append(0.0 if c is None else c.score)
        sc(span, "exact", pos)
        r = span[:]; 
        for j in random.sample(range(L), 2): r[j] = random.choice(VOC)
        sc(r, "replace2", pos)
        d = span[:]; 
        for j in sorted(random.sample(range(2, L-2), 2), reverse=True): del d[j]
        sc(d, "drop2", pos)
        sc([random.choice(VOC) for _ in range(3)] + span + [random.choice(VOC) for _ in range(2)], "extra5", pos)
        s = span[:]; j = random.randrange(0, L-1); s[j], s[j+1] = s[j+1], s[j]; sc(s, "swap", pos)
        sc(span[:L//2], "half", pos)
        sc([random.choice(VOC) for _ in range(L)], "random-vocab", neg)
        # span from another page
        other = random.choice([p for p in pages if p != pg]); ow = [w.text for w in doc.page_words(other).words]
        k = random.randrange(0, len(ow)-L-1); sc(ow[k:k+L], "other-page-same-span", neg)
print(f"fuzzy time ms: mean {st.mean(t_f):.2f} max {max(t_f):.2f}")
def summ(name, v):
    v = sorted(v); n = len(v)
    print(f"{name:22s} n={n:3d} min={v[0]:.2f} p5={v[int(.05*n)]:.2f} p25={v[int(.25*n)]:.2f} median={v[n//2]:.2f} p75={v[int(.75*n)]:.2f} p95={v[min(n-1,int(.95*n))]:.2f} max={v[-1]:.2f}")
print("POSITIVES"); [summ(k, v) for k, v in pos.items()]
print("NEGATIVES (unrelated text built from the SAME vocabulary = worst case)"); [summ(k, v) for k, v in neg.items()]
allneg = sum(neg.values(), []); allpos = sum(pos.values(), [])
for th in (0.6, 0.65, 0.7, 0.75, 0.8, 0.85):
    print(f"thr {th}: positives accepted {sum(x>=th for x in allpos)/len(allpos):.2%}   negatives accepted (false positives) {sum(x>=th for x in allneg)/len(allneg):.2%}")
