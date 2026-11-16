"""Private companies: opaque ids and fingerprints (privacy.py)."""

from conftest import make_job
from jobtracker import privacy
from jobtracker.dedupe import fingerprint


def test_a_private_posting_gets_a_stable_opaque_id():
    job = make_job(job_id="1376880057", title="Sheffield Tech Insight Programme")
    private = privacy.pseudonymise(job)
    assert private.private
    assert private.job_id == privacy.pseudonymise(job).job_id
    assert "1376880057" not in private.key and ":" not in private.job_id
    assert (private.title, private.url) == (job.title, job.url)  # still whole in memory


def test_a_private_fingerprint_is_a_hash_that_still_matches_the_same_posting():
    job = make_job(title="Sheffield Tech Insight Programme", locations=("Sheffield, GB",))
    private = privacy.pseudonymise(job)
    assert "Sheffield" not in fingerprint(private)
    assert fingerprint(private) == fingerprint(privacy.pseudonymise(make_job(
        job_id="other", title="Sheffield Tech Insight Programme", locations=("Sheffield, GB",)
    )))  # fmt: skip
    assert fingerprint(private) != fingerprint(job)


def test_a_basis_keeps_its_kind_but_not_its_quote():
    assert privacy.redact_basis('description: "all years"') == "description: (private)"
    assert privacy.redact_basis("spring week; the posting does not say otherwise") == "(private)"
