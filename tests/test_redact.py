"""Secret/PII redaction: gitleaks-aligned vendor patterns + a Shannon-entropy fallback for unknown
tokens, without over-masking prose or file paths."""
from collectors import redact


def test_masks_known_secret_formats():
    m = redact.secrets
    assert m("key sk-ant-api03-" + "a" * 93 + "AA end") == "key [API_KEY] end"
    assert m("OPENAI_API_KEY=sk-" + "Ab3" * 10) == "OPENAI_API_KEY=[API_KEY]"
    assert "[TOKEN]" in m("ghp_" + "a1B2c3" * 6)
    assert "[TOKEN]" in m("glpat-" + "abcdef1234567890abcd")
    assert "[AWS_KEY]" in m("creds AKIAIOSFODNN7EXAMPLE here")
    assert "[GCP_KEY]" in m("AIza" + "b" * 35)
    assert "[STRIPE_KEY]" in m("sk_live_" + "abcABC1234" * 2)
    assert "[JWT]" in m("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij_kl")
    assert m("mail me jane.doe@example.com") == "mail me [EMAIL]"
    assert "[CREDENTIALS]" in m("postgres://user:p4ssw0rd@db:5432/app")
    assert "[REDACTED]" in m('password="hunter2xyz"')


def test_entropy_catches_unknown_tokens():
    # a random-looking long token with no known prefix is still masked
    assert "[SECRET]" in redact.secrets("token Zk9x2Lm8Qp4Rt6Wv1Yb3Nc7Fd5Hj0Ka done")


def test_does_not_over_mask_prose_and_paths():
    keep = [
        "please refactor the scheduler and commit and push the changes",
        "edit /srv/acme-demo/foreman/collectors/scheduler.py now",
        "see https://api.example.com/v1/users/list?page=2 for data",
        "the process_all_the_transcripts helper needs a docstring",
    ]
    for t in keep:
        assert redact.secrets(t) == t, t


def test_full_degrades_to_secrets_without_presidio():
    # in the test env presidio isn't installed, so full == secrets (no crash, no PII tags added)
    t = "email a@b.com and key sk-" + "Zz9" * 10
    assert redact.full(t) == redact.secrets(t)


def test_shannon_entropy_helper():
    assert redact._shannon("aaaaaaaa") == 0.0                 # single symbol -> 0 bits
    assert redact._shannon("abcdefgh") > 2.9                  # 8 distinct -> 3 bits
