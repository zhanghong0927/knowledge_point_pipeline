"""Declared capacity and bounded pools for explicit stage-separated runs."""

from dataclasses import dataclass

from batch_pipeline import bounded_map, bounded_pipeline
from benchmark_support import select_cohort


@dataclass(frozen=True)
class ResourceProfile:
    gpu_cards: int = 16
    requests_per_card: int = 32
    limit: int | None = None
    target_per_track: int = 30
    front_limit: int = 64
    extraction_limit: int = 128
    model_limit: int = 128
    front_workers: int = 64
    cpu_workers: int = 8
    production_workers: int = 8
    mount_workers: int = 8
    prepare_workers: int = 2
    max_pending: int = 128
    calibration_evidence: dict | None = None
    admission_limit: int = 30

    def __post_init__(self):
        for name in ("gpu_cards", "requests_per_card", "target_per_track", "admission_limit", "front_limit",
                     "extraction_limit", "model_limit", "front_workers", "cpu_workers",
                     "production_workers", "mount_workers", "prepare_workers", "max_pending"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.admission_limit > 100 or self.target_per_track > self.admission_limit:
            raise ValueError("Book target exceeds explicitly allowed admission limit")
        if self.limit is not None and (type(self.limit) is not int or
                                       not 1 <= self.limit <= self.gpu_cards * self.requests_per_card):
            raise ValueError("limit exceeds declared resource budget")
        for name in ('front_limit', 'extraction_limit', 'model_limit'):
            if getattr(self, name) > self.declared_request_budget:
                raise ValueError(f'{name} exceeds declared resource budget')

    @property
    def declared_request_budget(self):
        return self.gpu_cards * self.requests_per_card

    @property
    def global_limit(self):
        return min(self.limit or self.declared_request_budget,
                   max(self.front_limit, self.extraction_limit, self.model_limit))

    @property
    def round_limits(self):
        """Compatibility alias for extraction; new callers specify the phase."""
        return self.round_limits_for('production')

    def round_limits_for(self, kind: str):
        configured = {'front': self.front_limit, 'production': self.extraction_limit,
                      'cleaning': self.model_limit, 'mounting': self.model_limit,
                      'boundary': self.model_limit}[kind]
        base = min(configured, self.global_limit)
        return base, max(1, base // 4), max(1, base // 16)

    def job_workers(self, kind: str, round_number: int = 1):
        if round_number not in (1, 2, 3):
            raise ValueError("round_number must be 1, 2 or 3")
        if kind == 'front':
            return 1
        # Native producers borrow from the shared gateway budget. This is an
        # upper bound, never a reserved or fixed quota for one book/subject.
        return self.round_limits_for(kind)[round_number - 1]

    def phase_workers(self, kind: str, round_number: int = 1):
        if round_number not in (1, 2, 3):
            raise ValueError('round_number must be 1, 2 or 3')
        jobs = {"front": self.front_workers, "production": self.production_workers,
                "cleaning": self.production_workers, "mounting": self.mount_workers,
                "boundary": self.mount_workers}[kind]
        return min(jobs, self.round_limits_for(kind)[round_number - 1])

    def as_dict(self):
        return {"declared_gpu_cards": self.gpu_cards,
                "requests_per_card": self.requests_per_card,
                "declared_request_budget": self.declared_request_budget,
                "global_request_limit": self.global_limit,
                "round_limits": list(self.round_limits),
                "round_limits_scope": "dictionary_extraction",
                "phase_round_limits": {kind: list(self.round_limits_for(kind))
                                       for kind in ('front', 'production', 'cleaning', 'mounting', 'boundary')},
                "limit_origin": ('native_performance_replay_with_conservative_downstream_budget'
                                 if self.calibration_evidence else 'codex_candidate_pending_calibration'),
                "performance_calibrated": bool(self.calibration_evidence),
                "performance_calibration_scope": self.calibration_evidence.get('scope',
                    ['front', 'dictionary_extraction']) if self.calibration_evidence else [],
                "calibration_evidence": self.calibration_evidence,
                "downstream_budget_calibrated": False,
                "cleaning_budget_calibrated": bool(self.calibration_evidence and
                    'important_cleaning' in self.calibration_evidence.get('scope', [])),
                "mounting_budget_calibrated": False,
                "target_per_track": self.target_per_track,
                "admission_limit": self.admission_limit,
                "front_workers": self.front_workers,
                "cpu_workers": self.cpu_workers,
                "production_workers": self.production_workers,
                "mount_workers": self.mount_workers,
                "prepare_workers": self.prepare_workers,
                "max_pending": self.max_pending,
                "producer_scheduling": "shared_elastic_global_pool",
                "configured_per_job_workers": {
                    kind: [self.job_workers(kind, round_number) for round_number in (1, 2, 3)]
                    for kind in ("front", "production", "cleaning", "mounting", "boundary")},
                "actual_gpu_cards_verified": False,
                "observed_gpu_cards": None}


def select_admissions(approved, target=30, seed=20261012, maximum=30):
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError("maximum must be between 1 and 100")
    if type(target) is not int or not 1 <= target <= maximum:
        raise ValueError("target exceeds explicitly allowed admission limit")
    selected = select_cohort(approved, target=target, seed=seed)
    return selected, {"target_maximum": target, "selected": len(selected),
                      "shortfall": target - len(selected), "seed": seed,
                      "no_review_promoted": True}


def run_bounded_phase(items, prepare, process, *, prepare_workers=2,
                      process_workers=8, max_pending=32):
    return list(bounded_pipeline(items, prepare, process, prepare_workers,
                                 process_workers, max_pending))


def run_simple_phase(items, process, workers):
    indexed = enumerate(items)
    completed = bounded_map(indexed, lambda pair: (pair[0], process(pair[1])), workers)
    return [value for _, value in sorted(completed, key=lambda pair: pair[0])]
