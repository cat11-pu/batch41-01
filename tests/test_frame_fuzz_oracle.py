import pytest

import frame_fuzz_oracle as fo


def clean_result():
    return {
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "stack": [],
        "sanitizer_errors": [],
        "stderr": "",
    }


def crash_result(signal=11, stack=("0x1", "0x2"),
                 error_type="heap-buffer-overflow"):
    return {
        "exit_code": 0,
        "signal": signal,
        "timed_out": False,
        "stack": list(stack),
        "error_type": error_type,
        "sanitizer_errors": [{"type": error_type}],
        "stderr": "",
    }


def test_clean_exit_is_not_a_crash():
    assert fo.CrashOracle().classify(clean_result()) == "ok"


def test_rejected_input_is_not_a_crash():
    result = {
        "exit_code": 1,
        "signal": None,
        "timed_out": False,
        "stack": [],
        "sanitizer_errors": [],
        "stderr": "input rejected: bad checksum",
    }
    assert fo.CrashOracle().classify(result) == "rejected"


def test_timeout_is_distinct_from_crash():
    result = {
        "exit_code": 0,
        "signal": 11,
        "timed_out": True,
        "stack": ["0x1"],
        "sanitizer_errors": [],
        "stderr": "",
    }
    assert fo.CrashOracle().classify(result) == "timeout"


def test_signal_is_classified_as_crash():
    assert fo.CrashOracle().classify(crash_result()) == "crash"


def test_same_stack_is_deduped():
    oracle = fo.CrashOracle()
    groups = oracle.dedupe({"a": crash_result(), "b": crash_result()})
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert groups[0]["inputs"] == ["a", "b"]


def test_different_stacks_are_kept_separate():
    oracle = fo.CrashOracle()
    groups = oracle.dedupe({
        "a": crash_result(),
        "b": crash_result(stack=("0x9", "0x8")),
    })
    assert len(groups) == 2
    assert sum(group["count"] for group in groups) == 2


def test_minimized_case_still_crashes():
    oracle = fo.CrashOracle()

    def runner(data):
        return b"BOOM" in data

    case = b"HEADBOOMTAIL"
    out = oracle.minimize(case, runner)
    assert out["reproduced"] is True
    assert b"BOOM" in out["case"]
    assert len(out["case"]) < len(case)
    assert runner(out["case"]) is True


def test_minimizer_keeps_original_when_needed():
    oracle = fo.CrashOracle()

    def runner(data):
        return data == b"KEEP"

    out = oracle.minimize(b"KEEP", runner)
    assert out["case"] == b"KEEP"
    assert out["reproduced"] is True


def test_corpus_clone_is_isolated():
    oracle = fo.CrashOracle()
    original = [bytearray(b"seed"), bytearray(b"other")]
    clone = oracle.clone_corpus(original)
    clone[0][0:1] = b"X"
    assert bytes(original[0]) == b"seed"
    assert bytes(clone[0]) == b"Xeed"
    assert clone[0] is not original[0]


def test_all_sanitizer_errors_are_parsed():
    stderr = (
        "ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1\n"
        "READ of size 4 at 0x1 thread T0\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow a.cc:1\n"
        "ERROR: AddressSanitizer: use-after-free on address 0x2\n"
        "WRITE of size 8 at 0x2 thread T0\n"
    )
    errors = fo.CrashOracle().parse_sanitizer(stderr)
    assert [error["type"] for error in errors] == [
        "heap-buffer-overflow", "use-after-free"]
    assert errors[0]["access"] == "READ"
    assert errors[1]["access"] == "WRITE"


def test_reaped_processes_are_not_reaped_twice():
    oracle = fo.CrashOracle()
    assert oracle.reap([102, 101]) == [101, 102]
    assert oracle.reap([101]) == []
    assert oracle.zombies() == []


def test_coverage_survives_restart(tmp_path):
    oracle = fo.CrashOracle()
    oracle.merge_coverage([1, 5, 9])
    assert oracle.coverage_size() == 3
    path = tmp_path / "coverage.json"
    oracle.save_coverage(str(path))
    other = fo.CrashOracle()
    other.load_coverage(str(path))
    assert other.coverage_size() == 3
    other.merge_coverage([5, 7])
    assert other.coverage_size() == 4


def test_seed_recorded_and_mutation_deterministic():
    oracle = fo.CrashOracle()
    first = oracle.run(lambda data: clean_result(), b"abc", seed=7)
    second = oracle.run(lambda data: clean_result(), b"abc", seed=7)
    assert first["seed"] == 7
    assert first["classification"] == "ok"
    assert first["signature"] == second["signature"]
    assert oracle.mutate(b"abc", 7) == oracle.mutate(b"abc", 7)
    assert oracle.mutate(b"abc", 7) != oracle.mutate(b"abc", 8)


def test_session_reuse_reduces_handshakes():
    oracle = fo.CrashOracle()

    def target(data):
        return clean_result()

    reused = oracle.run_many(target, [b"a", b"b", b"c"], reuse_session=True)
    assert reused["executed"] == 3
    assert reused["handshakes"] == 1
    fresh = oracle.run_many(target, [b"a", b"b"], reuse_session=False)
    assert fresh["handshakes"] == 2


def test_cumulative_memory_limit():
    oracle = fo.CrashOracle(memory_limit=1000)
    assert oracle.memory_ok([400, 400]) is True
    assert oracle.memory_ok([600, 600]) is False
