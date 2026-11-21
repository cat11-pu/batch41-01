import math
import threading

import pytest

import bc7_block_encoder as bc


def image(pixels, image_id="img"):
    return {"id": image_id, "width": len(pixels[0]), "height": len(pixels), "pixels": pixels}


def solid(width, height, color):
    return [[color for _ in range(width)] for _ in range(height)]


def test_gradient_block_error_is_bounded():
    columns = [0, 60, 130, 200]
    pixels = [[(value, value, value, 255) for value in columns] for _ in range(4)]
    encoded = bc.Bc7BlockEncoder().encode_image(image(pixels))
    assert encoded["stats"]["max_error"] <= 8.0
    assert encoded["stats"]["mean_error"] <= 4.0


def test_alpha_pixels_select_alpha_mode():
    pixels = solid(4, 4, (10, 10, 10, 255))
    pixels[2][2] = (10, 10, 10, 128)
    encoded = bc.Bc7BlockEncoder().encode_image(image(pixels))
    assert encoded["mode_counts"]["alpha"] == 1
    assert encoded["mode_counts"]["opaque"] == 0


def test_opaque_block_mode():
    pixels = solid(4, 4, (10, 10, 10, 255))
    encoded = bc.Bc7BlockEncoder().encode_image(image(pixels))
    assert encoded["mode_counts"]["opaque"] == 1
    assert encoded["mode_counts"]["alpha"] == 0


def test_uniform_black_block_is_exact():
    encoder = bc.Bc7BlockEncoder()
    decoded = encoder.decode_image(encoder.encode_image(image(solid(4, 4, (0, 0, 0, 255)))))
    assert all(pixel == (0, 0, 0, 255) for row in decoded["pixels"] for pixel in row)


def test_uniform_white_block_is_exact():
    encoder = bc.Bc7BlockEncoder()
    decoded = encoder.decode_image(encoder.encode_image(image(solid(4, 4, (255, 255, 255, 255)))))
    assert all(pixel == (255, 255, 255, 255) for row in decoded["pixels"] for pixel in row)


def test_gray_ramp_stays_channel_neutral():
    columns = [0, 32, 64, 96]
    pixels = [[(value, value, value, 255) for value in columns] for _ in range(4)]
    encoder = bc.Bc7BlockEncoder()
    decoded = encoder.decode_image(encoder.encode_image(image(pixels)))
    for row in decoded["pixels"]:
        for red, green, blue, alpha in row:
            assert abs(red - green) <= 1
            assert abs(green - blue) <= 1
            assert alpha == 255


def test_normal_reconstruction_is_unit_length():
    encoder = bc.Bc7BlockEncoder()
    normal = encoder.reconstruct_normal(0.6, 0.0)
    assert normal == pytest.approx((0.6, 0.0, 0.8), abs=1e-6)
    assert math.sqrt(sum(component * component for component in normal)) == pytest.approx(1.0, abs=1e-6)
    stretched = encoder.reconstruct_normal(0.8, 0.8)
    assert math.sqrt(sum(component * component for component in stretched)) == pytest.approx(1.0)
    assert stretched[2] >= 0.0


def test_fallback_format_is_used_when_bc7_missing():
    encoder = bc.Bc7BlockEncoder(fallback="bc3")
    encoder.set_supported(False)
    encoded = encoder.encode_image(image(solid(4, 4, (40, 50, 60, 255))))
    assert encoded["blocks"][0]["format"] == "bc3"
    assert encoded["stats"]["fallback_blocks"] == 1
    decoded = encoder.decode_image(encoded)
    assert decoded["pixels"][0][0] == pytest.approx((40, 50, 60, 255))


def test_missing_fallback_raises():
    encoder = bc.Bc7BlockEncoder(fallback=None)
    encoder.set_supported(False)
    with pytest.raises(ValueError):
        encoder.encode_image(image(solid(4, 4, (40, 50, 60, 255))))


def test_mips_are_built_before_compression():
    columns = [0, 30, 90, 200]
    pixels = [[(value, value, value, 255) for value in columns] for _ in range(4)]
    encoder = bc.Bc7BlockEncoder()
    encoded = encoder.encode_image(image(pixels), mip_levels=2)
    assert encoded["stats"]["mip_source"] == "source"
    mip = encoded["mips"][1]
    assert mip["width"] == 2
    assert mip["height"] == 2
    assert mip["pixels"][0][0] == (15, 15, 15, 255)
    assert mip["pixels"][0][1] == (145, 145, 145, 255)


def test_mip_levels_are_halved():
    encoder = bc.Bc7BlockEncoder()
    encoded = encoder.encode_image(image(solid(8, 8, (90, 90, 90, 255))), mip_levels=3)
    assert [mip["width"] for mip in encoded["mips"]] == [8, 4, 2]
    assert [mip["height"] for mip in encoded["mips"]] == [8, 4, 2]


def test_edge_blocks_replicate_and_crop():
    rows = []
    for y in range(5):
        rows.append([(x * 20, y * 30, 0, 255) for x in range(6)])
    encoder = bc.Bc7BlockEncoder()
    encoded = encoder.encode_image(image(rows))
    assert encoded["stats"]["blocks"] == 4
    assert encoded["edge_padding"] == "replicate"
    assert encoded["blocks"][0]["format"] == "bc7"
    decoded = encoder.decode_image(encoded)
    assert decoded["width"] == 6
    assert decoded["height"] == 5
    corner = decoded["pixels"][4][5]
    assert corner[0] == pytest.approx(100, abs=2)
    assert corner[1] == pytest.approx(120, abs=2)


def test_threaded_encoding_is_deterministic():
    rows = [[((x * 17) % 256, (y * 31) % 256, (x * y) % 256, 255) for x in range(8)] for y in range(8)]
    encoder = bc.Bc7BlockEncoder()
    page = image(rows)
    results = [None, None]
    barrier = threading.Barrier(2)

    def worker(index):
        barrier.wait()
        results[index] = encoder.encode_page(page)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results[0] == results[1]
    assert len(results[0]["blocks"]) == 4


def test_quality_report_includes_chroma_error():
    pixels = []
    for y in range(4):
        row = []
        for x in range(4):
            row.append((255, 0, 0, 255) if (x + y) % 2 == 0 else (0, 255, 0, 255))
        pixels.append(row)
    encoded = bc.Bc7BlockEncoder().encode_image(image(pixels))
    stats = encoded["stats"]
    assert stats["chroma_error"] > 0.0
    assert stats["max_error"] >= stats["mean_error"]


def test_stats_report_page_and_block_counts():
    encoder = bc.Bc7BlockEncoder()
    first = encoder.encode_image(image(solid(4, 4, (0, 0, 0, 255))))
    second = encoder.encode_image(image(solid(8, 8, (255, 255, 255, 255))))
    cumulative = encoder.stats()
    assert cumulative["pages"] == 2
    assert cumulative["blocks"] == 5
    assert first["stats"]["elapsed_ms"] >= 0.0
    assert second["stats"]["elapsed_ms"] >= 0.0
