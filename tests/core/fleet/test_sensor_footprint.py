import math

from aggregate_twin.core.fleet.sensor_footprint import SensorFootprint, outside_distance


def test_forward_camera_grows_a_wedge_from_the_body_centre():
    fp = SensorFootprint()
    for p in [(2.0, 1.0), (2.0, -1.0), (1.0, 0.0), (1.5, 0.2)]:     # last two fall inside
        fp.extend(p)
    assert sorted(fp.hull) == sorted([(0.0, 0.0), (2.0, -1.0), (2.0, 1.0)])


def test_points_inside_or_within_margin_do_not_change_the_hull():
    fp = SensorFootprint(margin=0.05)
    for p in [(0.0, 2.0), (2.0, 0.0), (2.0, 2.0)]:
        fp.extend(p)
    before = list(fp.hull)
    assert not fp.extend((1.0, 1.0))           # inside
    assert not fp.extend((2.03, 1.0))          # 3 cm outside: noise
    assert fp.hull == before
    assert fp.extend((2.5, 1.0))               # real growth


def test_vertex_count_stays_capped():
    fp = SensorFootprint(max_vertices=12)
    for k in range(360):                       # a 360 deg sensor tracing a 3 m circle
        a = math.radians(k)
        fp.extend((3.0 * math.cos(a), 3.0 * math.sin(a)))
    assert len(fp.hull) <= 12
    assert outside_distance(fp.hull, (2.0, 0.0)) == 0.0


def test_footprint_is_learned_in_the_body_frame():
    a, b = SensorFootprint(), SensorFootprint()
    # Same detection 2 m straight ahead, robot at two different world poses
    a.observe((0.0, 0.0, 0.0), (2.0, 0.0))
    b.observe((5.0, 5.0, math.pi / 2), (5.0, 7.0))
    assert [[round(v, 6) for v in p] for p in a.hull] == [[round(v, 6) for v in p] for p in b.hull]


def test_far_reports_are_ignored_as_outliers():
    fp = SensorFootprint(max_range=20.0)
    assert not fp.extend((50.0, 0.0))
    assert fp.hull == [(0.0, 0.0)]
