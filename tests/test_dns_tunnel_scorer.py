import pytest

import dns_tunnel_scorer as ds

RANDOM_LABELS = [
    "a8f3k2d9q1", "b7e1m4x8z2", "c6n9p3r5t7", "d5q8s2u4w6",
    "e4r7t9v1y3", "f3s6u8w0a2", "g2t5v7x9b1", "h1u4w6y8c0",
    "i9v3x5z7d2", "j8w2y4a6e1", "k7x1z3b5f0", "l6y0a2c4g9",
    "m5z9b1d3h8", "n4a8c0e2i7", "o3b7d9f1j6", "p2c6e8g0k5",
    "q1d5f7h9l4", "r0e4g6i8m3", "s9f3h5j7n2", "t8g2i4k6o1",
]


def event(client="c1", qname="www.example.com", qtype="A", response_bytes=60,
          rcode="NOERROR", rtt=0.02, now=0.0, stream="main"):
    return {
        "client": client,
        "qname": qname,
        "qtype": qtype,
        "response_bytes": response_bytes,
        "rcode": rcode,
        "rtt": rtt,
        "now": now,
        "stream": stream,
    }


def test_random_looking_cdn_labels_are_benign():
    scorer = ds.TunnelScorer(threshold=1.5)
    for index, label in enumerate(RANDOM_LABELS):
        scorer.observe(event(qname=label + ".cdn.example.com",
                             response_bytes=60, now=float(index)))
    assert scorer.verdict("c1", now=20.0) == "benign"
    assert scorer.features("c1", now=20.0)["txt_bytes"] == 0


def test_txt_heavy_queries_are_tunnels():
    scorer = ds.TunnelScorer(threshold=1.5)
    for index, label in enumerate(RANDOM_LABELS[:10]):
        scorer.observe(event(qname=label + ".tunnel.example.com", qtype="TXT",
                             response_bytes=800, now=float(index)))
    assert scorer.features("c1", now=10.0)["txt_bytes"] == 8000
    assert scorer.verdict("c1", now=10.0) == "tunnel"


def test_sliding_window_keeps_cross_boundary_events():
    scorer = ds.TunnelScorer(window=60.0)
    scorer.observe(event(qname="a.example.com", now=58.0))
    scorer.observe(event(qname="b.example.com", now=62.0))
    assert scorer.features("c1", now=63.0)["queries"] == 2


def test_txt_response_bytes_are_summed():
    scorer = ds.TunnelScorer()
    scorer.observe(event(qtype="TXT", response_bytes=100, now=0.0))
    scorer.observe(event(qtype="TXT", response_bytes=200, now=1.0))
    scorer.observe(event(qtype="TXT", response_bytes=300, now=2.0))
    scorer.observe(event(qtype="A", response_bytes=5000, now=3.0))
    assert scorer.features("c1", now=4.0)["txt_bytes"] == 600


def test_nxdomain_ratio_triggers_tunnel():
    scorer = ds.TunnelScorer(threshold=1.5)
    for index in range(12):
        scorer.observe(event(qname="n%d.example.com" % index,
                             rcode="NXDOMAIN", now=float(index)))
    features = scorer.features("c1", now=12.0)
    assert features["nx_ratio"] == 1.0
    assert scorer.verdict("c1", now=12.0) == "tunnel"


def test_whitelist_is_exact_not_suffix():
    scorer = ds.TunnelScorer(whitelist=["cdn.example.com"])
    assert scorer.is_whitelisted("cdn.example.com") is True
    assert scorer.is_whitelisted("evil.cdn.example.com") is False


def test_concurrent_streams_are_aggregated():
    scorer = ds.TunnelScorer()
    scorer.observe(event(qname="a.example.com", stream="s1", now=0.0))
    scorer.observe(event(qname="b.example.com", stream="s2", now=0.0))
    features = scorer.features("c1", now=1.0)
    assert features["queries"] == 2
    assert features["observed"] == 2
    assert features["unique_qnames"] == 2


def test_score_decays_after_window():
    scorer = ds.TunnelScorer(window=60.0, threshold=1.5)
    for index in range(12):
        scorer.observe(event(qname="n%d.example.com" % index,
                             rcode="NXDOMAIN", now=0.0))
    assert scorer.verdict("c1", now=1.0) == "tunnel"
    features = scorer.features("c1", now=61.0)
    assert features["queries"] == 0
    assert scorer.score("c1", now=61.0) == 0
    assert scorer.verdict("c1", now=61.0) == "benign"


def test_all_ingested_events_are_observed():
    scorer = ds.TunnelScorer()
    events = [event(qname="n%d.example.com" % index, now=float(index))
              for index in range(120)]
    assert scorer.ingest(events) == 120
    features = scorer.features("c1", now=119.0)
    assert features["observed"] == 120
    assert features["queries"] == 60


def test_rtt_jitter_alone_is_benign():
    scorer = ds.TunnelScorer(threshold=1.5)
    for index in range(10):
        rtt = 0.01 if index % 2 == 0 else 0.5
        scorer.observe(event(qname="www.aaaaaa.com", rtt=rtt, now=float(index)))
    features = scorer.features("c1", now=10.0)
    assert features["rtt_jitter"] > 0.2
    assert scorer.verdict("c1", now=10.0) == "benign"


def test_idn_is_normalized():
    scorer = ds.TunnelScorer(whitelist=["example.com"])
    assert scorer.normalize("ＥＸＡＭＰＬＥ.com.") == "example.com"
    assert scorer.is_whitelisted("ＥＸＡＭＰＬＥ.com") is True


def test_merge_state_combines_instances():
    first = ds.TunnelScorer()
    second = ds.TunnelScorer()
    for index in range(3):
        first.observe(event(qname="a%d.example.com" % index, now=float(index)))
        second.observe(event(qname="b%d.example.com" % index, now=float(index)))
    first.merge_state(second.export_state())
    features = first.features("c1", now=2.0)
    assert features["observed"] == 6
    assert features["queries"] == 6


def test_entropy_feature_uses_longest_label():
    scorer = ds.TunnelScorer()
    scorer.observe(event(client="high", qname="a8f3k2x9q1.example.com", now=0.0))
    scorer.observe(event(client="low", qname="www.aaaaaa.com", now=0.0))
    assert scorer.features("high", now=1.0)["entropy"] > 3.2
    assert scorer.features("low", now=1.0)["entropy"] < 2.0


def test_label_count_is_not_byte_length():
    scorer = ds.TunnelScorer()
    scorer.observe(event(client="many", qname="a.b.c.d.e.f.example.com", now=0.0))
    scorer.observe(event(client="one", qname="x" * 60 + ".com", now=0.0))
    many = scorer.features("many", now=1.0)
    one = scorer.features("one", now=1.0)
    assert many["labels"] == 8
    assert many["max_label_len"] == 7
    assert one["labels"] == 2
    assert one["max_label_len"] == 60


def test_rate_is_computed_over_window():
    scorer = ds.TunnelScorer(window=60.0)
    for index in range(60):
        scorer.observe(event(qname="h%d.example.com" % index, now=float(index)))
    assert scorer.features("c1", now=59.0)["rate"] == pytest.approx(1.0)
