import unittest

from stage_scheduling import ResourceProfile, select_admissions


class ResourceProfileTests(unittest.TestCase):
    def test_declared_profile_is_not_observed_hardware(self):
        profile = ResourceProfile()
        self.assertEqual(profile.global_limit, 128)
        self.assertEqual(profile.declared_request_budget, 512)
        self.assertEqual(profile.round_limits, (128, 32, 8))
        self.assertEqual(profile.target_per_track, 30)
        self.assertEqual(profile.as_dict()["actual_gpu_cards_verified"], False)
        self.assertEqual(profile.as_dict()["observed_gpu_cards"], None)
        self.assertEqual(profile.job_workers("production", 1), 128)
        self.assertEqual(profile.job_workers("production", 2), 32)
        self.assertEqual(profile.job_workers("production", 3), 8)
        self.assertEqual(profile.job_workers("front", 1), 1)
        self.assertEqual(profile.job_workers("front", 2), 1)
        self.assertEqual(profile.job_workers("front", 3), 1)
        self.assertEqual(profile.job_workers("mounting", 1), profile.global_limit)
        self.assertEqual(profile.as_dict()['producer_scheduling'], 'shared_elastic_global_pool')

    def test_override_cannot_exceed_declared_budget(self):
        self.assertEqual(ResourceProfile(limit=128).round_limits, (128, 32, 8))
        self.assertEqual(ResourceProfile(gpu_cards=8).declared_request_budget, 256)
        self.assertEqual(ResourceProfile(gpu_cards=8).round_limits, (128, 32, 8))
        with self.assertRaises(ValueError):
            ResourceProfile(gpu_cards=16, limit=513)
        with self.assertRaises(ValueError):
            ResourceProfile(gpu_cards=0)
        with self.assertRaises(ValueError):
            ResourceProfile(target_per_track=31)

    def test_deterministic_maximum_and_explicit_shortfall(self):
        rows = [{"book": {"identifier": f"b{i:02}"}, "subject": "education"}
                for i in range(35)]
        selected, details = select_admissions(rows)
        self.assertEqual(len(selected), 30)
        self.assertEqual(details["shortfall"], 0)
        self.assertEqual(selected, select_admissions(list(reversed(rows)))[0])
        short, details = select_admissions(rows[:3])
        self.assertEqual(len(short), 3)
        self.assertEqual(details["shortfall"], 27)
        with self.assertRaises(ValueError):
            select_admissions(rows[:1] * 2)


if __name__ == "__main__":
    unittest.main()
