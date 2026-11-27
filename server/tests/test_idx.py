from wxserver import idx

SAMPLE = """1:0:d=2026112712:HGT:500 mb:anl:
2:100:d=2026112712:TMP:500 mb:anl:
3:250:d=2026112712:TMP:2 m above ground:anl:
4:400:d=2026112712:APCP:surface:0-1 hour acc fcst:
5:2000000:d=2026112712:PRATE:surface:1 hour fcst:
6:2100000:d=2026112712:TCDC:entire atmosphere (considered as a single layer):1 hour fcst:
"""


def test_parse_offsets_and_ends():
    es = idx.parse(SAMPLE)
    assert [e.n for e in es] == [1, 2, 3, 4, 5, 6]
    assert es[0].end == 99 and es[-1].end is None
    assert es[1].pressure_hpa == 500.0 and es[2].pressure_hpa is None


def test_pick_prefers_instantaneous_and_wildcards():
    es = idx.parse(SAMPLE)
    assert idx.pick(es, ("PRATE",), "surface").n == 5
    assert idx.pick(es, ("MSLMA", "TCDC"), "entire atmosphere*").n == 6
    assert idx.pick(es, ("NOPE",), "surface") is None


def test_plan_ranges_merges_close_messages():
    es = idx.parse(SAMPLE)
    assert len(idx.plan_ranges(es, gap=0)) == 1  # a full inventory is contiguous
    sel = [es[0], es[1], es[4], es[5]]  # skip 2 m TMP and APCP
    groups = idx.plan_ranges(sel, gap=1000)
    assert [len(g[2]) for g in groups] == [2, 2]
    assert groups[0][:2] == (0, 249) and groups[1][:2] == (2000000, None)
    assert len(idx.plan_ranges(sel, gap=3_000_000)) == 1
