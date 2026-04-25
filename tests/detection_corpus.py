from __future__ import annotations

from src.catalog_extract import Detection


DETECTION_CORPUS = [
    {
        "name": "one_bird_split_close_centers_a9308223",
        "expected_count": 1,
        "detections": [
            Detection("bird", 0.8772702217102051, 3506, 1167, 5893, 2560, 0.13854545833333334),
            Detection("bird", 0.7741460800170898, 2364, 931, 5316, 2545, 0.198522),
        ],
    },
    {
        "name": "multi_bird_far_apart_a9308395",
        "expected_count": 2,
        "detections": [
            Detection("bird", 0.8881644010543823, 2945, 391, 4658, 2473, 0.14860275),
            Detection("bird", 0.8229416608810425, 2, 2034, 1018, 3360, 0.056134),
        ],
    },
    {
        "name": "tiny_fragments_with_large_primary_a9308507",
        "expected_count": 1,
        "detections": [
            Detection("bird", 0.525871753692627, 4243, 3145, 4507, 3353, 0.002288),
            Detection("bird", 0.43900033831596375, 3496, 3215, 4155, 3557, 0.00939075),
            Detection("bird", 0.28350672125816345, 1220, 725, 3998, 2737, 0.232889),
        ],
    },
]
