from PIL import Image

from spoolmix.imagepick import dominant_colors, pick_pixel


def _img(tmp_path):
    im = Image.new("RGB", (40, 20), (31, 77, 43))
    for x in range(30, 40):
        for y in range(20):
            im.putpixel((x, y), (240, 200, 40))
    path = tmp_path / "t.png"
    im.save(path)
    return path


def test_pick_pixel(tmp_path):
    p = _img(tmp_path)
    assert pick_pixel(p, 5, 5) == (31, 77, 43)
    assert pick_pixel(p, 35, 10) == (240, 200, 40)


def test_dominant_colors(tmp_path):
    cols = dominant_colors(_img(tmp_path), 2)
    assert cols[0][0] == (31, 77, 43) and abs(cols[0][1] - 0.75) < 1e-6
    assert cols[1][0] == (240, 200, 40)


def test_dominant_colors_k_larger_than_unique(tmp_path):
    assert len(dominant_colors(_img(tmp_path), 5)) == 2
